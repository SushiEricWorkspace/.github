"""認証済保存原文を保持元M/生成snapshotへ結束する。Data算法は合成しない。"""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .build_inputs import canonical
from .registry import RegistryError
from . import test_managed_save_evidence as saves
from . import test_mod_control_runtime as fixtures
from . import runtime_control


class SaveGameFixtureTests(unittest.TestCase):
    def test_fixture_accepts_both_boot_forms_without_rewriting_source(self):
        raw_boot = '00000000000000000000000000000001'
        for name, boot in (('nt', raw_boot), ('posix', '00000000-0000-0000-0000-000000000001')):
            with self.subTest(os=name):
                case = saves.SaveEvidenceTests()
                try:
                    with patch.object(fixtures, 'os', SimpleNamespace(name=name)), \
                            patch.object(runtime_control, 'os', SimpleNamespace(name=name)):
                        try:
                            case.setUp()
                        except RegistryError as error:
                            self.fail('fixtureの生成identityを保持登録できません: ' + str(error))
                        session, runtime = case.session, case.f.runtime
                        original = tuple(case.requests), session['raw']
                        process, frozen = runtime.game_processes[session['run']]
                        self.assertEqual(raw_boot, session['context']['owner']['bootId'])
                        self.assertEqual(boot, case.f.run['identity']['bootId'])
                        case.serve()
                        self.assertIsNone(session['error'])
                        evidence, pending = case.evidence()
                        self.assertEqual(session['save_evidence'], evidence)
                        self.assertEqual(evidence, pending['managedSave'])
                        self.assertEqual(original, (tuple(case.requests), session['raw']))
                        self.assertEqual(case.requests[1], session['save_request'])
                        self.assertEqual(case.f.channel.send_frame.call_args.args[0], session['completion_response'])
                        self.assertEqual(frozen, runtime.game_processes[session['run']][1])
                        held, snapshot = runtime._game_process(case.f.run)
                        self.assertIs(process, held)
                        self.assertEqual(session['context']['owner'], snapshot['owner'])
                        self.assertEqual(boot, snapshot['run']['identity']['bootId'])
                finally:
                    case.doCleanups()
                    case.base.doCleanups()


class SaveGameBindingTests(unittest.TestCase):
    def setUp(self):
        self.base = saves.SaveEvidenceTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.serve()
        self.assertIsNone(self.base.session['error'])
        self.f, self.session = self.base.f, self.base.session
        self.runtime = self.f.runtime
        self.run_id = self.session['run']
        self.process = self.runtime.game_processes[self.run_id][0]

    def record(self):
        return json.loads(self.f.db.execute('SELECT record FROM runs WHERE id=?', (self.run_id,)).fetchone()[0])

    def write_record(self, value):
        self.f.db.execute('UPDATE runs SET record=? WHERE id=?', (json.dumps(value), self.run_id))
        self.f.db.commit()

    def snapshot(self):
        return json.loads(self.runtime.game_processes[self.run_id][1])

    def write_snapshot(self, value):
        self.runtime.game_processes[self.run_id] = (self.process, canonical(value))

    def rejected(self, change, code='PROCESS_MISMATCH'):
        change()
        before = self.base.unchanged_effects()
        parent = self.base.parent()
        held = dict(self.runtime.game_processes)
        with self.assertRaises(RegistryError) as caught:
            self.base.evidence()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(before, self.base.unchanged_effects())
        self.assertEqual(parent, self.base.parent())
        self.assertEqual(held, self.runtime.game_processes)
        self.process.poll.assert_not_called(); self.process.members.assert_not_called(); self.process.close.assert_not_called()
        self.runtime.process_adapter.identity.assert_not_called()

    def test_original_nonempty_frames_and_reference_hashes_are_unchanged(self):
        before = self.base.unchanged_effects()
        original = self.session['save_request'], self.session['completion_response'], self.session['raw']
        requested = saves.fixtures.wire.decode_frame_v3(original[0])
        self.assertTrue(json.loads(requested['payload']['requestJson'])['status']['state']['payload'])
        evidence, pending = self.base.evidence()
        self.assertEqual({'operationId','requestBodySha256','responseBodySha256','descriptorSha256'}, set(evidence))
        self.assertEqual(self.session['save_evidence'], evidence)
        self.assertEqual(evidence, pending['managedSave'])
        self.assertEqual(original, (self.session['save_request'], self.session['completion_response'], self.session['raw']))
        self.assertEqual(self.snapshot()['owner'], self.session['context']['owner'])
        self.assertEqual(before, self.base.unchanged_effects())

    def test_raw_descriptor_mismatch_with_matching_gate_and_generated_hash(self):
        def change():
            self.f.db.execute('UPDATE run_admission SET descriptor_hash=?', ('1' * 64,))
            self.f.db.commit()
            snapshot = self.snapshot(); snapshot['descriptorSha256'] = '1' * 64
            self.write_snapshot(snapshot)
        self.rejected(change, 'STALE_RUN')

    def test_missing_retained_handle_rejects_without_effect(self):
        self.rejected(lambda: self.runtime.game_processes.pop(self.run_id), 'PROCESS_UNVERIFIED')

    def test_missing_run_record_rejects_without_effect(self):
        def change():
            self.f.db.execute('DELETE FROM runs WHERE id=?', (self.run_id,)); self.f.db.commit()
        self.rejected(change, 'PROOF_REQUIRED')

    def test_other_held_run_cannot_replace_registry_run(self):
        def change():
            run, snapshot = self.record(), self.snapshot()
            run['runId'] = snapshot['run']['runId'] = 'another-retained-run'
            self.runtime.game_processes[run['runId']] = (self.process, canonical(snapshot))
            self.write_record(run)
        self.rejected(change)

    def test_binding_lookup_crossing_deadline_has_no_effect(self):
        clock = [100.0]
        original = self.runtime._game_process
        def lookup(run):
            result = original(run); clock[0] = 102.0; return result
        before = self.base.unchanged_effects()
        with patch.object(self.runtime, '_game_process', side_effect=lookup), \
                patch('dev_server.mod_control_runtime.time.monotonic', side_effect=lambda: clock[0]):
            with self.f.transaction(read_only=True) as c:
                with self.assertRaises(RegistryError) as caught:
                    self.base.managed.save_evidence(c, 'slot-a', self.session, 101.0)
        self.assertEqual('STOP_TIMEOUT', caught.exception.code)
        self.assertEqual(before, self.base.unchanged_effects())


def changed_run(section, field):
    def test(self):
        def change():
            run = self.record()
            value = run if section is None else run[section]
            old = value[field]
            value[field] = old + 1 if type(old) is int else old + '-other'
            self.write_record(run)
        self.rejected(change, {'runId':'PROCESS_UNVERIFIED','nonce':'STALE_RUN'}.get(field, 'PROCESS_MISMATCH'))
    return test


for section, fields in ((None, ('runId','instanceId','generationId','artifactId','nonce')),
        ('identity', ('pid','startId','startMillis','bootId','cwd','executable','commandHash','owner')),
        ('creation', ('pid','stage','ownerGroup'))):
    for field in fields:
        setattr(SaveGameBindingTests, 'test_record_' + (section or 'binding') + '_' + field + '_mismatch', changed_run(section, field))


def changed_snapshot(section, field):
    def test(self):
        def change():
            snapshot = self.snapshot(); value = snapshot if section is None else snapshot[section]
            old = value[field]; value[field] = old + 1 if type(old) is int else old + '-other'
            self.write_snapshot(snapshot)
        self.rejected(change)
    return test


for section, fields in (('owner', ('hostHash','bootId','pid','startId')), (None, ('descriptorSha256',))):
    for field in fields:
        setattr(SaveGameBindingTests, 'test_snapshot_' + (section or 'binding') + '_' + field + '_mismatch', changed_snapshot(section, field))


def changed_handle(field):
    def test(self):
        self.rejected(lambda: setattr(self.process, field, 99999 if field == 'pid' else 'other-group'))
    return test


for field in ('pid','group'):
    setattr(SaveGameBindingTests, 'test_held_' + field + '_mismatch', changed_handle(field))


def changed_run_row(field):
    def test(self):
        def change():
            self.f.db.execute('UPDATE runs SET ' + field + '=? WHERE id=?', ('different', self.run_id))
            self.f.db.commit()
        self.rejected(change, 'PROCESS_UNVERIFIED')
    return test


for field in ('instance','generation'):
    setattr(SaveGameBindingTests, 'test_run_row_' + field + '_mismatch', changed_run_row(field))


class SaveGameBindingRegistryTests(unittest.TestCase):
    def setUp(self):
        self.case = saves.SaveEvidenceRegistryTests()
        self.case.setUp(); self.addCleanup(self.case.doCleanups)
        self.runtime, self.base, self.f = self.case.runtime, self.case.base, self.case.f

    def test_public_stop_and_start_use_original_transaction_and_keep_binding(self):
        self.case.public_stop()
        self.assertEqual(1, len(self.runtime.game_processes))
        self.assertEqual(1, len(self.f.adapter.created))

    def test_initial_binding_failure_preserves_sources_without_stop_effect(self):
        session = self.base.start()
        entered, release = threading.Event(), threading.Event()
        original = self.base.channel.wait_peer_close.side_effect
        def close(deadline):
            result = original(deadline)
            process, raw = self.runtime.game_processes[session['run']]
            snapshot = json.loads(raw); snapshot['owner']['hostHash'] = '0' * 64
            self.runtime.game_processes[session['run']] = process, canonical(snapshot)
            entered.set()
            if not release.wait(5): raise TimeoutError('試験の初回保存原文照合')
            return result
        self.base.channel.wait_peer_close.side_effect = close
        self.base.stop_ready.set(); self.base.save_ready.set()
        try:
            self.assertTrue(entered.wait(5))
            before = self.case.query('SELECT * FROM reservations ORDER BY operation,reference')
            with self.assertRaises(RegistryError): self.f.start('competing-start')
            self.assertEqual(before, self.case.query('SELECT * FROM reservations ORDER BY operation,reference'))
        finally:
            release.set()
        self.case.join(session)
        self.assertIsInstance(session['error'], RegistryError)
        self.assertEqual('PROCESS_MISMATCH', session['error'].code)
        self.assertFalse(session['done'].is_set())
        self.assertEqual('RECOVERY_REQUIRED', self.f.state()['state'])
        self.assertEqual(before, self.case.query('SELECT * FROM reservations ORDER BY operation,reference'))
        parent = self.case.query('SELECT * FROM runtime_operations WHERE id=?', (session['stop_operation'],))[0]
        self.assertEqual('PENDING', parent['status'])
        self.assertNotIn('managedSave', json.loads(parent['response']))
        self.assertIn('save_request', session); self.assertIn('completion_response', session)
        self.assertIn(session['run'], self.runtime.processes)
        self.assertFalse(self.runtime.game_processes[session['run']][0].closed)
        self.assertEqual(['GRANT','ACK','BLOCKED','COMPLETION'], self.base.trace)
        self.assertEqual(1, len(self.f.adapter.created)); self.assertEqual(0, self.f.rcon_calls)

    def final_change(self, *, direct=False):
        session = self.base.start()
        entered, release, reserved = threading.Event(), threading.Event(), threading.Event()
        original = self.runtime._stop
        def stop(*args):
            reserved.set()
            result = original(*args)
            entered.set()
            if not release.wait(5): raise TimeoutError('試験の元M最終照合')
            return result
        def change():
            self.assertTrue(entered.wait(5))
            before = self.case.query('SELECT * FROM reservations ORDER BY operation,reference')
            self.assertEqual(1, self.f.state()['pending'])
            with self.assertRaises(RegistryError): self.f.start('competing-start')
            self.assertEqual(before, self.case.query('SELECT * FROM reservations ORDER BY operation,reference'))
            self.assertEqual(1, len(self.f.adapter.created))
            with self.runtime._transaction(self.case.instance) as c:
                run = json.loads(c.execute('SELECT record FROM runs WHERE id=?', (session['run'],)).fetchone()[0])
                run['identity']['startId'] += '-other'
                c.execute('UPDATE runs SET record=? WHERE id=?', (json.dumps(run), session['run']))
            return before
        with patch.object(self.runtime, '_stop', side_effect=stop), ThreadPoolExecutor(max_workers=1) as pool:
            future = None if direct else pool.submit(self.case.stop)
            try:
                if not direct: self.assertTrue(reserved.wait(5))
                self.base.stop_ready.set(); self.base.save_ready.set()
                before = change()
            finally:
                release.set()
            if future is not None:
                with self.assertRaises(RegistryError) as caught: future.result(5)
                self.assertEqual('PROCESS_MISMATCH', caught.exception.code)
        self.case.join(session)
        self.assertEqual('RECOVERY_REQUIRED', self.f.state()['state'])
        self.assertEqual(1, self.f.state()['pending'])
        self.assertEqual(before, self.case.query('SELECT * FROM reservations ORDER BY operation,reference'))
        parent = self.case.query('SELECT * FROM runtime_operations WHERE id=?', (session['stop_operation'],))[0]
        self.assertNotEqual('COMPLETED', parent['status'])
        self.assertIn('managedSave', json.loads(parent['response']))
        self.assertFalse(self.runtime.game_processes[session['run']][0].closed)
        sent = self.base.channel.send_frame.call_count
        with self.assertRaises(RegistryError): self.case.stop()
        self.assertEqual(sent, self.base.channel.send_frame.call_count)
        self.assertEqual(['GRANT','ACK','BLOCKED','COMPLETION'], self.base.trace)
        self.assertEqual(1, len(self.f.adapter.created)); self.assertEqual(0, self.f.rcon_calls)

    def test_public_final_binding_change_preserves_pending_and_blocks_start(self):
        self.final_change()

    def test_direct_final_binding_change_preserves_pending_and_blocks_start(self):
        self.final_change(direct=True)

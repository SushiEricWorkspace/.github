"""認証済Mの元handle・生成snapshotを、終了後も保持する。"""
from copy import deepcopy
import json
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from .runtime_control import RuntimeControl
from .mod_control_runtime import process_identity
from .registry import RegistryError
from . import test_managed_save_evidence as saves


class GameProcessTests(unittest.TestCase):
    def setUp(self):
        self.runtime = RuntimeControl(Mock(), process_adapter=Mock())
        self.process = SimpleNamespace(pid=10001, group='owned-group', poll=Mock(return_value=0),
                                       members=Mock(return_value=[]), close=Mock())
        self.run = dict(runId='run-a', instanceId='slot-a', generationId='gen-a', artifactId='artifact-a', nonce='nonce-a',
            identity=dict(pid=10001, startId='start-a', startMillis=1234, bootId='boot-a', cwd='/fixture/world',
                          executable='/fixture/java', commandHash='a' * 64, owner='native-owner'),
            creation=dict(pid=10001, stage='RUNNING', ownerGroup='owned-group'))
        self.session = dict(context=dict(owner=dict(hostHash='b' * 64, bootId='boot-a', pid=10001, startId='start-a')),
                            raw=b'{"fixture":"authenticated descriptor bytes"}')
        self.runtime.processes['run-a'] = self.process
        self.runtime._record = Mock(return_value='next-record')
        self.runtime._transaction = MagicMock()

    def remember(self):
        self.runtime._remember_game_process(self.run, self.session)
        return self.runtime.game_processes['run-a'][1]

    def rejected(self, change, *, observe=False):
        raw = self.remember()
        change()
        with self.assertRaises(RegistryError):
            if observe:
                self.runtime._wait_exit(self.run, self.process, 10, expected='original')
            else:
                self.runtime._game_process(self.run)
        self.runtime._record.assert_not_called()
        self.runtime._transaction.assert_not_called()
        self.process.close.assert_not_called()
        self.assertEqual(raw, self.runtime.game_processes['run-a'][1])

    def test_snapshot_copies_are_independent_and_handle_is_original(self):
        raw = self.remember()
        process, snapshot = self.runtime._game_process(self.run)
        self.assertIs(self.process, process)
        snapshot['run']['identity']['pid'] = 99999
        snapshot['owner']['startId'] = 'other'
        self.session['context']['owner']['hostHash'] = 'c' * 64
        self.assertEqual(raw, self.runtime.game_processes['run-a'][1])
        self.assertEqual(10001, self.runtime._game_process(self.run)[1]['owner']['pid'])
        self.runtime.process_adapter.identity.assert_not_called()

    def test_mac_boot_uuid_case_preserves_original_owner_and_snapshot(self):
        lower = 'abcdefab-1234-4abc-8abc-abcdefabcdef'
        for original in (lower.upper(), 'AbCdEfAb-1234-4aBc-8AbC-aBcDeFaBcDeF'):
            with self.subTest(bootId=original):
                self.runtime.game_processes.clear()
                self.run['identity'].update(bootId=lower, startId='12:34')
                self.runtime.process_adapter.identity.return_value = deepcopy(self.run['identity'])
                def pidinfo(pid, flavor, arg, buffer, size):
                    struct.pack_into('QQ', buffer, 120, 12, 34)
                    return size
                native = SimpleNamespace(transport='unix-socket',
                    _host_boot=Mock(return_value=('b' * 64, original)),
                    proc=SimpleNamespace(proc_pidinfo=Mock(side_effect=pidinfo)))
                owner = process_identity(native, self.runtime.process_adapter, self.process.pid)
                self.session['context']['owner'] = owner
                before = deepcopy(self.run), deepcopy(self.session)
                with patch('dev_server.runtime_control.os', SimpleNamespace(name='posix')):
                    try:
                        self.remember()
                    except RegistryError as error:
                        self.fail('同一boot UUIDの表記差を拒否しました: ' + str(error))
                process, snapshot = self.runtime._game_process(self.run)
                self.assertIs(self.process, process)
                self.assertEqual(original, snapshot['owner']['bootId'])
                self.assertEqual(lower, snapshot['run']['identity']['bootId'])
                self.assertEqual(before, (self.run, self.session))
                self.runtime._record.assert_not_called(); self.runtime._transaction.assert_not_called()
                self.process.poll.assert_not_called(); self.process.close.assert_not_called()

    def test_mac_different_boot_uuid_is_rejected_before_effect(self):
        self.run['identity']['bootId'] = 'abcdefab-1234-4abc-8abc-abcdefabcdef'
        self.session['context']['owner']['bootId'] = 'ABCDEFAB-1234-4ABC-8ABC-ABCDEFABCDE0'
        before = deepcopy(self.run), deepcopy(self.session)
        with patch('dev_server.runtime_control.os', SimpleNamespace(name='posix')):
            with self.assertRaises(RegistryError) as caught: self.remember()
        self.assertEqual('PROCESS_MISMATCH', caught.exception.code)
        self.assertEqual({}, self.runtime.game_processes)
        self.assertEqual(before, (self.run, self.session))
        self.runtime._record.assert_not_called(); self.runtime._transaction.assert_not_called()
        self.process.poll.assert_not_called(); self.process.close.assert_not_called()

    def test_windows_boot_binding_remains_exact(self):
        self.session['context']['owner']['bootId'] = 'BOOT-A'
        with patch('dev_server.runtime_control.os', SimpleNamespace(name='nt')):
            with self.assertRaises(RegistryError) as caught: self.remember()
        self.assertEqual('PROCESS_MISMATCH', caught.exception.code)
        self.assertEqual({}, self.runtime.game_processes)
        self.runtime._record.assert_not_called(); self.runtime._transaction.assert_not_called()
        self.process.poll.assert_not_called(); self.process.close.assert_not_called()

    def test_duplicate_registration_does_not_replace_original(self):
        raw = self.remember()
        other = deepcopy(self.process)
        self.runtime.processes['run-a'] = other
        with self.assertRaises(RegistryError): self.runtime._remember_game_process(self.run, self.session)
        self.assertEqual(raw, self.runtime.game_processes['run-a'][1])
        self.assertIs(self.process, self.runtime.game_processes['run-a'][0])

    def test_different_handle_with_same_pid_and_group_is_rejected_before_effect(self):
        self.remember()
        other = SimpleNamespace(pid=self.process.pid, group=self.process.group, poll=Mock(return_value=0),
                                members=Mock(return_value=[]), close=Mock())
        with self.assertRaises(RegistryError): self.runtime._wait_exit(self.run, other, 10, expected='original')
        self.runtime._record.assert_not_called(); self.runtime._transaction.assert_not_called()
        other.poll.assert_not_called(); other.close.assert_not_called(); self.process.close.assert_not_called()

    def test_members_binding_change_is_rejected_before_exit_record(self):
        def change():
            self.process.members.side_effect = lambda: (setattr(self.process, 'group', 'other-group') or [])
        self.rejected(change, observe=True)

    def test_last_poll_binding_change_is_rejected_before_exit_record(self):
        def change():
            count = [0]
            def poll():
                count[0] += 1
                if count[0] == 2: self.process.group = 'other-group'
                return 0
            self.process.poll.side_effect = poll
        self.rejected(change, observe=True)

    def test_normal_exit_keeps_owned_handle_and_removes_only_live_reference(self):
        raw = self.remember()
        self.assertEqual('next-record', self.runtime._wait_exit(self.run, self.process, 10, expected='original'))
        self.assertEqual(0, self.run['exitCode'])
        self.assertTrue(self.run['jvmAndChildrenExited'])
        self.assertNotIn('run-a', self.runtime.processes)
        self.process.close.assert_not_called()
        self.assertEqual(raw, self.runtime.game_processes['run-a'][1])
        self.assertIs(self.process, self.runtime._game_process(self.run)[0])

    def test_legacy_exit_still_closes_handle(self):
        self.runtime._wait_exit(self.run, self.process, 10, expected='original')
        self.process.close.assert_called_once_with()
        self.assertEqual({}, self.runtime.game_processes)



    def test_timeout_keeps_original_snapshot_and_handle_without_exit_record(self):
        raw = self.remember()
        clock = [0.0]
        self.process.poll.return_value = None
        with patch('dev_server.runtime_control.time.monotonic', side_effect=lambda: clock[0]), \
                patch('dev_server.runtime_control.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, 2.0)):
            with self.assertRaises(RegistryError) as caught:
                self.runtime._wait_exit(self.run, self.process, 1, expected='original')
        self.assertEqual('STOP_TIMEOUT', caught.exception.code)
        self.runtime._record.assert_not_called(); self.runtime._transaction.assert_not_called()
        self.process.close.assert_not_called()
        self.assertIs(self.process, self.runtime.processes['run-a'])
        self.assertEqual(raw, self.runtime.game_processes['run-a'][1])


def invalid_registration(section, field):
    def test(self):
        value = self.session['context']['owner'] if section == 'owner' else self.run['creation']
        old = value[field]
        value[field] = old + 1 if type(old) is int else old + '-other'
        with self.assertRaises(RegistryError): self.runtime._remember_game_process(self.run, self.session)
        self.assertEqual({}, self.runtime.game_processes)
        self.assertIs(self.process, self.runtime.processes['run-a'])
        self.runtime._record.assert_not_called(); self.runtime._transaction.assert_not_called()
        self.process.close.assert_not_called()
    return test


for section, fields in (('owner', ('pid','startId','bootId')), ('creation', ('pid','stage','ownerGroup'))):
    for field in fields:
        setattr(GameProcessTests, 'test_registration_' + section + '_' + field + '_mismatch', invalid_registration(section, field))


def changed_run(section, field):
    def test(self):
        def change():
            value = self.run if section is None else self.run[section]
            old = value[field]
            value[field] = old + 1 if type(old) is int else old + '-other'
        self.rejected(change, observe=True)
    return test


for section, fields in ((None, ('runId','instanceId','generationId','artifactId','nonce')),
        ('identity', ('pid','startId','startMillis','bootId','cwd','executable','commandHash','owner')),
        ('creation', ('pid','stage','ownerGroup'))):
    for field in fields:
        setattr(GameProcessTests, 'test_run_' + (section or 'binding') + '_' + field + '_mismatch', changed_run(section, field))


def changed_process(field):
    def test(self):
        self.rejected(lambda: setattr(self.process, field, 99999 if field == 'pid' else 'other-group'), observe=True)
    return test


for field in ('pid', 'group'):
    setattr(GameProcessTests, 'test_held_' + field + '_mismatch', changed_process(field))


class GameProcessRegistryTests(unittest.TestCase):
    def setUp(self):
        self.case = saves.SaveEvidenceRegistryTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.runtime, self.f, self.managed = self.case.runtime, self.case.f, self.case.managed

    def run_record(self, session):
        return json.loads(self.case.query('SELECT record FROM runs WHERE id=?', (session['run'],))[0]['record'])

    def retained(self, session):
        self.assertIn(session['run'], self.runtime.game_processes)
        process, snapshot = self.runtime._game_process(self.run_record(session))
        self.assertFalse(process.closed)
        self.assertNotIn(session['run'], self.runtime.processes)
        self.assertEqual(session['context']['owner'], snapshot['owner'])
        self.assertEqual(session['run'], snapshot['run']['runId'])
        self.assertEqual(session['save_evidence']['descriptorSha256'], snapshot['descriptorSha256'])
        self.assertNotIn(self.case.instance, self.managed.sessions)
        self.assertNotIn(self.case.instance, self.managed.startup.sessions)
        self.assertEqual(['GRANT','ACK','BLOCKED','COMPLETION'], self.case.base.trace)
        self.assertEqual(0, self.f.rcon_calls)
        return process, snapshot

    def test_direct_stop_retains_original_handle_after_control_release(self):
        session = self.case.base.start()
        original = self.runtime.processes[session['run']]
        self.case.base.stop_ready.set(); self.case.base.save_ready.set(); self.case.join(session)
        self.assertIsNone(session['error'])
        process, snapshot = self.retained(session)
        self.assertIs(original, process)
        self.assertEqual('LEASED_STOPPED', self.f.state()['state'])
        self.assertEqual(1, len(self.f.adapter.created))
        with patch.object(self.runtime.process_adapter, 'identity', side_effect=AssertionError('旧PIDを再照会しません')):
            self.assertIs(original, self.runtime._game_process(self.run_record(session))[0])

    def test_public_stop_and_competing_start_keep_original_handle_and_reservations(self):
        self.case.public_stop()
        self.retained(self.case.base.session)
        self.assertEqual(1, len(self.f.adapter.created))

    def test_post_commit_timeout_retains_handle_and_allows_independent_next_run(self):
        self.case.final_boundary(after_write=True, restart=True)
        self.assertEqual(2, len(self.runtime.game_processes))
        self.assertEqual(2, len(self.f.adapter.created))
        for run_id, (process, raw) in self.runtime.game_processes.items():
            snapshot = json.loads(raw)
            self.assertEqual(run_id, snapshot['run']['runId'])
            self.assertEqual(process.pid, snapshot['run']['creation']['pid'])
            self.assertFalse(process.closed)
        self.assertEqual(2, len({p.pid for p, _ in self.runtime.game_processes.values()}))

    def test_pre_commit_failure_preserves_original_handle_without_replay(self):
        self.case.final_boundary()
        session = self.case.base.session
        process, _ = self.runtime._game_process(self.run_record(session))
        self.assertFalse(process.closed)
        self.assertEqual('RECOVERY_REQUIRED', self.f.state()['state'])
        self.assertTrue(self.case.query('SELECT * FROM reservations'))
        before = self.case.base.channel.send_frame.call_count
        with self.assertRaises(RegistryError): self.case.stop()
        self.assertEqual(before, self.case.base.channel.send_frame.call_count)
        self.assertEqual(1, len(self.f.adapter.created))

    def test_native_binding_changes_during_exit_preserve_pending_and_original_snapshot(self):
        session = self.case.base.start()
        process, snapshot = self.runtime._game_process(self.run_record(session))
        raw = self.runtime.game_processes[session['run']][1]
        original = process.members
        def members():
            values = original()
            if process.poll() == 0: process.group = 'other-group'
            return values
        process.members = members
        with patch.object(self.runtime, '_record', wraps=self.runtime._record) as record:
            self.case.base.stop_ready.set(); self.case.base.save_ready.set(); self.case.join(session)
            record.assert_not_called()
        self.assertIsInstance(session['error'], RegistryError)
        self.assertEqual('PROCESS_MISMATCH', session['error'].code)
        self.assertFalse(process.closed)
        self.assertEqual(raw, self.runtime.game_processes[session['run']][1])
        self.assertIs(process, self.runtime.processes[session['run']])
        self.assertEqual('RECOVERY_REQUIRED', self.f.state()['state'])
        self.assertEqual(1, self.f.state()['pending'])
        self.assertTrue(self.case.query('SELECT * FROM reservations'))
        self.assertEqual(1, len(self.f.adapter.created))
        self.assertEqual(['GRANT','ACK','BLOCKED','COMPLETION'], self.case.base.trace)

    def test_nonzero_exit_preserves_handle_and_intent_without_success_or_retry(self):
        session = self.case.base.start()
        process = self.runtime.processes[session['run']]
        original = self.case.base.exit
        def exit(deadline):
            value = original(deadline)
            process.exit_code = 1
            return value
        self.case.base.channel.wait_peer_close.side_effect = exit
        self.case.base.stop_ready.set(); self.case.base.save_ready.set(); self.case.join(session)
        self.assertIsInstance(session['error'], RegistryError)
        self.assertEqual('SAVE_FAILED', session['error'].code)
        self.assertFalse(process.closed)
        self.assertIs(process, self.runtime.game_processes[session['run']][0])
        self.assertEqual('RECOVERY_REQUIRED', self.f.state()['state'])
        self.assertEqual(1, self.f.state()['pending'])
        self.assertIsNone(self.f.state()['runtime_proof'])
        self.assertTrue(self.case.query('SELECT * FROM reservations'))
        self.assertEqual(['GRANT','ACK','BLOCKED','COMPLETION'], self.case.base.trace)
        self.assertEqual(1, len(self.f.adapter.created))

"""認証済保存原文の停止正本接続。Data保存/hash算法を合成しない。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from . import mod_control_operations as operations
from . import mod_control_runtime as target
from . import test_mod_control_runtime as fixtures
from .registry import RegistryError


class SaveEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.ManagedRuntimeTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.f = self.base.running()
        self.managed, self.session = self.base.managed, self.base.session
        self.f.channel.send_frame.reset_mock()
        self.requests = self.base.stop_frames()
        self.f.channel.receive_frame.side_effect = iter(self.requests)
        self.f.channel.send_frame.side_effect = lambda raw, deadline: len(raw)
        self.f.endpoint.close.return_value = True
        self.f.runtime._direct_stop = Mock()

    def serve(self):
        self.managed._serve('slot-a', self.session)

    def evidence(self):
        with self.f.transaction(read_only=True) as connection:
            return self.managed.save_evidence(connection, 'slot-a', self.session, time.monotonic() + 10)

    def parent(self):
        return dict(self.f.db.execute('SELECT * FROM runtime_operations WHERE id=?', (self.session['stop_operation'],)).fetchone())

    def unchanged_effects(self):
        return self.f.channel.send_frame.call_count, self.f.runtime._direct_stop.call_count, self.f.db.total_changes

    def test_nonempty_original_frames_and_hash_references_are_preserved(self):
        value = fixtures.wire.decode_frame_v3(self.requests[1])
        self.assertTrue(json.loads(value['payload']['requestJson'])['status']['state']['payload'])
        body = json.dumps(value, ensure_ascii=False, indent=2).encode()
        self.requests[1] = len(body).to_bytes(4, 'big') + body
        self.serve()
        self.assertIsNone(self.session['error'])
        self.assertTrue(self.session['done'].is_set())
        self.assertEqual(self.requests[1], self.session['save_request'])
        self.assertEqual(self.f.channel.send_frame.call_args.args[0], self.session['completion_response'])
        evidence, pending = self.evidence()
        self.assertIn('managedSave', pending)
        self.assertEqual(evidence, pending['managedSave'])
        self.assertEqual(fixtures.wire.sha256(body), evidence['requestBodySha256'])
        self.assertEqual(fixtures.wire.sha256(self.session['completion_response'][4:]), evidence['responseBodySha256'])
        self.assertEqual('PENDING', self.parent()['status'])
        self.assertNotIn('ready', evidence)
        self.assertNotIn('gameOwner', evidence)

    def rejected(self, change):
        self.serve()
        self.assertIsNone(self.session['error'])
        change()
        before = self.unchanged_effects()
        with self.assertRaises((RegistryError, ValueError)):
            self.evidence()
        self.assertEqual(before, self.unchanged_effects())

    def replace_requested(self, key, value):
        message = fixtures.wire.decode_frame_v3(self.session['save_request'])
        requested = json.loads(message['payload']['requestJson'])
        state = requested['status']['state']
        if key in ('owner', 'management'):
            state['payload'][key] = value
        else:
            state[key] = value
        raw = json.dumps(requested, separators=(',', ':'))
        message['payload'].update(requestJson=raw, requestSha256=fixtures.wire.sha256(raw.encode()))
        request = fixtures.frame(message)
        response = fixtures.wire.decode_frame_v3(self.session['completion_response'])
        response['payload'].update(requestBodySha256=fixtures.wire.sha256(request[4:]), requestSha256=message['payload']['requestSha256'])
        response['payload']['completion']['request'] = requested
        reply = fixtures.frame(response)
        self.session.update(save_request=request, completion_response=reply)
        record = dict(requestBodyUtf8=request[4:].decode(), responseBodyUtf8=reply[4:].decode())
        self.f.db.execute('UPDATE runtime_operations SET fingerprint=?,response=? WHERE id=?',
                          (fixtures.wire.sha256(request[4:]), json.dumps(record), message['operationId']))
        self.f.db.commit()

    def test_completion_loss_preserves_original_without_retry(self):
        def send(raw, deadline):
            if fixtures.wire.decode_frame_v3(raw)['phase'] == 'COMPLETION':
                raise EOFError('作用後の応答喪失')
            return len(raw)
        self.f.channel.send_frame.side_effect = send
        self.serve()
        self.assertIsInstance(self.session['error'], EOFError)
        self.assertFalse(self.session['done'].is_set())
        self.assertEqual(self.requests[1], self.session['save_request'])
        self.assertTrue(self.session['completion_response'])
        self.assertNotIn('managedSave', json.loads(self.parent()['response']))
        self.assertEqual('PENDING', self.parent()['status'])
        before = self.f.channel.send_frame.call_count
        self.f.channel.receive_frame.side_effect = iter(self.requests)
        self.serve()
        self.assertEqual(before, self.f.channel.send_frame.call_count)
        self.f.runtime._direct_stop.assert_not_called()

    def timeout(self, *, after_record=False, after_verify=False):
        clock = [0.0]
        self.enterContext(patch.object(target.time, 'monotonic', lambda: clock[0]))
        if after_record:
            self.f.db.set_trace_callback(lambda sql: clock.__setitem__(0, 31.0)
                                         if sql.startswith('UPDATE runtime_operations SET response=') and 'managedSave' in sql else None)
            self.addCleanup(self.f.db.set_trace_callback, None)
        elif after_verify:
            original = self.managed.save_evidence
            def verify(*args):
                value = original(*args)
                clock[0] = 31.0
                return value
            self.managed.save_evidence = verify
        else:
            self.f.endpoint.close.side_effect = lambda deadline: (clock.__setitem__(0, 31.0) or True)
        self.serve()
        self.assertIsInstance(self.session['error'], RegistryError)
        self.assertEqual('STOP_TIMEOUT', self.session['error'].code)
        self.assertFalse(self.session['done'].is_set())
        self.f.runtime._direct_stop.assert_not_called()
        self.assertEqual(after_record, 'managedSave' in json.loads(self.parent()['response']))
        self.assertEqual('PENDING', self.parent()['status'])
        self.assertEqual(2, self.f.channel.send_frame.call_count)
        self.assertEqual('BLOCKED', self.f.gate()['gate'])

    def test_close_crosses_deadline_before_record(self): self.timeout()
    def test_last_verification_crosses_deadline_before_record(self): self.timeout(after_verify=True)
    def test_record_crosses_deadline_without_done(self): self.timeout(after_record=True)


    def test_latest_writer_added_after_completion_is_rejected(self):
        self.rejected(lambda: self.f.writer_session(status='UNKNOWN', pending=0))

    def test_new_accepted_call_after_completion_is_rejected(self):
        def change():
            row = self.f.row()
            self.f.db.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',
                              ('another-call', 'writer-call:new', 'slot-a', row['lease'], row['epoch']))
            self.f.db.commit()
        self.rejected(change)

    def test_source_record_write_failure_keeps_original_frames_and_reservation(self):
        import sqlite3
        def close(deadline):
            self.f.db.set_authorizer(lambda action, a, b, db, source:
                                     sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE and a == 'runtime_operations'
                                     else sqlite3.SQLITE_OK)
            return True
        self.f.endpoint.close.side_effect = close
        self.addCleanup(self.f.db.set_authorizer, None)
        self.serve()
        self.assertIsInstance(self.session['error'], sqlite3.DatabaseError)
        self.assertFalse(self.session['done'].is_set())
        self.assertEqual(self.requests[1], self.session['save_request'])
        self.assertTrue(self.session['completion_response'])
        self.assertEqual('PENDING', self.parent()['status'])
        self.assertNotIn('managedSave', json.loads(self.parent()['response']))
        self.f.runtime._direct_stop.assert_not_called()
        self.assertEqual(2, self.f.channel.send_frame.call_count)


    def test_completion_writer_set_with_matching_readback_is_rejected(self):
        def change():
            response = fixtures.wire.decode_frame_v3(self.session['completion_response'])
            response['payload']['completion']['writers'] = [dict(identity=dict(self.f.context['owner'], pid=300), state='CLOSED', children=[])]
            reply = fixtures.frame(response)
            request = self.session['save_request']
            self.session['completion_response'] = reply
            body = dict(requestBodyUtf8=request[4:].decode(), responseBodyUtf8=reply[4:].decode())
            operation = fixtures.wire.decode_frame_v3(request)['operationId']
            self.f.db.execute('UPDATE runtime_operations SET response=? WHERE id=?', (json.dumps(body), operation))
            self.f.db.commit()
        self.rejected(change)

    def test_expired_evidence_rejects_without_write(self):
        self.serve()
        before = self.unchanged_effects()
        with patch.object(target.time, 'monotonic', return_value=100):
            with self.f.transaction(read_only=True) as connection:
                with self.assertRaises(RegistryError) as caught:
                    self.managed.save_evidence(connection, 'slot-a', self.session, 99)
        self.assertEqual('STOP_TIMEOUT', caught.exception.code)
        self.assertEqual(before, self.unchanged_effects())


    def test_original_stop_deadline_is_kept_after_elapsed_save_phase(self):
        clock = [0.0]
        self.enterContext(patch.object(target.time, 'monotonic', lambda: clock[0]))
        requests = iter(self.requests)
        def receive(deadline, **kwargs):
            value = next(requests)
            if fixtures.wire.decode_frame_v3(value)['phase'] == 'SAVE_REQUESTED':
                self.assertEqual(30.0, deadline)
                clock[0] = 20.0
            return value
        def send(raw, deadline):
            if fixtures.wire.decode_frame_v3(raw)['phase'] == 'BLOCKED': clock[0] = 10.0
            return len(raw)
        original = self.managed.save_evidence
        def verify(*args):
            value = original(*args)
            clock[0] = 31.0
            return value
        self.f.channel.receive_frame.side_effect = receive
        self.f.channel.send_frame.side_effect = send
        self.managed.save_evidence = verify
        self.serve()
        self.assertIsInstance(self.session['error'], RegistryError)
        self.assertEqual('STOP_TIMEOUT', self.session['error'].code)
        self.assertEqual(30.0, self.session['stop_deadline'])
        self.assertFalse(self.session['done'].is_set())
        self.assertNotIn('managedSave', json.loads(self.parent()['response']))
        self.f.runtime._direct_stop.assert_not_called()

    def test_parent_run_mismatch_with_other_fields_preserved(self):
        def change():
            body = json.loads(self.parent()['response'])
            body['runId'] = 'another-run'
            self.f.db.execute('UPDATE runtime_operations SET response=? WHERE id=?', (json.dumps(body), self.session['stop_operation']))
            self.f.db.commit()
        self.rejected(change)

    def test_parent_control_mismatch_with_other_fields_preserved(self):
        def change():
            body = json.loads(self.parent()['response'])
            body['controlOperationId'] = 'another-control'
            self.f.db.execute('UPDATE runtime_operations SET response=? WHERE id=?', (json.dumps(body), self.session['stop_operation']))
            self.f.db.commit()
        self.rejected(change)

    def test_parent_fingerprint_mismatch_preserves_original_request(self):
        def change():
            self.f.db.execute("UPDATE runtime_operations SET fingerprint=? WHERE command='stop'", ('0' * 64,))
            self.f.db.commit()
        self.rejected(change)


def state_case(key, value):
    def test(self):
        self.rejected(lambda: self.replace_requested(key, value(self) if callable(value) else value))
    return test


for key, value in (
    ('worldUuid', '00000000-0000-4000-8000-000000000099'),
    ('runUuid', '00000000-0000-4000-8000-000000000098'),
    ('generation', 'another-generation'),
    ('paths', lambda s: dict(json.loads(fixtures.wire.decode_frame_v3(s.requests[1])['payload']['requestJson'])['status']['state']['paths'], worldRoot='/another/world')),
    ('stateRevision', lambda s: fixtures.wire._inner(fixtures.wire.decode_frame_v3(s.f.frames[1][0])['payload'], 'statusJson', 'statusJsonSha256')['state']['stateRevision']),
    ('owner', lambda s: dict(s.f.context['owner'], startId='another-start')),
    ('management', lambda s: dict(s.f.context['management'], drainSha256='1' * 64)),
):
    setattr(SaveEvidenceTests, 'test_requested_' + key + '_mismatch_with_matching_outer_hashes', state_case(key, value))


def row_case(sql, params):
    def test(self):
        def change():
            self.f.db.execute(sql, params)
            self.f.db.commit()
        self.rejected(change)
    return test


for name, sql, params in (
    ('lease', "UPDATE instances SET lease='other'", ()),
    ('epoch', 'UPDATE instances SET epoch=epoch+1', ()),
    ('generation', "UPDATE instances SET generation='other'", ()),
    ('run', "UPDATE instances SET run_id='other'", ()),
    ('pending', 'UPDATE instances SET pending=0', ()),
    ('writers', 'UPDATE instances SET writers=1', ()),
    ('children', 'UPDATE instances SET children=1', ()),
    ('gate', "UPDATE run_admission SET gate='OPEN'", ()),
    ('revision', 'UPDATE run_admission SET revision=revision+1', ()),
    ('descriptor', "UPDATE run_admission SET descriptor_hash=?", ('1' * 64,)),
    ('parent_status', "UPDATE runtime_operations SET status='COMPLETED' WHERE command='stop'", ()),
    ('parent_body', "UPDATE runtime_operations SET response='{}' WHERE command='stop'", ()),
    ('source_status', "UPDATE runtime_operations SET status='RECOVERY_REQUIRED' WHERE command='mod-control/SAVE_REQUESTED'", ()),
    ('reservation', "DELETE FROM reservations WHERE reference LIKE 'stop:%'", ()),
    ('reservation_epoch', "UPDATE reservations SET epoch=epoch+1 WHERE reference LIKE 'stop:%'", ()),
):
    setattr(SaveEvidenceTests, 'test_' + name + '_mismatch_has_no_additional_effect', row_case(sql, params))


def actor_case(kind, field):
    def test(self):
        def change():
            value = deepcopy(self.f.context['owner' if kind == 'owner' else 'management'])
            actor = value['controller'] if kind == 'controller' else value
            old = actor[field]
            actor[field] = old + 1 if type(old) is int else ('0' * 64 if field == 'hostHash' and old != '0' * 64 else old + '-other')
            self.replace_requested('owner' if kind == 'owner' else 'management', value)
        self.rejected(change)
    return test


for kind, fields in (('owner', ('hostHash','bootId','pid')),
                     ('management', ('leaseId','epoch','generation')),
                     ('controller', ('hostHash','bootId','pid','startId'))):
    for field in fields:
        setattr(SaveEvidenceTests, 'test_requested_' + kind + '_' + field + '_mismatch', actor_case(kind, field))
setattr(SaveEvidenceTests, 'test_requested_shop_path_mismatch', state_case('paths',
        lambda s: dict(json.loads(fixtures.wire.decode_frame_v3(s.requests[1])['payload']['requestJson'])['status']['state']['paths'], shopDirectory='/another/shop')))


class SaveEvidenceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.ManagedRuntimeIntegrationTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.f = self.base.f
        self.runtime, self.managed = self.base.runtime, self.base.managed
        self.instance = 'persistent-01'

    def join(self, session):
        session['thread'].join(5)
        self.assertFalse(session['thread'].is_alive())

    def query(self, sql, args=()):
        with self.runtime._lock_wait(time.monotonic() + 5), self.runtime._transaction(self.instance, read_only=True) as c:
            return [dict(row) for row in c.execute(sql, args)]

    def stop(self):
        return self.f.command('stop', dict(self.f.token, timeoutSeconds=10), 'public-stop')

    def test_direct_stop_carries_original_authenticated_hashes_into_result(self):
        session = self.base.start()
        self.base.stop_ready.set(); self.base.save_ready.set()
        self.join(session)
        self.assertIsNone(session['error'])
        parent = self.query('SELECT * FROM runtime_operations WHERE id=?', (session['stop_operation'],))[0]
        self.assertEqual('COMPLETED', parent['status'])
        result = json.loads(parent['response'])
        self.assertEqual(session['save_evidence'], result['managedSave'])
        self.assertEqual(fixtures.wire.sha256(self.base.requests[3][4:]), result['managedSave']['requestBodySha256'])
        self.assertEqual(1, len(self.f.adapter.created))
        self.assertEqual(0, self.f.rcon_calls)
        self.assertEqual([], self.query('SELECT * FROM reservations'))

    def public_stop(self, *, mutate=False):
        session = self.base.start()
        entered, release = threading.Event(), threading.Event()
        reserved, proceed = threading.Event(), threading.Event()
        original = self.runtime._stop
        def stop(*args):
            reserved.set()
            if not proceed.wait(5): raise TimeoutError('試験停止予約')
            result = original(*args)
            if mutate:
                entered.set()
                if not release.wait(5): raise TimeoutError('試験最終照合')
            return result
        with patch.object(self.runtime, '_stop', side_effect=stop), ThreadPoolExecutor(max_workers=1) as pool:
            public = pool.submit(self.stop)
            try:
                self.assertTrue(reserved.wait(5))
                if mutate:
                    self.base.stop_ready.set(); self.base.save_ready.set()
                    proceed.set()
                    self.assertTrue(entered.wait(5))
                before = self.query('SELECT * FROM reservations ORDER BY operation,reference')
                self.assertEqual(1, self.f.state()['pending'])
                with self.assertRaises(RegistryError): self.f.start('competing-start')
                self.assertEqual(before, self.query('SELECT * FROM reservations ORDER BY operation,reference'))
                self.assertEqual(1, len(self.f.adapter.created))
                if mutate:
                    with self.runtime._transaction(self.instance) as c:
                        record = json.loads(c.execute('SELECT response FROM runtime_operations WHERE id=?', ('public-stop',)).fetchone()[0])
                        record['managedSave']['requestBodySha256'] = '0' * 64
                        c.execute('UPDATE runtime_operations SET response=? WHERE id=?', (json.dumps(record), 'public-stop'))
                else:
                    self.base.stop_ready.set(); self.base.save_ready.set()
            finally:
                proceed.set(); release.set()
            if mutate:
                with self.assertRaises(RegistryError) as caught: public.result(5)
                self.assertEqual('STALE_OPERATION', caught.exception.code)
                self.assertEqual('RECOVERY_REQUIRED', self.f.state()['state'])
                self.assertEqual(1, self.f.state()['pending'])
                self.assertEqual(before, self.query('SELECT * FROM reservations ORDER BY operation,reference'))
            else:
                result = public.result(5)
                self.assertEqual(session['save_evidence'], result['managedSave'])
                count = self.base.channel.send_frame.call_count
                self.assertTrue(self.stop()['replayed'])
                self.assertEqual(count, self.base.channel.send_frame.call_count)
        self.join(session)
        self.assertEqual(1, len(self.f.adapter.created))
        self.assertEqual(['GRANT','ACK','BLOCKED','COMPLETION'], self.base.trace)

    def test_public_stop_reservation_blocks_competing_start_without_new_generation(self):
        self.public_stop()

    def test_public_final_transaction_rejects_changed_source_record(self):
        self.public_stop(mutate=True)

    def test_direct_stop_before_source_record_blocks_competing_start(self):
        session = self.base.start()
        entered, release = threading.Event(), threading.Event()
        original = self.base.exit
        def exit(deadline):
            entered.set()
            if not release.wait(5): raise TimeoutError('試験COMPLETION close')
            return original(deadline)
        self.base.channel.wait_peer_close.side_effect = exit
        self.base.stop_ready.set(); self.base.save_ready.set()
        try:
            self.assertTrue(entered.wait(5))
            before = self.query('SELECT * FROM reservations ORDER BY operation,reference')
            self.assertEqual(1, self.f.state()['pending'])
            with self.assertRaises(RegistryError): self.f.start('competing-start')
            self.assertEqual(before, self.query('SELECT * FROM reservations ORDER BY operation,reference'))
        finally:
            release.set()
        self.join(session)
        self.assertIsNone(session['error'])
        self.assertEqual(1, len(self.f.adapter.created))
        self.assertEqual('LEASED_STOPPED', self.f.state()['state'])

    def final_boundary(self, *, direct=False, after_write=False, after_release=False, restart=False):
        session = self.base.start()
        original = self.managed.save_evidence
        native_clock = time.monotonic
        clock = [None]
        last_deadline = [0]
        original_release = self.managed.release
        def release(*args):
            original_release(*args)
            if after_release: clock[0] = last_deadline[0] + 1
        def now(): return native_clock() if clock[0] is None else clock[0]
        def evidence(connection, instance, held, deadline):
            value = original(connection, instance, held, deadline)
            if held['done'].is_set():
                last_deadline[0] = deadline
                if after_release:
                    pass
                elif after_write:
                    connection.set_trace_callback(lambda sql: clock.__setitem__(0, deadline + 1)
                        if sql.startswith("UPDATE runtime_operations SET status='COMPLETED'") else None)
                else:
                    clock[0] = deadline + 1
            return value
        with patch.object(target.time, 'monotonic', side_effect=now), \
                patch.object(self.managed, 'save_evidence', side_effect=evidence), \
                patch.object(self.managed, 'release', side_effect=release):
            if direct:
                self.base.stop_ready.set(); self.base.save_ready.set()
                self.join(session)
                self.assertIsInstance(session['error'], RegistryError)
                self.assertEqual('STOP_TIMEOUT', session['error'].code)
                parent_id = session['stop_operation']
            else:
                reserved = threading.Event()
                original_stop = self.runtime._stop
                def stop(*args):
                    reserved.set()
                    return original_stop(*args)
                with patch.object(self.runtime, '_stop', side_effect=stop), ThreadPoolExecutor(max_workers=1) as pool:
                    public = pool.submit(self.stop)
                    self.assertTrue(reserved.wait(5))
                    self.base.stop_ready.set(); self.base.save_ready.set()
                    with self.assertRaises(RegistryError) as caught: public.result(5)
                    self.assertEqual('STOP_TIMEOUT', caught.exception.code)
                self.join(session)
                parent_id = 'public-stop'
        parent = self.query('SELECT * FROM runtime_operations WHERE id=?', (parent_id,))[0]
        recorded = after_write or after_release
        self.assertEqual('COMPLETED' if recorded else ('PENDING' if direct else 'RECOVERY_REQUIRED'), parent['status'])
        saved = json.loads(parent['response'])
        if after_write:
            self.assertNotIn(self.instance, self.managed.sessions)
            self.assertNotIn(self.instance, self.managed.startup.sessions)
        self.assertIn('managedSave', saved)
        self.assertEqual(session['save_evidence'], saved['managedSave'])
        self.assertEqual(recorded, not bool(self.query('SELECT * FROM reservations')))
        self.assertEqual(1, len(self.f.adapter.created))
        self.assertEqual(['GRANT','ACK','BLOCKED','COMPLETION'], self.base.trace)
        if not recorded:
            with self.assertRaises(RegistryError): self.f.start('failed-boundary-start')
            self.assertEqual(1, len(self.f.adapter.created))

        if restart:
            self.assertTrue(self.stop()['replayed'])
            self.assertEqual([], self.query('SELECT * FROM reservations'))
            self.base.stop_ready.clear(); self.base.save_ready.clear(); self.base.blocked.clear()
            self.base.index = 0; self.base.requests.clear(); self.base.trace.clear()
            accept = self.base.accept
            def next_accept(*args):
                channel = accept(*args)
                for i, raw in enumerate(self.base.requests):
                    message = fixtures.wire.decode_frame_v3(raw)
                    message['operationId'] = 'next-' + message['operationId']
                    self.base.requests[i] = fixtures.frame(message)
                return channel
            with patch.object(self.base.endpoint, 'accept', side_effect=next_accept):
                self.f.start('next-after-timeout')
            next_session = self.managed.sessions[self.instance]
            next_process = self.f.adapter.created[-1]
            def finish_next():
                def exited(deadline):
                    next_process.stopped_proof()
                    next_process.exit_code = 0
                    return True
                self.base.channel.wait_peer_close.side_effect = exited
                self.base.stop_ready.set(); self.base.save_ready.set()
                self.join(next_session)
                self.assertIsNone(next_session['error'])
                self.assertFalse(next_process.closed)
                self.assertIs(next_process, self.runtime.game_processes[next_session['run']][0])
                self.assertNotIn(self.instance, self.managed.sessions)
                self.assertNotIn(self.instance, self.managed.startup.sessions)
            self.addCleanup(finish_next)
            self.assertEqual(2, len(self.f.adapter.created))
            self.assertEqual('RUNNING', self.f.state()['state'])
            self.assertEqual(0, self.f.state()['pending'])
            self.assertNotEqual(session['run'], self.f.state()['run_id'])
            self.assertTrue(all(r['run'] == self.f.state()['run_id'] for r in self.query('SELECT * FROM endpoint_reservations')))
            self.assertEqual(parent, self.query('SELECT * FROM runtime_operations WHERE id=?', (parent_id,))[0])
            finish_next()

    def test_public_commit_timeout_replay_allows_next_start_without_old_slot(self):
        self.final_boundary(after_write=True, restart=True)

    def test_public_final_verification_crosses_deadline_without_success_record(self):
        self.final_boundary()

    def test_direct_final_verification_crosses_deadline_without_success_record(self):
        self.final_boundary(direct=True)

    def test_public_result_commit_crosses_deadline_keeps_record_without_return(self):
        self.final_boundary(after_write=True)

    def test_direct_result_commit_crosses_deadline_keeps_record_without_return(self):
        self.final_boundary(direct=True, after_write=True)

    def test_direct_final_transaction_rejects_changed_source_record(self):
        session = self.base.start()
        entered, release = threading.Event(), threading.Event()
        original = self.runtime._stop
        def stop(*args):
            result = original(*args)
            entered.set()
            if not release.wait(5): raise TimeoutError('試験直接stop最終照合')
            return result
        with patch.object(self.runtime, '_stop', side_effect=stop):
            self.base.stop_ready.set(); self.base.save_ready.set()
            try:
                self.assertTrue(entered.wait(5))
                before = self.query('SELECT * FROM reservations ORDER BY operation,reference')
                with self.runtime._transaction(self.instance) as connection:
                    parent_id = session['stop_operation']
                    pending = json.loads(connection.execute('SELECT response FROM runtime_operations WHERE id=?', (parent_id,)).fetchone()[0])
                    pending['managedSave']['responseBodySha256'] = '0' * 64
                    connection.execute('UPDATE runtime_operations SET response=? WHERE id=?', (json.dumps(pending), parent_id))
                with self.assertRaises(RegistryError): self.f.start('competing-start')
            finally:
                release.set()
            self.join(session)
        self.assertIsInstance(session['error'], RegistryError)
        self.assertEqual('STALE_OPERATION', session['error'].code)
        self.assertEqual('RECOVERY_REQUIRED', self.f.state()['state'])
        self.assertEqual(1, self.f.state()['pending'])
        self.assertEqual(before, self.query('SELECT * FROM reservations ORDER BY operation,reference'))
        self.assertEqual(1, len(self.f.adapter.created))

    def test_public_release_crosses_deadline_without_normal_return(self):
        self.final_boundary(after_release=True)

    def test_direct_release_crosses_deadline_without_normal_return(self):
        self.final_boundary(direct=True, after_release=True)

    def test_direct_stop_keeps_original_deadline_after_elapsed_save_phase(self):
        session = self.base.start()
        clock = [0.0]
        original_exit = self.base.exit
        original_evidence = self.managed.save_evidence
        def exit(deadline):
            clock[0] = 20.0
            return original_exit(deadline)
        def evidence(*args):
            value = original_evidence(*args)
            if session['done'].is_set(): clock[0] = 31.0
            return value
        self.base.channel.wait_peer_close.side_effect = exit
        with patch.object(target.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.managed, 'save_evidence', side_effect=evidence):
            self.base.stop_ready.set(); self.base.save_ready.set()
            self.join(session)
        self.assertEqual(30.0, session['stop_deadline'])
        self.assertIsInstance(session['error'], RegistryError)
        self.assertEqual('STOP_TIMEOUT', session['error'].code)
        parent = self.query('SELECT * FROM runtime_operations WHERE id=?', (session['stop_operation'],))[0]
        self.assertEqual('PENDING', parent['status'])
        self.assertEqual(session['save_evidence'], json.loads(parent['response'])['managedSave'])
        self.assertTrue(self.query('SELECT * FROM reservations'))
        self.assertEqual(1, len(self.f.adapter.created))

    def test_late_observer_of_completed_stop_has_no_effect_even_after_deadline(self):
        session = self.base.start()
        self.base.stop_ready.set(); self.base.save_ready.set()
        self.join(session)
        self.assertIsNone(session['error'])
        before = self.query('SELECT * FROM runtime_operations ORDER BY id')
        sends, created = self.base.channel.send_frame.call_count, len(self.f.adapter.created)
        with patch.object(target.time, 'monotonic', return_value=session['stop_deadline'] + 1):
            try:
                self.runtime._direct_stop(self.instance, session)
            except RegistryError as error:
                self.fail('既確定stopへの遅延観測で新作用/誤拒否しません: ' + error.code)
        self.assertEqual(before, self.query('SELECT * FROM runtime_operations ORDER BY id'))
        self.assertEqual(sends, self.base.channel.send_frame.call_count)
        self.assertEqual(created, len(self.f.adapter.created))
        self.assertEqual('LEASED_STOPPED', self.f.state()['state'])


    def join_boundary(self, phase, *, source_deadline=False):
        session = self.base.start()
        paused, proceed, parent_done = threading.Event(), threading.Event(), threading.Event()
        clock = [0.0]
        public_thread = [None]
        parent_snapshot = []
        depths = threading.local()
        original_stop = self.runtime._stop
        original_join = self.runtime._join_direct_stop
        original_instance = self.f.registry._instance
        original_wait = self.runtime._lock_wait
        original_transaction = self.f.registry.transaction
        limit = 30.0 if source_deadline else 15.0

        def stop(*args):
            paused.set()
            if not proceed.wait(5): raise TimeoutError('直接stopを合流予約まで保持')
            return original_stop(*args)

        def join(*args):
            public_thread[0] = threading.get_ident()
            clock[0] = 16.0 if phase == 'entry' else 10.0
            return original_join(*args)

        def trace(sql):
            if phase == 'admission' and sql.startswith('SELECT 1 FROM snapshot_operations WHERE id=') and 'public-join' in sql:
                clock[0] = limit + 1
            if phase == 'reservation' and sql.startswith('INSERT INTO reservations') and 'public-join' in sql:
                clock[0] = limit + 1
            if not parent_done.is_set(): return
            if phase == 'verification' and sql.startswith('SELECT * FROM runtime_operations WHERE id=') and "command='stop'" in sql:
                clock[0] = limit + 1

        def instance(connection, key):
            if threading.get_ident() == public_thread[0]: connection.set_trace_callback(trace)
            return original_instance(connection, key)

        def sleep(seconds):
            self.assertEqual(public_thread[0], threading.get_ident())
            self.assertEqual(0.01, seconds)
            proceed.set()
            self.join(session)
            self.assertIsNone(session['error'])
            parent_snapshot.extend(self.query('SELECT * FROM runtime_operations WHERE id=?', (session['stop_operation'],)))
            parent_done.set()
            if phase == 'wait': clock[0] = limit + 1

        @contextmanager
        def lock_wait(deadline):
            depth = getattr(depths, 'value', 0) + 1
            depths.value = depth
            try:
                with original_wait(deadline): yield
                if phase == 'return' and threading.get_ident() == public_thread[0] and depth == 2:
                    clock[0] = limit + 1
            finally:
                depths.value = depth - 1

        @contextmanager
        def transaction(*args, **kwargs):
            with original_transaction(*args, **kwargs) as connection:
                yield connection
            if phase == 'commit' and kwargs.get('read_only') and parent_done.is_set() \
                    and threading.get_ident() == public_thread[0]:
                clock[0] = limit + 1

        timeout = 40 if source_deadline else 10
        payload = dict(self.f.token, timeoutSeconds=timeout)
        with patch.object(target.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(target.time, 'sleep', side_effect=sleep), \
                patch.object(self.runtime, '_stop', side_effect=stop), \
                patch.object(self.runtime, '_join_direct_stop', side_effect=join), \
                patch.object(self.f.registry, '_instance', side_effect=instance), \
                patch.object(self.runtime, '_lock_wait', side_effect=lock_wait), \
                patch.object(self.f.registry, 'transaction', side_effect=transaction):
            self.base.stop_ready.set(); self.base.save_ready.set()
            try:
                self.assertTrue(paused.wait(5))
                self.assertEqual(30.0, session['stop_deadline'])
                clock[0] = 5.0
                with ThreadPoolExecutor(max_workers=1) as pool:
                    public = pool.submit(self.f.command, 'stop', payload, 'public-join')
                    if phase == 'normal':
                        result = public.result(5)
                        self.assertEqual(session['save_evidence'], result['managedSave'])
                    else:
                        with self.assertRaises(RegistryError) as caught: public.result(5)
                        self.assertEqual('STOP_TIMEOUT', caught.exception.code)
            finally:
                proceed.set()
                self.join(session)
        self.assertIsNone(session['error'])
        parent = self.query('SELECT * FROM runtime_operations WHERE id=?', (session['stop_operation'],))[0]
        self.assertEqual('COMPLETED', parent['status'])
        if parent_snapshot: self.assertEqual(parent_snapshot[0], parent)
        self.assertEqual('LEASED_STOPPED', self.f.state()['state'])
        self.assertEqual(0, self.f.state()['pending'])
        joined = self.query('SELECT * FROM runtime_operations WHERE id=?', ('public-join',))
        if phase in ('entry', 'admission'):
            self.assertEqual([], joined)
            self.assertEqual([], self.query('SELECT * FROM reservations'))
        else:
            self.assertEqual('COMPLETED', joined[0]['status'])
            references = self.query('SELECT * FROM reservations WHERE operation=?', ('public-join',))
            self.assertEqual(0, len(references))
            before = self.query('SELECT * FROM runtime_operations ORDER BY id')
            self.assertTrue(self.f.command('stop', payload, 'public-join')['replayed'])
            self.assertEqual(before, self.query('SELECT * FROM runtime_operations ORDER BY id'))
        self.assertEqual(1, len(self.f.adapter.created))
        self.assertEqual(0, self.f.rcon_calls)
        self.assertEqual(['GRANT','ACK','BLOCKED','COMPLETION'], self.base.trace)
        self.assertEqual(4, self.base.channel.send_frame.call_count)
        self.assertEqual([], self.query('SELECT * FROM endpoint_reservations'))

    def test_join_keeps_caller_deadline_across_delayed_admission(self):
        self.join_boundary('wait')

    def test_join_keeps_original_stop_deadline(self):
        self.join_boundary('wait', source_deadline=True)

    def test_join_expired_before_admission_has_no_reservation(self):
        self.join_boundary('entry')

    def test_join_reservation_crosses_deadline_keeps_parent_result_without_normal_return(self):
        self.join_boundary('reservation')

    def test_join_final_verification_crosses_deadline_without_normal_return(self):
        self.join_boundary('verification')

    def test_join_read_commit_crosses_deadline_keeps_parent_record_without_return(self):
        self.join_boundary('commit')

    def test_join_return_crosses_deadline_without_normal_return(self):
        self.join_boundary('return')

    def test_join_within_original_deadlines_returns_recorded_result(self):
        self.join_boundary('normal')

    def test_public_stop_passes_fixed_deadline_to_waiting(self):
        clock = [0.0]
        original_stop = self.runtime._stop
        original_drain = self.managed.drain
        observed = []
        stop_thread = [None]
        def stop(*args):
            stop_thread[0] = threading.get_ident()
            clock[0] = 5.0
            return original_stop(*args)
        def drain(instance, session, deadline):
            if threading.get_ident() == stop_thread[0]: observed.append(deadline)
            return original_drain(instance, session, deadline)
        with patch.object(target.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.runtime, '_stop', side_effect=stop), \
                patch.object(self.managed, 'drain', side_effect=drain):
            self.public_stop()
        self.assertEqual([10.0], observed)


    def test_public_drain_deadline_prevents_additional_rcon(self):
        self.base.start()
        clock = [0.0]
        original = self.managed.drain
        def drain(instance, session, deadline):
            result = original(instance, session, deadline)
            clock[0] = deadline + 1
            return result
        with patch.object(target.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.managed, 'drain', side_effect=drain):
            with self.assertRaises(RegistryError) as caught: self.stop()
            self.assertEqual('STOP_TIMEOUT', caught.exception.code)
            with self.assertRaises(RegistryError) as replay: self.stop()
            self.assertEqual('RECOVERY_REQUIRED', replay.exception.code)
            self.assertEqual(0, self.f.rcon_calls)
            self.assertEqual(1, len(self.f.adapter.created))
            self.assertIsNone(self.f.adapter.created[0].poll())
            self.assertEqual(['GRANT','ACK'], self.base.trace)
            self.assertEqual('RECOVERY_REQUIRED', self.f.state()['state'])
            self.assertTrue(self.query('SELECT * FROM reservations'))

    def test_join_admission_transaction_crosses_deadline_without_reservation(self):
        self.join_boundary('admission')

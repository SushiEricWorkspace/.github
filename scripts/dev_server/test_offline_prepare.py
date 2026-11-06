"""実Registry予約と既存C/Wの合成OS境界でPREPAREDまでを検証する。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import sqlite3
import struct
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import offline_process, runtime_control
from . import test_offline_control_concurrency as concurrency
from . import test_offline_process as processes
from .build_inputs import canonical, digest
from .database_proxy_pipe import ProxyPipe
from .local_ipc import encode
from .registry import RegistryError


class OfflinePrepareTests(unittest.TestCase):
    def setUp(self):
        f = concurrency.OfflineConcurrencyTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f, self.runtime, self.registry = f, f.runtime, f.f.registry
        p = processes.OfflineProcessTests()
        p.setUp()
        self.addCleanup(p.doCleanups)
        self.p = p
        original = p.adapter.identity
        p.adapter.identity = lambda pid: None if pid == f.old_pid else original(pid)
        self.runtime.process_adapter = p.adapter
        self.runtime.managed = SimpleNamespace(transport=SimpleNamespace(native=p.native))
        self.enterContext(patch.object(processes.Pipe, 'acknowledged', self.acknowledged, create=True))
        self.token = deepcopy(f.f.token)
        self.operation = processes.frame(True)['operationId']
        self.request = processes.frame(True)['request']
        self.opened = self.runtime._offline_open(self.token, operation_id='offline-prepare-owner',
            revision=f.revision, ready=f.ready, deadline=time.monotonic()+10)
        self.c = self.runtime._offline.controllers[self.token['instanceId']]

    @staticmethod
    def acknowledged(pipe):
        pipe.acks = getattr(pipe, 'acks', 0) + 1

    def prepare(self):
        return self.runtime._offline_prepare(self.token, operation_id=self.operation,
                                             request=self.request, deadline=time.monotonic()+10)

    def saved(self):
        with self.runtime._transaction(self.token['instanceId'], read_only=True) as db:
            return json.loads(db.execute("SELECT response FROM runtime_operations WHERE id='offline-prepare-owner'").fetchone()[0])

    def mutate(self, sql, args=()):
        with self.runtime._transaction(self.token['instanceId']) as db:
            db.execute(sql, args)

    def pending(self):
        with self.runtime._transaction(self.token['instanceId'], read_only=True) as db:
            self.assertEqual(1, db.execute('SELECT pending FROM instances WHERE id=?', (self.token['instanceId'],)).fetchone()[0])
            self.assertEqual('PENDING', db.execute("SELECT status FROM runtime_operations WHERE id='offline-prepare-owner'").fetchone()[0])
            self.assertEqual(1, db.execute("SELECT count(*) FROM reservations WHERE operation='offline-prepare-owner'").fetchone()[0])

    def no_retry(self):
        counts = (len(self.p.adapter.processes), [p.starts for p in self.p.adapter.processes],
                  [len(p.sent) for p in self.p.pipes], [p.effects for p in self.p.pipes])
        with self.assertRaises(RegistryError): self.prepare()
        self.assertEqual(counts, (len(self.p.adapter.processes), [p.starts for p in self.p.adapter.processes],
                                [len(p.sent) for p in self.p.pipes], [p.effects for p in self.p.pipes]))
        self.pending()

    def test_original_transaction_and_nonempty_request_reach_prepared_without_edit(self):
        self.assertIs(self.runtime._transaction.__func__, runtime_control.RuntimeControl._transaction)
        original = self.c.acknowledge_startup
        def acknowledged(value):
            self.assertEqual(self.operation, self.saved()['worker']['operationId'])
            self.assertNotIn('startupResponseSha256', self.saved()['worker'])
            original(value)
        with patch.object(self.c, 'acknowledge_startup', acknowledged):
            result = self.prepare()
        w = self.runtime._offline.workers[self.token['instanceId']]
        self.assertEqual('PREPARED', result['status'])
        self.assertEqual(self.request, w.pipe.sent[0]['request'])
        self.assertEqual(1, len(w.pipe.sent[0]['request']['action']['products']))
        for key, value in [('expectedStateSha256',self.opened['stateSha256']),
                           ('expectedRevision',self.opened['stateRevision']),('runUuid',self.opened['runUuid'])]:
            self.assertEqual(value, w.pipe.sent[0][key])
        self.assertEqual(self.c.data_identity, w.pipe.sent[0]['owner'])
        self.assertNotEqual(self.c.process.group, w.process.group)
        self.assertEqual([1, 0], [p.effects for p in self.p.pipes])
        self.assertEqual([1, 1], [p.acks for p in self.p.pipes])
        self.assertEqual(digest(w.response), self.saved()['worker']['startupResponseSha256'])
        self.pending()
        saved = json.dumps(self.saved())
        for secret in (w.session, w.nonce, 'tab.apple', 'purchasePrice'):
            self.assertNotIn(secret, saved)
        self.no_retry()

    def test_each_recorded_open_and_registry_binding_mismatch_prevents_worker_creation(self):
        mutations = [
            ("UPDATE run_admission SET revision=revision+1", ()),
            ("UPDATE run_admission SET descriptor_hash=?", ('e'*64,)),
            ("UPDATE instances SET writers=1", ()),
            ("UPDATE instances SET children=1", ()),
            ("UPDATE instances SET pending=0", ()),
            ("UPDATE instances SET epoch=epoch+1", ()),
            ("UPDATE instances SET state='RECOVERY_REQUIRED'", ()),
            ("UPDATE run_admission SET gate='OPEN'", ()),
            ("UPDATE reservations SET epoch=epoch+1", ()),
            ("UPDATE runtime_operations SET fingerprint='wrong' WHERE id='offline-prepare-owner'", ()),
        ]
        # 各負例は独立した正本から開始する。
        for sql, args in mutations:
            with self.subTest(sql=sql):
                with self.runtime._transaction(self.token['instanceId'], read_only=True) as db:
                    backup = sqlite3.connect(':memory:')
                    db.backup(backup)
                try:
                    self.mutate(sql, args)
                    with self.assertRaises(RegistryError): self.prepare()
                    self.assertEqual(1, len(self.p.adapter.processes))
                    self.assertFalse(self.c.startup_acknowledged)
                finally:
                    with self.registry.db_lock:
                        restore = sqlite3.connect(self.registry.root / 'manager/registry.sqlite')
                        try: backup.backup(restore)
                        finally: restore.close(); backup.close()

    def test_recorded_open_hash_and_binding_hash_mismatch_reject_before_ack(self):
        saved = self.saved()
        for bad in (dict(saved, startupResponseSha256='e'*64),
                    dict(saved, binding=dict(saved['binding'], ready=dict(saved['binding']['ready'], stateRevision=99)))):
            with self.subTest(bad=bad):
                self.mutate("UPDATE runtime_operations SET response=? WHERE id='offline-prepare-owner'", (json.dumps(bad),))
                with self.assertRaises(RegistryError): self.prepare()
                self.assertEqual(1, len(self.p.adapter.processes))
                self.assertFalse(self.c.startup_acknowledged)
                self.assertNotIn('worker', self.saved())
        self.mutate("UPDATE runtime_operations SET response=? WHERE id='offline-prepare-owner'", (json.dumps(saved),))

    def test_each_request_binding_mismatch_rejects_before_worker_or_ack(self):
        for key, value in [('requestId', processes.WORLD), ('worldUuid', processes.RUN)]:
            with self.subTest(key=key):
                with patch.dict(self.request, {key:value}):
                    with self.assertRaises(RegistryError): self.prepare()
                self.assertEqual(1, len(self.p.adapter.processes))
                self.assertFalse(self.c.startup_acknowledged)
                self.assertNotIn('worker', self.saved())

    def test_intent_journal_failure_has_no_worker_or_pipe_ack(self):
        self.mutate("CREATE TRIGGER deny_prepare BEFORE UPDATE OF response ON runtime_operations WHEN OLD.id='offline-prepare-owner' BEGIN SELECT RAISE(FAIL,'intent failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.prepare()
        self.assertNotIn('worker', self.saved())
        self.assertEqual(1, len(self.p.adapter.processes))
        self.assertFalse(self.c.startup_acknowledged)
        self.pending()

    def test_prepared_journal_failure_keeps_worker_and_unacknowledged_response(self):
        original = self.p.after_send
        def after():
            original()
            if len(self.p.pipes) == 2:
                self.mutate("CREATE TRIGGER deny_prepared BEFORE UPDATE OF response ON runtime_operations WHEN OLD.id='offline-prepare-owner' BEGIN SELECT RAISE(FAIL,'prepared failure'); END")
        self.p.after_send = after
        with self.assertRaises(sqlite3.IntegrityError): self.prepare()
        w = self.runtime._offline.workers[self.token['instanceId']]
        self.assertTrue(w.ready)
        self.assertFalse(w.startup_acknowledged)
        self.assertNotIn('startupResponseSha256', self.saved()['worker'])
        self.assertEqual([1, 0], [p.effects for p in self.p.pipes])
        self.no_retry()

    def test_c_ack_journal_failure_preserves_intent_without_worker_creation(self):
        with patch.object(self.c, '_record', side_effect=OSError('C ack journal')):
            with self.assertRaises(OSError): self.prepare()
        self.assertEqual(1, len(self.p.adapter.processes))
        self.assertFalse(self.c.startup_acknowledged)
        self.assertEqual(self.operation, self.saved()['worker']['operationId'])
        self.no_retry()

    def test_worker_partial_send_is_not_retried_or_acked(self):
        self.p.failure = 'partial'
        with self.assertRaises(TimeoutError): self.prepare()
        w = self.runtime._offline.workers[self.token['instanceId']]
        self.assertFalse(w.ready)
        self.assertFalse(w.startup_acknowledged)
        self.assertEqual([1, 0], [p.effects for p in self.p.pipes])
        self.no_retry()

    def test_worker_ack_journal_failure_keeps_recorded_prepared_and_pipe(self):
        original = offline_process.write_json
        def write(path, value):
            if value['role'] == 'w' and 'startupAcknowledgedSha256' in value:
                raise OSError('W ack journal')
            return original(path, value)
        with patch.object(offline_process, 'write_json', write):
            with self.assertRaises(OSError): self.prepare()
        w = self.runtime._offline.workers[self.token['instanceId']]
        self.assertTrue(w.ready)
        self.assertFalse(w.startup_acknowledged)
        self.assertEqual(digest(w.response), self.saved()['worker']['startupResponseSha256'])
        self.assertEqual(0, getattr(w.pipe, 'acks', 0))
        self.no_retry()

    def test_gc_and_stopping_reject_prepare_before_intent_or_ack(self):
        for field in ('gc_active','stopping'):
            with self.subTest(field=field):
                setattr(self.registry, field, True)
                try:
                    with self.assertRaises(RegistryError): self.prepare()
                finally:
                    setattr(self.registry, field, False)
                self.assertNotIn('worker', self.saved())
                self.assertFalse(self.c.startup_acknowledged)
                self.assertEqual(1, len(self.p.adapter.processes))
                self.pending()

    def test_worker_response_loss_preserves_intent_and_never_retries(self):
        self.p.failure = 'lost'
        with self.assertRaises(EOFError): self.prepare()
        self.assertEqual(2, len(self.p.adapter.processes))
        self.assertNotIn('startupResponseSha256', self.saved()['worker'])
        self.assertEqual([1, 0], [p.effects for p in self.p.pipes])
        self.no_retry()

    def test_worker_response_state_mismatch_stops_before_ack_and_edit(self):
        self.p.response_change = ('stateRevision', 99)
        with self.assertRaises(RegistryError): self.prepare()
        w = self.runtime._offline.workers[self.token['instanceId']]
        self.assertFalse(w.ready)
        self.assertFalse(w.startup_acknowledged)
        self.assertEqual([1, 0], [p.effects for p in self.p.pipes])
        self.no_retry()

    def test_changed_worker_intent_during_creation_prevents_any_prepare_send(self):
        original = self.p.adapter.OwnedProcess
        def construct():
            saved = self.saved()
            saved['worker']['requestSha256'] = 'e'*64
            self.mutate("UPDATE runtime_operations SET response=? WHERE id='offline-prepare-owner'", (json.dumps(saved),))
            return original()
        with patch.object(self.p.adapter, 'OwnedProcess', construct):
            with self.assertRaises(RegistryError): self.prepare()
        self.assertEqual(0, self.p.adapter.processes[-1].starts)
        self.assertEqual(1, len(self.p.pipes))
        self.no_retry()

    def test_concurrent_prepare_reserves_once_using_original_transaction(self):
        committed, release = threading.Event(), threading.Event()
        original = self.c.acknowledge_startup
        entered = []
        def held(value):
            entered.append(value)
            self.assertEqual(1, len(entered), '同じCへ二重ACKが到達しました')
            committed.set()
            self.assertTrue(release.wait(10))
            original(value)
        with patch.object(self.c, 'acknowledge_startup', held), ThreadPoolExecutor(max_workers=2) as workers:
            winner = workers.submit(self.prepare)
            try:
                self.assertTrue(committed.wait(10))
                loser = workers.submit(self.prepare)
                with self.assertRaises(RegistryError): loser.result(10)
                self.assertEqual(1, len(self.p.adapter.processes))
                self.assertNotIn('startupResponseSha256', self.saved()['worker'])
                self.pending()
            finally:
                release.set()
            self.assertEqual('PREPARED', winner.result(10)['status'])
        self.assertEqual(2, len(self.p.adapter.processes))
        self.assertIs(self.runtime._transaction.__func__, runtime_control.RuntimeControl._transaction)
        self.no_retry()


class StartupAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.pipe = ProxyPipe()
        self.addCleanup(self.pipe.close_after_exit)
        self.response = dict(status='PREPARED', stateRevision=1)
        body = encode(self.response)
        self.pipe.send(dict(command='prepare'), time.monotonic()+1, lambda:None)
        os.write(self.pipe.child_output, struct.pack('!I', len(body))+body)
        self.assertEqual(self.response, self.pipe.receive(time.monotonic()+1, lambda:None))
        self.helper = offline_process.OfflineProcess.__new__(offline_process.OfflineProcess)
        self.helper.ready = True
        self.helper.startup_acknowledged = False
        self.helper.response = canonical(self.response)
        self.helper.record = {}
        self.helper.verify = Mock()
        self.helper._record = Mock()
        self.helper.pipe = self.pipe

    def test_real_pipe_can_advance_only_after_matched_recorded_response(self):
        original = self.pipe.acknowledged
        def ack():
            self.helper._record.assert_called_once()
            original()
        with patch.object(self.pipe, 'acknowledged', ack):
            self.helper.acknowledge_startup(digest(self.helper.response))
        self.assertTrue(self.helper.startup_acknowledged)
        self.assertIsNone(self.pipe.outgoing)
        self.assertEqual(bytearray(), self.pipe.incoming)
        self.pipe.send(dict(command='admit'), time.monotonic()+1, lambda:None)
        with self.assertRaises(RegistryError): self.helper.acknowledge_startup(digest(self.helper.response))
        self.assertIsNotNone(self.pipe.outgoing)

    def test_mismatch_verify_and_journal_failure_preserve_real_pipe_buffers(self):
        before = (self.pipe.outgoing, bytes(self.pipe.incoming), self.pipe.offset, self.pipe.expected)
        for failure in ('hash','verify','journal'):
            with self.subTest(failure=failure):
                self.helper.verify.side_effect = OSError('identity') if failure=='verify' else None
                self.helper._record.side_effect = OSError('journal') if failure=='journal' else None
                with self.assertRaises((RegistryError,OSError)):
                    self.helper.acknowledge_startup('e'*64 if failure=='hash' else digest(self.helper.response))
                self.assertFalse(self.helper.startup_acknowledged)
                self.assertEqual(before, (self.pipe.outgoing, bytes(self.pipe.incoming), self.pipe.offset, self.pipe.expected))

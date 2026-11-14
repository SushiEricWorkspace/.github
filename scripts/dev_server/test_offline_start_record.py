"""固定start予約から新run/閉門正本への一度だけの記録を検査する。"""
from contextlib import contextmanager
from copy import deepcopy
import json
import sys
import unittest
from unittest.mock import patch

from . import offline_control
from .registry import RegistryError
from . import test_offline_start_reservation as reservations


class StartRecordTests(unittest.TestCase):
    def setUp(self):
        self.b = reservations.StartReservationTests()
        self.addCleanup(self.b.doCleanups)
        self.b.setUp()
        self.runtime, self.offline = self.b.runtime, self.b.offline
        self.key = self.b.key

    def record(self, **changes):
        args = dict(operation_id=reservations.START, read_operation_id=reservations.readers.OPERATION,
            open_operation_id=self.b.base.o.base.opened['operationId'], ready=self.b.base.o.ready,
            deadline=self.b.base.f.deadline)
        args.update(changes)
        return self.runtime._offline_record_start(self.b.token, **args)

    def rows(self):
        run = self.offline.starts[self.key]['run']
        with self.runtime.registry.transaction(read_only=True) as db:
            values = [db.execute(sql, (run,)).fetchone() for sql in
                ('SELECT * FROM runs WHERE id=?', 'SELECT * FROM run_admission WHERE run=?')]
        return [dict(v) if v else None for v in values]

    def retained(self, recorded):
        held = self.offline.start_records[self.key]
        self.assertIs(held['recorded'], recorded)
        self.assertIs(held['returned'], False)
        self.assertTrue(callable(self.offline.starts[self.key]['binding']))
        self.assertIs(self.b.base.p.reader, self.offline.readers[self.b.base.key]['reader'])
        self.assertIsNotNone(self.b.base.p.reader.request)
        self.assertIsNotNone(self.b.base.p.reader.ready_check_response)
        self.assertEqual({}, self.runtime.processes)
        self.assertEqual(('STARTING', 1), (self.b.row()['state'], self.b.row()['pending']))
        self.assertEqual(1, len(self.b.saved()[1]))
        effects = self.b.base.p.effects()
        with self.assertRaises(RegistryError):self.record()
        with self.assertRaises(RegistryError):self.record(operation_id=reservations.OTHER)
        self.assertEqual(effects, self.b.base.p.effects())
        self.assertIs(held, self.offline.start_records[self.key])

    def final_copy(self, change, *, recorded=True):
        original = deepcopy
        fired = []
        def copy(value, *args, **kwargs):
            result = original(value, *args, **kwargs)
            if isinstance(value, dict) and value.get('status') == 'OFFLINE_START_RECORDED':
                fired.append(True);change()
            return result
        with patch.object(offline_control, 'deepcopy', side_effect=copy):
            with self.assertRaises(RegistryError):self.record()
        self.assertEqual([True], fired)
        self.retained(recorded)

    def test_same_new_run_and_nonce_are_recorded_without_m_or_second_ports(self):
        before = self.b.base.f.saved()
        result, binding = self.record()
        row, gate = self.rows()
        record = json.loads(row['record'])
        intent = json.loads(self.b.saved()[0]['response'])['offlineStart']
        self.assertEqual('OFFLINE_START_RECORDED', result['status'])
        self.assertEqual((intent['runId'], intent['nonce'], reservations.START),
            (record['runId'], record['nonce'], record['startOperationId']))
        self.assertEqual(intent['endpoints'], record['endpoints'])
        self.assertEqual(('CLOSED', 0, None), (gate['gate'], gate['revision'], gate['descriptor_hash']))
        self.assertEqual(row['record'], binding())
        self.assertIs(binding, self.offline.start_records[self.key]['binding'])
        self.assertEqual(before, self.b.base.f.saved())
        with self.runtime.registry.transaction(read_only=True) as db:
            self.assertEqual(3, db.execute('SELECT count(*) FROM endpoint_reservations WHERE run=?', (record['runId'],)).fetchone()[0])
        self.assertTrue(self.offline.start_records[self.key]['returned'])
        self.assertEqual({}, self.runtime.processes)
        self.b.no_retry()

    def test_epoch_commit_before_record_write_has_no_new_run(self):
        original = self.runtime._record
        def record(*args, **kwargs):
            self.b.commit()
            return original(*args, **kwargs)
        with patch.object(self.runtime, '_record', side_effect=record):
            with self.assertRaises(RegistryError):self.record()
        self.assertEqual([None, None], self.rows())
        self.retained(False)

    def test_writer_closed_commit_before_write_has_no_new_run(self):
        original = self.runtime._record
        def record(*args, **kwargs):
            self.b.commit('closed')
            return original(*args, **kwargs)
        with patch.object(self.runtime, '_record', side_effect=record):
            with self.assertRaises(RegistryError):self.record()
        self.assertEqual([None, None], self.rows())
        self.retained(False)

    def test_approval_change_inside_record_write_rolls_back(self):
        transaction = self.runtime._transaction
        @contextmanager
        def changed(instance, *, read_only=False):
            with transaction(instance, read_only=read_only) as db:
                if not read_only and self.key in self.offline.start_records:
                    self.b.approval['changed'] = True
                yield db
        with patch.object(self.runtime, '_transaction', changed):
            with self.assertRaises(RegistryError):self.record()
        self.assertEqual([None, None], self.rows())
        self.retained(False)

    def test_run_mutation_before_serialization_is_rejected(self):
        original = self.runtime._record
        def record(run, **kwargs):
            run['nonce'] = 'changed'
            return original(run, **kwargs)
        with patch.object(self.runtime, '_record', side_effect=record):
            with self.assertRaises(RegistryError):self.record()
        self.assertEqual([None, None], self.rows())
        self.retained(False)

    def test_deadline_before_record_write_creates_no_run(self):
        original = self.runtime._record
        def record(*args, **kwargs):
            self.b.base.f.clock = self.b.base.f.deadline
            return original(*args, **kwargs)
        with patch.object(self.runtime, '_record', side_effect=record):
            with self.assertRaises(RegistryError):self.record()
        self.assertEqual([None, None], self.rows())
        self.retained(False)

    def test_deadline_after_record_commit_keeps_run_without_return(self):
        original = self.runtime._record
        def record(*args, **kwargs):
            value = original(*args, **kwargs)
            self.b.base.f.clock = self.b.base.f.deadline
            return value
        with patch.object(self.runtime, '_record', side_effect=record):
            with self.assertRaises(RegistryError):self.record()
        self.assertTrue(all(self.rows()))
        self.retained(True)

    def test_final_copy_run_object_change_refuses_return(self):
        self.final_copy(lambda:self.offline.start_records[self.key]['run'].update(nonce='changed'))

    def test_final_copy_record_progress_change_refuses_return(self):
        self.final_copy(lambda:self.offline.start_records[self.key].update(recorded=False), recorded=False)

    def test_final_copy_new_gate_commit_refuses_return(self):
        def change():
            run = self.offline.starts[self.key]['run']
            def write():
                with self.runtime.registry.transaction() as db:
                    db.execute("UPDATE run_admission SET gate='OPEN' WHERE run=?", (run,))
            self.b.base.pool.submit(write).result(10)
        self.final_copy(change)

    def test_final_copy_new_run_commit_refuses_return(self):
        def change():
            run = self.offline.starts[self.key]['run']
            def write():
                with self.runtime.registry.transaction() as db:
                    db.execute("UPDATE runs SET record='{}' WHERE id=?", (run,))
            self.b.base.pool.submit(write).result(10)
        self.final_copy(change)

    def test_callback_rejects_later_new_gate_change(self):
        _, binding = self.record()
        run = self.offline.starts[self.key]['run']
        with self.runtime.registry.transaction() as db:
            db.execute('UPDATE run_admission SET revision=revision+1 WHERE run=?', (run,))
        with self.assertRaises(RegistryError):binding()
        self.assertTrue(self.offline.start_records[self.key]['returned'])

    def test_public_renew_before_record_write_and_after_return_are_allowed(self):
        original = self.runtime._record
        def renew(operation):
            self.b.base.pool.submit(self.runtime.registry.execute, 'renew',
                dict(self.b.token, ttlSeconds=600), operation_id=operation).result(10)
        def record(*args, **kwargs):
            renew('new-run-renew')
            return original(*args, **kwargs)
        with patch.object(self.runtime, '_record', side_effect=record):
            result, binding = self.record()
        renew('new-run-renew-later')
        self.assertEqual(result['runId'], json.loads(binding())['runId'])

    def test_record_write_has_no_file_or_native_read(self):
        transaction = self.runtime._transaction
        regular = offline_control.regular_bytes
        resources = self.offline._terminal_resources
        writing = []
        @contextmanager
        def checked(instance, *, read_only=False):
            with transaction(instance, read_only=read_only) as db:
                active = not read_only and self.key in self.offline.start_records
                if active:writing.append(True)
                try:yield db
                finally:
                    if active:writing.pop()
        def read(*args, **kwargs):
            self.assertFalse(writing)
            return regular(*args, **kwargs)
        def native(*args, **kwargs):
            self.assertFalse(writing)
            return resources(*args, **kwargs)
        with patch.object(self.runtime, '_transaction', checked), \
             patch.object(offline_control, 'regular_bytes', side_effect=read), \
             patch.object(self.offline, '_terminal_resources', side_effect=native):
            self.record()[1]()

    def test_serialized_record_change_is_rejected_inside_write(self):
        from . import runtime_control
        dumps = json.dumps
        def changed(value, *args, **kwargs):
            if isinstance(value, dict) and value.get('runId') == self.offline.starts.get(self.key, {}).get('run'):
                if self.key in self.offline.start_records and sys._getframe(1).f_code.co_name == '_record':return '{}'
            return dumps(value, *args, **kwargs)
        with patch.object(runtime_control.json, 'dumps', changed):
            with self.assertRaises(RegistryError):self.record()
        self.assertEqual([None, None], self.rows())
        self.retained(False)

    def test_final_return_copy_change_refuses_return(self):
        original = deepcopy
        def copy(value, *args, **kwargs):
            result = original(value, *args, **kwargs)
            if isinstance(value, dict) and value.get('status') == 'OFFLINE_START_RECORDED':
                result['runId'] = 'changed'
            return result
        with patch.object(offline_control, 'deepcopy', side_effect=copy):
            with self.assertRaises(RegistryError):self.record()
        self.retained(True)

    def test_final_read_exit_run_change_refuses_return(self):
        transaction = self.runtime._transaction
        reads = []
        @contextmanager
        def changed(instance, *, read_only=False):
            with transaction(instance, read_only=read_only) as db:
                yield db
            held = self.offline.start_records.get(self.key)
            if read_only and held is not None and held['recorded']:
                reads.append(True)
                if len(reads)==2:held['run']['nonce']='changed'
        with patch.object(self.runtime, '_transaction', changed):
            with self.assertRaises(RegistryError):self.record()
        self.assertEqual(2, len(reads))
        self.retained(True)

    def test_final_copy_packet_replacement_refuses_return(self):
        kept = []
        def change():
            kept.append(self.offline.start_records[self.key]['packet'])
            self.offline.start_records[self.key]['packet'] = ('changed', None)
        self.final_copy(change)
        self.assertIs(kept[0][1], self.offline.starts[self.key]['binding'])

    def test_epoch_change_during_admission_insert_rolls_back_both_rows(self):
        transaction = self.runtime._transaction
        fired = []
        @contextmanager
        def changed(instance, *, read_only=False):
            with transaction(instance, read_only=read_only) as db:
                def trace(sql):
                    if sql.startswith('INSERT INTO run_admission') and not fired:
                        fired.append(True)
                        db.execute('UPDATE instances SET epoch=epoch+1 WHERE id=?', (instance,))
                if not read_only and self.key in self.offline.start_records:db.set_trace_callback(trace)
                try:yield db
                finally:db.set_trace_callback(None)
        with patch.object(self.runtime, '_transaction', changed):
            with self.assertRaises(RegistryError):self.record()
        self.assertEqual([True], fired)
        self.assertEqual([None, None], self.rows())
        self.retained(False)

    def test_manifest_read_crossing_deadline_has_no_new_run(self):
        regular = offline_control.regular_bytes
        fired = []
        def read(path, *args, **kwargs):
            raw = regular(path, *args, **kwargs)
            if path.name == 'runtime.json' and self.key in self.offline.starts \
                    and self.offline.starts[self.key].get('returned') is True:
                fired.append(True)
                self.b.base.f.clock = self.b.base.f.deadline
            return raw
        with patch.object(offline_control, 'regular_bytes', side_effect=read):
            with self.assertRaises(RegistryError):self.record()
        self.assertEqual([True], fired)
        self.assertEqual([None, None], self.rows())
        self.retained(False)

    def test_final_copy_callback_replacement_refuses_return(self):
        self.final_copy(lambda:self.offline.start_records[self.key].update(binding=lambda:None))

    def test_record_and_final_return_keep_resource_and_lock_deadline(self):
        record, copy = self.runtime._record, deepcopy
        checks = []
        def check():
            self.assertGreater(self.runtime.registry.resource_users, 0)
            self.assertEqual(self.b.base.f.deadline, self.runtime._lock_deadline.value)
            checks.append(True)
        def writing(*args, **kwargs):
            check()
            return record(*args, **kwargs)
        def copying(value, *args, **kwargs):
            result = copy(value, *args, **kwargs)
            if isinstance(value, dict) and value.get('status')=='OFFLINE_START_RECORDED':check()
            return result
        with patch.object(self.runtime, '_record', side_effect=writing), \
             patch.object(offline_control, 'deepcopy', side_effect=copying):
            self.record()
        self.assertEqual([True, True], checks)
        self.assertEqual(0, self.runtime.registry.resource_users)
        self.assertIsNone(self.runtime._lock_deadline.value)

    def test_gc_exclusion_is_held_after_reservation_until_record(self):
        original = self.runtime._record
        def exclusive():
            with self.runtime.registry.resource_use(exclusive=True):pass
        def record(*args, **kwargs):
            with self.assertRaises(RegistryError):self.b.base.pool.submit(exclusive).result(10)
            return original(*args, **kwargs)
        with patch.object(self.runtime, '_record', side_effect=record):self.record()

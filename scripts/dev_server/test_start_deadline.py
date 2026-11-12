"""入口で固定した起動期限と、期限境界での正本・所有資源の保全を検証する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import os
import threading
import unittest
from unittest.mock import Mock, patch

from . import runtime_control as control, mod_control_startup as startup, runtime_process
from . import test_runtime_control as runtime_tests, test_mod_control_startup as startup_tests
from .registry import RegistryError


class StartDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.base = runtime_tests.RuntimeControlTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.prepare()
        self.runtime = self.base.registry.runtime
        self.clock = [0.0]
        self.enterContext(patch.object(control.time, 'monotonic', side_effect=lambda: self.clock[0]))

    def expired(self, call=None, *, created=0, committed=False):
        with self.assertRaises(RegistryError) as caught:
            (call or self.base.start)()
        self.assertEqual('START_TIMEOUT', caught.exception.code)
        self.assertEqual(created, len(self.base.adapter.created))
        self.assertEqual(0, self.base.rcon_calls)
        self.assertTrue(all(not process.closed for process in self.base.adapter.created))
        with self.base.registry.transaction(read_only=True) as db:
            operation = db.execute("SELECT * FROM runtime_operations WHERE id='start-01'").fetchone()
            reservations = db.execute("SELECT * FROM reservations WHERE operation='start-01'").fetchall()
            if committed:
                self.assertEqual('COMPLETED', operation['status'])
                self.assertEqual('RUNNING', self.base.state()['state'])
                self.assertEqual([], reservations)
            elif operation is None:
                self.assertEqual('READY', self.base.state()['state'])
                self.assertEqual([], reservations)
            else:
                self.assertEqual('RECOVERY_REQUIRED', operation['status'])
                self.assertEqual('RECOVERY_REQUIRED', self.base.state()['state'])
                self.assertEqual(1, len(reservations))

    def test_preflight_time_is_included_before_reservation(self):
        with patch.object(self.runtime.managed, 'approved', side_effect=lambda *_: self.clock.__setitem__(0, 2.0)):
            self.expired()
        with self.base.registry.transaction(read_only=True) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM runtime_operations WHERE id='start-01'").fetchone())

    def test_dry_run_checks_entry_deadline(self):
        with patch.object(self.runtime.managed, 'approved', side_effect=lambda *_: self.clock.__setitem__(0, 2.0)):
            self.expired(lambda: self.runtime.execute('start', dict(self.base.token, timeoutSeconds=2),
                                                     operation_id='start-01', dry_run=True))

    def test_expiry_before_first_run_record_keeps_reservation(self):
        original = self.runtime._start
        def start(*args):
            self.clock[0] = 2.0
            return original(*args)
        with patch.object(self.runtime, '_start', side_effect=start), patch.object(self.runtime, '_record') as record:
            self.expired()
        record.assert_not_called()

    def test_expiry_before_os_creation_has_no_process_effect(self):
        original = control.launch_command
        def launch(*args, **kwargs):
            result = original(*args, **kwargs)
            self.clock[0] = 2.0
            return result
        with patch.object(control, 'launch_command', side_effect=launch):
            self.expired()
        self.assertEqual('NOT_CREATED', next(iter(self.runtime.processes.values())).stage)

    def test_handshake_receives_entry_deadline_without_reset(self):
        original = self.runtime.managed.prepare
        def prepare(*args):
            self.clock[0] = 1.5
            return original(*args)
        with patch.object(self.runtime.managed, 'prepare', side_effect=prepare), \
             patch.object(self.runtime.managed, 'handshake') as handshake:
            self.assertTrue(self.base.start()['running'])
        self.assertEqual(2.0, handshake.call_args.args[-1])

    def windows_preparation_expiry(self, boundary):
        adapter = runtime_process
        process = adapter.OwnedProcess()
        original = adapter.subprocess.list2cmdline

        def expire(at, value):
            if at == boundary:
                self.clock[0] = 2.0
            return value

        kernel = Mock(CreateJobObjectW=Mock(side_effect=lambda *_: expire('job', 126)))
        with patch.object(self.base.adapter, 'OwnedProcess', return_value=process), \
             patch.object(adapter, 'security_descriptor', side_effect=lambda: expire('descriptor', nullcontext(None))), \
             patch.object(adapter, 'kernel', kernel), \
             patch.object(adapter.ctypes, 'get_last_error', return_value=0), \
             patch.object(adapter, 'open', return_value=nullcontext(Mock(fileno=lambda: 11)), create=True), \
             patch.object(adapter.msvcrt, 'get_osfhandle', side_effect=lambda fd: fd + 100), \
             patch.object(adapter.os, 'set_handle_inheritable', side_effect=lambda h, flag: expire('handles', None) if flag else None) as inherit, \
             patch.object(adapter.subprocess, 'list2cmdline', side_effect=lambda args: expire('command', original(args))), \
             patch.object(adapter._winapi, 'CreateProcess', side_effect=AssertionError('期限後のprocess生成')) as create, \
             patch.object(adapter._winapi, 'CloseHandle') as close_process:
            self.expired()
        create.assert_not_called()
        kernel.AssignProcessToJobObject.assert_not_called()
        kernel.ResumeThread.assert_not_called()
        kernel.CloseHandle.assert_not_called()
        close_process.assert_not_called()
        self.assertEqual((126, None, None, None, 'JOB_CREATED'),
                         (process.job, process.handle, process.thread, process.pid, process.stage))
        self.assertIs(process, next(iter(self.runtime.processes.values())))
        enabled = [call.args[0] for call in inherit.call_args_list if call.args[1]]
        disabled = [call.args[0] for call in inherit.call_args_list if not call.args[1]]
        self.assertTrue(enabled)
        self.assertEqual(enabled, disabled)
        with self.base.registry.transaction(read_only=True) as db:
            run_id = db.execute("SELECT run_id FROM instances WHERE id=?", (self.base.token['instanceId'],)).fetchone()[0]
        creation = self.runtime._load_run({'run_id': run_id})['creation']
        self.assertEqual({'pid': None, 'stage': 'JOB_CREATED', 'ownerGroup': process.group}, creation)

    @unittest.skipUnless(os.name == 'nt', '合成Windows生成境界')
    def test_expiry_during_security_preparation_keeps_job_without_process(self):
        self.windows_preparation_expiry('descriptor')

    @unittest.skipUnless(os.name == 'nt', '合成Windows生成境界')
    def test_expiry_during_job_preparation_keeps_job_without_process(self):
        self.windows_preparation_expiry('job')

    @unittest.skipUnless(os.name == 'nt', '合成Windows生成境界')
    def test_expiry_during_handle_preparation_restores_inheritance_without_process(self):
        self.windows_preparation_expiry('handles')

    @unittest.skipUnless(os.name == 'nt', '合成Windows生成境界')
    def test_expiry_during_command_preparation_has_no_process_effect(self):
        self.windows_preparation_expiry('command')

    def test_expiry_after_ack_preserves_owned_process(self):
        with patch.object(self.runtime.managed, 'handshake', side_effect=lambda *_: self.clock.__setitem__(0, 2.0)), \
             patch.object(self.runtime.managed, 'listen') as listen:
            self.expired(created=1)
        listen.assert_not_called()

    def test_expiry_before_proof_record_does_not_write_success(self):
        original = self.base.adapter.sockets
        def sockets(pid):
            result = original(pid)
            self.clock[0] = 2.0
            return result
        with patch.object(self.base.adapter, 'sockets', side_effect=sockets):
            self.expired(created=1)
        self.assertEqual([], list(self.base.root.glob('instances/*/diagnostics/*/startup-proof.json')))

    def test_expiry_after_proof_record_preserves_proof_without_return(self):
        original = control.write_json
        def write(path, value):
            original(path, value)
            if path.name == 'startup-proof.json':
                self.clock[0] = 2.0
        with patch.object(control, 'write_json', side_effect=write), \
             patch.object(self.runtime.managed, 'ready', wraps=self.runtime.managed.ready) as ready:
            self.expired(created=1)
        ready.assert_not_called()
        self.assertEqual(1, len(list(self.base.root.glob('instances/*/diagnostics/*/startup-proof.json'))))

    def test_expiry_before_final_gate_open_rolls_back(self):
        with patch.object(self.runtime.managed, 'ready', side_effect=lambda *_: self.clock.__setitem__(0, 2.0)), \
             patch.object(control, '_change', wraps=control._change) as change:
            self.expired(created=1)
        self.assertFalse(any(call.args[-1] == 'OPEN' for call in change.call_args_list))
        with self.base.registry.transaction(read_only=True) as db:
            self.assertEqual('BLOCKED', db.execute('SELECT gate FROM run_admission').fetchone()[0])

    def test_expiry_during_result_encoding_rolls_back(self):
        original = control.json.dumps
        def dumps(value, *args, **kwargs):
            result = original(value, *args, **kwargs)
            if type(value) is dict and value.get('running') is True and value.get('snapshotClean') is False:
                self.clock[0] = 2.0
            return result
        with patch.object(control.json, 'dumps', side_effect=dumps):
            self.expired(created=1)

    def test_commit_crossing_deadline_keeps_completed_result_and_refuses_return(self):
        original = self.runtime._transaction
        committed, resume = threading.Event(), threading.Event()
        @contextmanager
        def transaction(instance, **kwargs):
            completed = False
            with original(instance, **kwargs) as db:
                yield db
                row = db.execute("SELECT status FROM runtime_operations WHERE id='start-01'").fetchone()
                completed = bool(row and row[0] == 'COMPLETED')
            if completed and not kwargs.get('read_only'):
                committed.set()
                self.assertTrue(resume.wait(10))
        with patch.object(self.runtime, '_transaction', side_effect=transaction), ThreadPoolExecutor() as workers:
            future = workers.submit(self.base.start)
            try:
                self.assertTrue(committed.wait(10))
                with self.base.registry.transaction(read_only=True) as db:
                    self.assertEqual('COMPLETED', db.execute("SELECT status FROM runtime_operations WHERE id='start-01'").fetchone()[0])
                self.clock[0] = 2.0
            finally:
                resume.set()
            self.expired(future.result, created=1, committed=True)
        self.assertEqual(1, len(self.base.adapter.created))

    def test_replay_expiry_does_not_respawn_or_downgrade(self):
        self.base.start()
        original = self.runtime._running
        def running(*args, **kwargs):
            value = original(*args, **kwargs)
            self.clock[0] = 2.0
            return value
        with patch.object(self.runtime, '_running', side_effect=running):
            self.expired(created=1, committed=True)

    def test_pending_start_excludes_competing_start_until_expiry(self):
        original = control.launch_command
        entered, resume = threading.Event(), threading.Event()
        def launch(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            self.assertTrue(resume.wait(10))
            return result
        with patch.object(control, 'launch_command', side_effect=launch), ThreadPoolExecutor() as workers:
            future = workers.submit(self.base.start)
            try:
                self.assertTrue(entered.wait(10))
                self.base.assert_code('INVALID_STATE', lambda: self.base.start('competing-start'))
                self.base.assert_code('WRITER_PENDING', lambda: self.runtime._offline_open(self.base.token,
                    operation_id='competing-offline', revision=0, ready=dict(
                        worldUuid='00000000-0000-4000-8000-000000000001',
                        runUuid='00000000-0000-4000-8000-000000000002', stateSha256='a' * 64, stateRevision=0),
                    deadline=2.0))
                with self.base.registry.transaction(read_only=True) as db:
                    self.assertEqual('PENDING', db.execute("SELECT status FROM runtime_operations WHERE id='start-01'").fetchone()[0])
                    self.assertIsNone(db.execute("SELECT 1 FROM runtime_operations WHERE id='competing-start'").fetchone())
                self.clock[0] = 2.0
            finally:
                resume.set()
            self.expired(future.result)

    def test_instance_lock_conflict_preserves_immediate_rejection(self):
        from .supervisor import dispatch, new_request, private_lock
        with private_lock(self.base.root / 'manager/locks/instance-persistent-01.lock'), \
             patch.object(control.time, 'sleep', side_effect=lambda _: self.clock.__setitem__(0, 2.0)) as sleep:
            response = dispatch(self.base.registry, new_request('start',
                dict(self.base.token, timeoutSeconds=2), operation_id='start-01'))
        self.assertEqual('LOCK_BUSY', response['error']['code'])
        sleep.assert_not_called()
        self.assertEqual([], self.base.adapter.created)
        with self.base.registry.transaction(read_only=True) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM runtime_operations WHERE id='start-01'").fetchone())


class StartupDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.base = startup_tests.StartupTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.clock = [0.0]
        self.enterContext(patch.object(startup.time, 'monotonic', side_effect=lambda: self.clock[0]))

    def test_expiry_before_grant_send_keeps_record_without_sending(self):
        original = startup._Startup._save
        def save(owner, *args):
            result = original(owner, *args)
            self.clock[0] = 12.0
            return result
        with patch.object(startup._Startup, '_save', new=save), self.assertRaises(RegistryError) as caught:
            self.base.start()
        self.assertEqual('START_TIMEOUT', caught.exception.code)
        self.assertEqual([], self.base.sent)
        with self.base.transaction(read_only=True) as db:
            self.assertEqual(self.base.frames[0][1], startup.operations.readback(db, 'slot-a', self.base.frames[0][0]))

    def test_expiry_before_ack_commit_does_not_open_gate(self):
        original = startup._Startup._row
        def row(owner, *args):
            result = original(owner, *args)
            if len(self.base.sent) == 2:
                self.clock[0] = 12.0
            return result
        with patch.object(startup._Startup, '_row', new=row), \
             patch.object(startup, '_change', wraps=startup._change) as change, \
             self.assertRaises(RegistryError) as caught:
            self.base.start()
        self.assertEqual('START_TIMEOUT', caught.exception.code)
        self.assertFalse(any(call.args[-1] == 'OPEN' for call in change.call_args_list))
        self.assertEqual('BLOCKED', self.base.gate()['gate'])

    def test_expiry_before_ack_record_keeps_grant_and_sends_no_ack(self):
        original = startup._reply
        def reply(request, context, phase):
            result = original(request, context, phase)
            if phase == 'ACK':
                self.clock[0] = 12.0
            return result
        with patch.object(startup, '_reply', side_effect=reply), self.assertRaises(RegistryError) as caught:
            self.base.start()
        self.assertEqual('START_TIMEOUT', caught.exception.code)
        self.assertEqual(1, len(self.base.sent))
        self.assertEqual('BLOCKED', self.base.gate()['gate'])
        with self.base.transaction(read_only=True) as db:
            self.assertIsNone(startup.operations.readback(db, 'slot-a', self.base.frames[1][0]))

    def test_expiry_before_ack_send_keeps_recorded_ack(self):
        original = startup._Startup._save
        def save(owner, *args):
            result = original(owner, *args)
            if len(self.base.sent) == 1:
                self.clock[0] = 12.0
            return result
        with patch.object(startup._Startup, '_save', new=save), self.assertRaises(RegistryError) as caught:
            self.base.start()
        self.assertEqual('START_TIMEOUT', caught.exception.code)
        self.assertEqual(1, len(self.base.sent))
        self.assertEqual('BLOCKED', self.base.gate()['gate'])
        with self.base.transaction(read_only=True) as db:
            self.assertEqual(self.base.frames[1][1], startup.operations.readback(db, 'slot-a', self.base.frames[1][0]))

    def test_expiry_after_ack_send_keeps_wire_proof_without_ready(self):
        original = self.base.send
        def send(raw, deadline):
            result = original(raw, deadline)
            if len(self.base.sent) == 2:
                self.clock[0] = 12.0
            return result
        self.base.channel.send_frame.side_effect = send
        with self.assertRaises(RegistryError) as caught:
            self.base.start(defer_ready=True)
        self.assertEqual('START_TIMEOUT', caught.exception.code)
        self.assertEqual(2, len(self.base.sent))
        self.assertEqual('BLOCKED', self.base.gate()['gate'])
        self.assertFalse(self.base.runtime._control.sessions['slot-a'].get('acknowledged', False))

    def test_expiry_during_ack_result_encoding_rolls_back_completion(self):
        original = startup.json.dumps
        def dumps(value, *args, **kwargs):
            raw = original(value, *args, **kwargs)
            if type(value) is dict and value.get('running') is True:
                self.clock[0] = 12.0
            return raw
        with patch.object(startup.json, 'dumps', side_effect=dumps), self.assertRaises(RegistryError) as caught:
            self.base.start()
        self.assertEqual('START_TIMEOUT', caught.exception.code)
        self.assertEqual('BLOCKED', self.base.gate()['gate'])
        self.assertEqual('RECOVERY_REQUIRED', self.base.db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()[0])

    def test_ack_commit_crossing_deadline_keeps_completed_result(self):
        original = self.base.runtime._transaction
        @contextmanager
        def transaction(instance, **kwargs):
            with original(instance, **kwargs) as db:
                yield db
                row = db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()
                completed = row[0] == 'COMPLETED'
            if completed:
                self.clock[0] = 12.0
        with patch.object(self.base.runtime, '_transaction', side_effect=transaction), self.assertRaises(RegistryError) as caught:
            self.base.start()
        self.assertEqual('START_TIMEOUT', caught.exception.code)
        self.assertEqual('OPEN', self.base.gate()['gate'])
        self.assertEqual('COMPLETED', self.base.db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()[0])

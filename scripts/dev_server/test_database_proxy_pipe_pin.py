"""子processを生成せず、元pipeの所有pinとcloseの排他を検証する。"""
from concurrent.futures import ThreadPoolExecutor
from copy import copy
import importlib.util
import os
import stat
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import database_proxy_pipe as target


class PipePinTests(unittest.TestCase):
    def setUp(self):
        self.pipe = target.ProxyPipe()
        self.pins = self.pipe.pins
        self.live = {fd for pin in self.pins for fd in (pin.fd, pin.pin)}
        self.closed = []
        self.real_close = os.close
        self.addCleanup(self.cleanup)
        self.enterContext(patch.object(target.os, 'close', side_effect=self.close))

    def close(self, fd):
        self.real_close(fd)
        self.live.discard(fd)
        self.closed.append(fd)

    def cleanup(self):
        for fd in self.live:
            self.real_close(fd)

    def after_observation(self, change):
        calls = 0
        def verify():
            nonlocal calls
            calls += 1
            if calls == 2:
                change()
        return verify

    def test_all_creation_pins_are_distinct_non_inheritable_and_same_entity(self):
        self.assertEqual(8, len(self.live))
        self.assertEqual(('child_input', 'child_output', 'input', 'output'), tuple(p.name for p in self.pins))
        for pin in self.pins:
            self.assertFalse(os.get_inheritable(pin.fd))
            self.assertFalse(os.get_inheritable(pin.pin))
            self.assertTrue(target._same_pipe(pin.fd, pin.pin))
            self.assertEqual((pin.name, pin.fd, pin.pin), pin.binding)
            self.assertIs(self.pipe, pin.owner)
        self.pipe.verify_owned()

    def test_copied_pipe_cannot_adopt_original_pins(self):
        other = copy(self.pipe)
        for operation in (other.verify_owned, other.close_after_exit):
            with self.assertRaises(OSError):
                operation()
        self.assertEqual([], self.closed)
        self.pipe.verify_owned()

    def test_callback_cannot_replace_creation_owner_before_close(self):
        def changed():
            self.pins[0]._owner = object()
        with self.assertRaises(OSError):
            self.pipe.close_after_exit(verify=self.after_observation(changed))
        self.assertEqual([], self.closed)

    def test_normal_close_releases_each_original_then_its_pin_once(self):
        expected = [fd for pin in self.pins for fd in (pin.fd, pin.pin)]
        self.pipe.close_after_exit()
        self.pipe.close_after_exit()
        self.assertEqual(expected, self.closed)
        self.assertEqual(['RELEASED'] * 4, [p.state for p in self.pins])
        self.assertFalse(self.live)
        self.pipe.verify_owned()

    def test_child_close_does_not_leave_hidden_write_pin_or_delay_eof(self):
        self.pipe.close_child_ends()
        self.assertEqual(4, len(self.closed))
        self.assertEqual(b'', os.read(self.pipe.input, 1))
        with self.assertRaises(OSError):
            os.write(self.pipe.output, b'x')
        self.assertEqual(['RELEASED', 'RELEASED', 'OPEN', 'OPEN'], [p.state for p in self.pins])
        self.pipe.close_after_exit()
        self.assertFalse(self.live)

    def test_reused_original_fd_is_rejected_without_closing_replacement(self):
        read, write = os.pipe()
        self.live.update((read, write))
        original = self.pins[0].fd
        os.dup2(read, original, inheritable=False)
        with self.assertRaises(OSError):
            self.pipe.close_after_exit()
        self.assertEqual([], self.closed)
        self.assertEqual(original, self.pipe.child_input)
        self.assertFalse(target._same_pipe(original, self.pins[0].pin))

    def test_other_end_of_same_pipe_is_not_adopted(self):
        self.assertFalse(target._same_pipe(self.pipe.child_input, self.pipe.output))

    def test_unavailable_identity_api_does_not_fall_back_to_numbers(self):
        with patch.object(target, '_same_pipe', side_effect=OSError('API利用不能')):
            with self.assertRaisesRegex(OSError, 'API利用不能'):
                self.pipe.close_after_exit()
        self.assertEqual([], self.closed)
        self.assertEqual(['OPEN'] * 4, [p.state for p in self.pins])

    def test_pinless_holding_slot_is_rejected(self):
        self.pipe._pins = ()
        for operation in (self.pipe.verify_owned, self.pipe.close_after_exit):
            with self.assertRaises(OSError):
                operation()
        self.assertEqual([], self.closed)

    def test_callback_cannot_replace_pin_before_close(self):
        pin = self.pins[0]
        def changed():
            pin.pin = self.pins[1].pin
        with self.assertRaises(OSError):
            self.pipe.close_after_exit(verify=self.after_observation(changed))
        self.assertEqual([], self.closed)

    def test_callback_cannot_replace_original_binding_before_close(self):
        pin = self.pins[0]
        def changed():
            pin.fd = self.pins[1].fd
        with self.assertRaises(OSError):
            self.pipe.close_after_exit(verify=self.after_observation(changed))
        self.assertEqual([], self.closed)

    def test_callback_cannot_replace_pin_group_before_close(self):
        def changed():
            self.pipe._pins = tuple(list(self.pins))
        with self.assertRaises(OSError):
            self.pipe.close_after_exit(verify=self.after_observation(changed))
        self.assertEqual([], self.closed)

    def test_callback_cannot_replace_original_fd_before_close(self):
        def changed():
            self.pipe.child_input = self.pins[1].fd
        with self.assertRaises(OSError):
            self.pipe.close_after_exit(verify=self.after_observation(changed))
        self.assertEqual([], self.closed)

    def test_callback_failure_before_original_close_has_no_effect(self):
        def refused():
            raise TimeoutError('同一期限')
        with self.assertRaises(TimeoutError):
            self.pipe.close_after_exit(verify=refused)
        self.assertEqual([], self.closed)

    def test_callback_failure_after_original_close_retains_pin(self):
        calls = 0
        def refused():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise TimeoutError('同一期限')
        with self.assertRaises(TimeoutError):
            self.pipe.close_after_exit(verify=refused)
        self.assertEqual([self.pins[0].fd], self.closed)
        self.assertIsNone(self.pipe.child_input)
        self.assertEqual('PIN_PENDING', self.pins[0].state)
        self.assertIn(self.pins[0].pin, self.live)
        with self.assertRaises(OSError):
            self.pipe.close_after_exit()
        self.assertEqual([self.pins[0].fd], self.closed)

    def test_observation_deadline_prevents_original_close(self):
        expired = False
        same = target._same_pipe
        def observe(fd, pin):
            nonlocal expired
            result = same(fd, pin)
            expired = True
            return result
        def verify():
            if expired:
                raise TimeoutError('観測中に同一期限へ到達')
        with patch.object(target, '_same_pipe', side_effect=observe):
            with self.assertRaises(TimeoutError):
                self.pipe.close_after_exit(verify=verify)
        self.assertEqual([], self.closed)
        self.assertEqual(['OPEN'] * 4, [p.state for p in self.pins])

    def test_pin_close_deadline_prevents_next_native_observation(self):
        expired = False
        observed = []
        same = target._same_pipe
        def observe(fd, pin):
            observed.append(fd)
            return same(fd, pin)
        def close(fd):
            nonlocal expired
            self.close(fd)
            if fd == self.pins[0].binding[2]:
                expired = True
        def verify():
            if expired:
                raise TimeoutError('pin解放中に同一期限へ到達')
        with patch.object(target, '_same_pipe', side_effect=observe), patch.object(target.os, 'close', side_effect=close):
            for _ in range(2):
                with self.assertRaises(TimeoutError):
                    self.pipe.close_after_exit(verify=verify)
        self.assertEqual([self.pins[0].fd], observed)
        self.assertEqual([self.pins[0].fd, self.pins[0].binding[2]], self.closed)
        self.assertIsNone(self.pipe.child_input)
        self.assertEqual(['RELEASED', 'OPEN', 'OPEN', 'OPEN'], [p.state for p in self.pins])

    def test_last_callback_cannot_change_an_already_released_binding(self):
        def changed():
            if self.pins[-1].state == 'PIN_PENDING':
                self.pipe.child_input = self.pins[0].fd
        with self.assertRaises(OSError):
            self.pipe.close_after_exit(verify=changed)
        self.assertFalse(self.live)
        before = list(self.closed)
        with self.assertRaises(OSError):
            self.pipe.close_after_exit()
        self.assertEqual(before, self.closed)

    def test_original_close_exception_is_unknown_and_never_retried(self):
        fd = self.pins[0].fd
        def failed(value):
            if value == fd:
                raise OSError('close作用不明')
            self.close(value)
        with patch.object(target.os, 'close', side_effect=failed):
            with self.assertRaises(OSError):
                self.pipe.close_after_exit()
        self.assertEqual('UNKNOWN', self.pins[0].state)
        with self.assertRaises(OSError):
            self.pipe.close_after_exit()
        self.assertEqual([], self.closed)

    def test_pin_close_exception_is_unknown_and_preserves_identifier(self):
        held = self.pins[0].pin
        def failed(value):
            if value == held:
                raise OSError('pin close作用不明')
            self.close(value)
        with patch.object(target.os, 'close', side_effect=failed):
            with self.assertRaises(OSError):
                self.pipe.close_after_exit()
        self.assertEqual('UNKNOWN', self.pins[0].state)
        self.assertEqual(held, self.pins[0].pin)
        with self.assertRaises(OSError):
            self.pipe.close_after_exit()
        self.assertEqual([self.pins[0].fd], self.closed)

    def test_close_exception_after_actual_effect_does_not_repeat_old_fd(self):
        for role in ('fd', 'pin'):
            with self.subTest(role=role):
                pipe = target.ProxyPipe()
                pin = pipe.pins[0]
                self.live.update(fd for p in pipe.pins for fd in (p.fd, p.pin))
                value = getattr(pin, role)
                def failed(fd):
                    self.close(fd)
                    if fd == value:
                        raise OSError('正常close後の返却喪失')
                with patch.object(target.os, 'close', side_effect=failed):
                    with self.assertRaises(OSError):
                        pipe.close_after_exit()
                before = list(self.closed)
                with self.assertRaises(OSError):
                    pipe.close_after_exit()
                self.assertEqual(before, self.closed)
                self.assertEqual('UNKNOWN', pin.state)

    def test_concurrent_close_and_observation_refuse_without_duplicate_effect(self):
        entered, resume = threading.Event(), threading.Event()
        def wait_once():
            if not entered.is_set():
                entered.set()
                if not resume.wait(5):
                    raise TimeoutError('試験の待機期限')
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.pipe.close_after_exit, wait_once)
            self.assertTrue(entered.wait(5))
            try:
                for operation in (self.pipe.close_after_exit, self.pipe.close_child_ends, self.pipe.verify_owned):
                    with self.assertRaises(OSError):
                        operation()
                self.assertEqual([], self.closed)
            finally:
                resume.set()
            first.result(timeout=5)
        self.assertEqual(8, len(self.closed))
        self.assertFalse(self.live)


class PinCreationTests(unittest.TestCase):
    def test_duplicate_failure_releases_only_newly_created_descriptors(self):
        created = []
        pipe, dup = os.pipe, os.dup
        def allocate():
            result = pipe()
            created.extend(result)
            return result
        def duplicate(fd):
            if len(created) == 6:
                raise OSError('pin取得失敗')
            result = dup(fd)
            created.append(result)
            return result
        with patch.object(target.os, 'pipe', side_effect=allocate), patch.object(target.os, 'dup', side_effect=duplicate):
            with self.assertRaises(OSError) as caught:
                target.ProxyPipe()
        self.assertIsNotNone(caught.exception.pipe)
        for fd in created:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_inheritable_duplicate_is_rejected_before_publication(self):
        with patch.object(target.os, 'get_inheritable', return_value=True):
            with self.assertRaises(OSError) as caught:
                target.ProxyPipe()
        self.assertTrue(all(p.state == 'RELEASED' for p in caught.exception.pipe.pins))

    def test_setup_cleanup_failure_keeps_original_object_and_unknown_progress(self):
        close, created = os.close, []
        pipe = os.pipe
        def allocate():
            result = pipe()
            created.extend(result)
            return result
        with patch.object(target.os, 'pipe', side_effect=allocate), \
                patch.object(target.os, 'get_inheritable', return_value=True), \
                patch.object(target.os, 'close', side_effect=OSError('整理作用不明')):
            with self.assertRaisesRegex(OSError, '整理作用不明') as caught:
                target.ProxyPipe()
        held = caught.exception.pipe
        self.assertEqual('UNKNOWN', held.pins[0].state)
        self.assertEqual(created[0], held.child_input)
        try:
            with patch.object(target.os, 'close') as retry:
                with self.assertRaises(OSError):
                    held.close_after_exit()
                retry.assert_not_called()
        finally:
            for fd in [*created, *(p.pin for p in held.pins if p.pin is not None)]:
                close(fd)

    @unittest.skipUnless(os.name == 'nt', 'Windows API境界です')
    def test_windows_api_absence_refuses_without_numeric_fallback(self):
        with patch.object(target.ctypes, 'WinDLL', return_value=SimpleNamespace()):
            with self.assertRaisesRegex(OSError, '照合API'):
                target._same_pipe(1, 1)


class PosixIdentityTests(unittest.TestCase):
    def setUp(self):
        self.fcntl = SimpleNamespace(F_GETFL=3, fcntl=lambda fd, command: fd % 2)
        self.enterContext(patch.object(os, 'O_ACCMODE', 3, create=True))
        spec = importlib.util.spec_from_file_location('dev_server.pipe_posix_fixture', target.__file__)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, fcntl=self.fcntl), patch.object(os, 'name', 'posix'):
            spec.loader.exec_module(self.module)
        self.observed = SimpleNamespace(st_mode=stat.S_IFIFO, st_dev=0, st_ino=123)

    def test_held_fifo_inode_and_access_direction_are_required(self):
        with patch.object(self.module.os, 'fstat', return_value=self.observed):
            self.assertTrue(self.module._same_pipe(2, 4))
            self.assertFalse(self.module._same_pipe(2, 3))
        for changed in (dict(st_ino=0), dict(st_ino=124), dict(st_mode=stat.S_IFREG)):
            other = SimpleNamespace(**(vars(self.observed) | changed))
            with self.subTest(changed=changed), patch.object(self.module.os, 'fstat', side_effect=[self.observed, other]):
                self.assertFalse(self.module._same_pipe(2, 4))

    def test_missing_inode_evidence_is_not_a_positive_match(self):
        self.observed.st_ino = 0
        with patch.object(self.module.os, 'fstat', return_value=self.observed):
            self.assertFalse(self.module._same_pipe(2, 4))

    def test_unavailable_stat_is_not_replaced_by_fd_equality(self):
        with patch.object(self.module.os, 'fstat', side_effect=OSError('観測不能')):
            with self.assertRaises(OSError):
                self.module._same_pipe(2, 2)


class PipeCallerTests(unittest.TestCase):
    def failed_creation(self):
        close = os.close
        def create():
            with patch.object(target.os, 'get_inheritable', return_value=True), \
                    patch.object(target.os, 'close', side_effect=OSError('生成途中の整理不明')):
                try:
                    return target.ProxyPipe()
                except OSError as error:
                    pipe = error.pipe
                    fds = {getattr(pipe, name) for name in ('child_input', 'child_output', 'input', 'output')}
                    fds.update(pin.pin for pin in pipe.pins)
                    for fd in fds - {None}:
                        self.addCleanup(close, fd)
                    raise
        return create

    def test_offline_creation_unknown_retains_original_pipe_without_respawn(self):
        from . import offline_process
        from .test_offline_process import OfflineProcessTests, frame
        from .registry import RegistryError
        fixture = OfflineProcessTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        helper = fixture.helper()
        with patch.object(offline_process, 'ProxyPipe', side_effect=self.failed_creation()):
            with self.assertRaises(OSError) as caught:
                helper._launch(frame(), 'open', fixture.verify)
        original = caught.exception.pipe
        self.assertEqual('生成途中の整理不明', str(caught.exception))
        self.assertIs(original, helper.pipe)
        self.assertEqual((helper.process, original), helper.creation_resources)
        self.assertEqual('UNKNOWN', original.pins[0].state)
        self.assertEqual(0, helper.process.starts)
        self.assertEqual(1, len(list(helper.diagnostics.glob('creation-*.json'))))
        with self.assertRaises(RegistryError):
            helper.start(frame(), 10)
        self.assertEqual(0, helper.process.starts)

    def test_database_creation_unknown_retains_original_pipe_without_respawn(self):
        from . import database_proxy
        from .test_database_proxy import DatabaseProxyTests
        from .registry import RegistryError
        fixture = DatabaseProxyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.object(database_proxy, 'proxy_command', return_value=fixture.command), \
                patch.object(database_proxy, 'ProxyPipe', side_effect=self.failed_creation()):
            with self.assertRaises(OSError) as caught:
                fixture.proxy.start()
        original = caught.exception.pipe
        self.assertEqual('生成途中の整理不明', str(caught.exception))
        self.assertIs(original, fixture.proxy.pipe)
        self.assertEqual('UNKNOWN', original.pins[0].state)
        self.assertEqual(0, fixture.process.starts)
        self.assertEqual(1, len(list(fixture.proxy.diagnostics.glob('creation-*.json'))))
        with self.assertRaises(RegistryError):
            fixture.proxy.start()
        self.assertEqual(0, fixture.process.starts)

    def test_original_stdio_and_child_pin_release_use_synthetic_process_only(self):
        from . import offline_process
        from .test_offline_process import OfflineProcessTests, frame
        fixture = OfflineProcessTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.object(offline_process, 'ProxyPipe', target.ProxyPipe):
            helper = fixture.helper()
            request = helper._launch(frame(), 'open', fixture.verify)
        self.addCleanup(helper.pipe.close_after_exit)
        self.assertEqual('open', request['command'])
        self.assertEqual(1, helper.process.starts)
        self.assertIs(helper.pipe, helper.creation_resources[1])
        self.assertIs(helper.pipe.pins, helper.pipe_pins)
        self.assertEqual(['RELEASED', 'RELEASED', 'OPEN', 'OPEN'], [p.state for p in helper.pipe.pins])
        self.assertEqual(helper.pipe.pins[0].fd, helper.process.stdio['input_fd'])
        self.assertEqual(helper.pipe.pins[1].fd, helper.process.stdio['output_fd'])
        self.assertNotIn('pin', helper.process.stdio)
        helper.pipe.verify_owned()

    def test_registry_commit_while_caller_verifies_starts_no_pipe_close(self):
        from .test_offline_owner_release import OfflineOwnerReleaseTests
        from .registry import RegistryError
        fixture = OfflineOwnerReleaseTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        pipe = target.ProxyPipe()
        self.addCleanup(pipe.close_after_exit)
        original = pipe.pins
        saved = fixture.f.saved()
        entered, resume = threading.Event(), threading.Event()
        registry = fixture.runtime.registry
        def verify():
            entered.set()
            if not resume.wait(5):
                raise TimeoutError('試験の待機期限')
            with registry.transaction(read_only=True) as db:
                row = db.execute('SELECT * FROM instances WHERE id=?', (fixture.token['instanceId'],)).fetchone()
                registry._token(row, fixture.token)
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(pipe.close_after_exit, verify)
            try:
                self.assertTrue(entered.wait(5))
                with registry.transaction() as db:
                    db.execute('UPDATE instances SET epoch=epoch+1 WHERE id=?', (fixture.token['instanceId'],))
            finally:
                resume.set()
            with self.assertRaises(RegistryError):
                request.result(timeout=5)
        self.assertIs(original, pipe.pins)
        self.assertEqual(['OPEN'] * 4, [p.state for p in pipe.pins])
        pipe.verify_owned()
        self.assertEqual(saved, fixture.f.saved())

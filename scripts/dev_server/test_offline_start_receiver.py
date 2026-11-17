"""offline起動確定を既存の監督・停止制御へ接続する。実Mは生成しない。"""
import json
from pathlib import Path
from queue import Queue
import threading
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import mod_control_runtime, mod_control_wire as wire, runtime_control
from .build_inputs import canonical
from .registry import RegistryError
from . import test_offline_start_running as running
from . import test_mod_control_runtime as controls


class StartReceiverTests(unittest.TestCase):
    def setUp(self):
        self.f = running.StartRunningTests()
        self.f.real_receiver = True
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.runtime, self.b = self.f.runtime, self.f.b
        self.managed = self.runtime.managed
        self.f.f.creator.m.members = Mock(return_value=set())
        self.entered = threading.Event()
        self.requests = Queue()
        self.sent = []
        self.thread = None
        self.enterContext(patch.object(mod_control_runtime, 'time',
            SimpleNamespace(monotonic=lambda: self.b.base.f.clock, sleep=time.sleep)))
        self.enterContext(patch.object(runtime_control.time, 'sleep', time.sleep))
        listen = self.managed.listen

        def connected(instance, **options):
            connection = options['channel']
            def receive(deadline, **kwargs):
                self.entered.set()
                value = self.requests.get(timeout=10)
                if isinstance(value, BaseException):
                    raise value
                return value
            self.enterContext(patch.object(connection, 'receive_frame', side_effect=receive))
            self.enterContext(patch.object(connection, 'send_frame', side_effect=self.send))
            self.enterContext(patch.object(connection, 'wait_peer_close', side_effect=self.exit))
            self.enterContext(patch.object(connection.endpoint, 'close', return_value=True))
            self.thread = listen(instance, **options)
            return self.thread
        self.enterContext(patch.object(self.managed, 'listen', side_effect=connected))
        self.addCleanup(self.finish_receiver)

    def finish_receiver(self):
        if self.thread is not None:
            if self.thread.is_alive():
                self.requests.put(TimeoutError('合成受信の試験終了'))
            self.thread.join(5)
            self.assertFalse(self.thread.is_alive())

    def start(self):
        result, packet = self.f.record()
        self.assertTrue(self.entered.wait(5))
        self.session = packet[0]
        self.assertIs(self.managed.sessions[self.b.token['instanceId']], self.session)
        self.assertEqual(self.b.token, self.session['token'])
        self.assertIs(self.f.f.creator.m, self.runtime.game_processes[result['runId']][0])
        self.assertIs(self.thread, self.session['thread'])
        self.assertIsNone(self.session['error'])
        return result, packet

    def send(self, raw, deadline):
        self.sent.append(wire.decode_frame_v3(raw)['phase'])
        return len(raw)

    def exit(self, deadline):
        path = Path(self.f.f.held()['creation_run']['proofPath'])
        value = json.loads(path.read_bytes())
        value.update(phase='STOPPED', modSaveCompleted=True, vanillaShutdownTailReached=True,
            saves=dict(failures=[], pendingWriters=0, drained=True, success=True,
                results={key: True for key in ('management-drain', 'money-history', 'shop-history',
                                               'shop-stocks', 'player-data')}))
        path.write_bytes(canonical(value))
        self.f.f.creator.m.poll.return_value = 0
        self.f.f.creator.m.members.return_value = set()
        return True

    def test_public_status_and_refresh_consume_same_running_session(self):
        result, packet = self.start()
        status = self.runtime.registry.execute('status', {'instanceId': self.b.token['instanceId']},
                                               operation_id='offline-public-status')
        self.assertIn(result['runId'], json.dumps(status))
        self.runtime.refresh()
        self.assertEqual(('RUNNING', 0), (self.b.row()['state'], self.b.row()['pending']))
        self.assertFalse(self.session['done'].is_set())

    def test_public_stop_waits_for_existing_serve_done_and_completes(self):
        self.start()
        held = self.f.f.held()
        frames = controls.ManagedRuntimeTests.stop_frames(SimpleNamespace(session=self.session,
            fixture=SimpleNamespace(context=self.session['context'], frames=[None, (held['acknowledged'][0], None)])))
        def stop(run, generation, process, timeout, *, expected):
            self.requests.put(frames[0])
            self.requests.put(frames[1])
            run.update(stopRequested=True, stopAcknowledged=True)
            return self.runtime._record(run, expected=expected)
        with patch.object(self.runtime, '_rcon_stop', side_effect=stop), \
                patch.object(self.runtime, '_stop_handle_release'):
            try:
                result = self.runtime.registry.execute('stop', dict(self.b.token, timeoutSeconds=30),
                                                      operation_id='offline-public-stop')
            except BaseException as error:
                error.add_note('元受信thread: ' + ''.join(traceback.format_exception(self.session['error'])))
                raise
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(self.session['done'].is_set())
        self.assertIsNone(self.session['error'])
        self.assertEqual(['BLOCKED', 'COMPLETION'], self.sent)
        self.assertTrue(result['runtimeStopped'])
        self.assertEqual(('LEASED_STOPPED', 0), (self.b.row()['state'], self.b.row()['pending']))

    def stopping(self):
        self.start()
        path = Path(self.f.f.held()['creation_run']['proofPath'])
        value = json.loads(path.read_bytes())
        value['phase'] = 'STOPPING'
        path.write_bytes(canonical(value))

    def test_stopping_proof_enters_existing_running_supervision(self):
        self.stopping()
        self.runtime._running(self.b.row(), observe_stop=True)
        self.assertEqual(('STOPPING', 1), (self.b.row()['state'], self.b.row()['pending']))
        self.assertTrue(self.session['stop'].is_set())
        self.assertIsNone(self.session['error'])

    def test_refresh_preserves_legitimate_stopping_proof(self):
        self.stopping()
        self.runtime.refresh()
        self.assertEqual(('STOPPING', 1), (self.b.row()['state'], self.b.row()['pending']))
        self.assertTrue(self.session['stop'].is_set())
        self.assertIsNone(self.session['error'])

    def retained(self, committed=False):
        self.finish_receiver()
        self.f.retained(committed)
        self.assertFalse(self.entered.is_set())

    def test_listen_failure_does_not_publish_running(self):
        with patch.object(self.managed, 'listen', side_effect=OSError('受信開始失敗')):
            with self.assertRaises(OSError):
                self.f.record()
        self.retained()

    def test_receiver_start_deadline_does_not_publish_running(self):
        original = self.managed.listen
        def expired(*args, **kwargs):
            result = original(*args, **kwargs)
            self.b.base.f.clock = self.b.base.f.deadline
            return result
        with patch.object(self.managed, 'listen', side_effect=expired):
            with self.assertRaises(RegistryError):
                self.f.record()
        self.retained()

    def test_write_failure_keeps_receiver_and_connection_without_receiving(self):
        with self.f.boundary('inside', lambda: (_ for _ in ()).throw(OSError('確定write失敗'))):
            with self.assertRaises(OSError):
                self.f.record()
        self.retained()

    def test_last_proof_failure_retains_started_receiver_and_original_resources(self):
        def changed():
            Path(self.f.f.held()['creation_run']['proofPath']).write_bytes(b'{}')
        with self.f.boundary('after', changed):
            with self.assertRaises(RegistryError):
                self.f.record()
        self.assertTrue(self.entered.wait(5))
        held = self.f.f.held()
        self.assertFalse(held['returned'])
        self.assertTrue(held['committed'])
        self.assertEqual(self.f.f.proof_raw, held['proof'][0])
        self.f.f.creator.m.close.assert_not_called()
        held['accepted'][0].peer.close.assert_not_called()
        held['accepted'][0].channel.close.assert_not_called()
        self.assertIs(held['accepted'][0], held['startup'][0]['endpoint'].connection)
        self.assertFalse(self.b.saved()[1])
        with self.assertRaises(RegistryError):
            self.f.record()

    def test_post_commit_unknown_keeps_receiver_for_durable_running(self):
        with self.f.boundary('after', lambda: (_ for _ in ()).throw(OSError('commit応答喪失'))):
            with self.assertRaises(OSError):
                self.f.record()
        self.assertTrue(self.entered.wait(5))
        self.assertFalse(self.f.f.held()['returned'])
        self.assertFalse(self.f.f.held()['committed'])
        self.assertEqual(('RUNNING', 0), (self.b.row()['state'], self.b.row()['pending']))
        self.f.f.creator.m.close.assert_not_called()
        self.f.f.f.f.f.peer.close.assert_not_called()
        self.f.f.f.f.channel.close.assert_not_called()
        self.assertIsNotNone(self.b.base.p.reader.request)
        with self.assertRaises(RegistryError):
            self.f.record()

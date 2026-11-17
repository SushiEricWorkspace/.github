"""元ACKから未公開/INITIALIZING proofの完成を実Registryと合成Mで待つ。"""
from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from . import offline_control, runtime_control, snapshot_data
from .build_inputs import canonical, digest
from .registry import RegistryError
from . import test_offline_start_proof as proofs


class StartProofWaitTests(unittest.TestCase):
    def setUp(self):
        self.f = proofs.StartProofTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.runtime, self.b = self.f.runtime, self.f.b
        self.initial = 'INITIALIZING'
        self.change = lambda value: None
        self.sleeps = []
        self.on_sleep = lambda index: self.publish('STARTED')

        def hook(kind, stage, operation):
            if (kind, stage, operation.round) == ('write', 'finished', 'bound') and self.initial is not None:
                self.publish(self.initial)
        self.f.f.hook = hook

    def publish(self, phase):
        def change(value):
            value['phase'] = phase
            if phase == 'INITIALIZING':
                value['startup'] = {}
            self.change(value)
        with patch.object(self.f, 'change', change):
            self.f.publish_proof()

    def path(self):
        return Path(self.f.held()['creation_run']['proofPath'])

    @contextmanager
    def waiting(self):
        def sleep(delay):
            self.assertEqual(0.1, delay)
            self.assertIn('acknowledged', self.f.held())
            self.sleeps.append(delay)
            self.b.base.f.clock += delay
            self.on_sleep(len(self.sleeps))
        with patch.object(offline_control.time, 'sleep', side_effect=sleep):
            yield

    def reject(self):
        with self.waiting():
            self.f.reject()
        self.assertNotIn('startupProofJson', self.f.saved()[0])

    def test_missing_then_initializing_then_started_keeps_original_packet(self):
        self.initial = None
        self.on_sleep = lambda index: self.publish('INITIALIZING' if index == 1 else 'STARTED')
        with self.waiting():
            result, (_, process, _, verify) = self.f.record()
        held = self.f.held()
        initial = held['proof_initializing']
        self.assertEqual('INITIALIZING', json.loads(initial[0])['phase'])
        self.assertEqual(digest(initial[0]), initial[1])
        self.assertIs(initial, held['proof'][7])
        self.assertIs(process, self.f.creator.m)
        self.assertEqual('OFFLINE_START_PROOF_RECORDED', result['status'])
        self.assertEqual(self.f.proof_raw.decode(), json.loads(verify())['startupProofJson'])
        self.assertEqual([0.1, 0.1], self.sleeps)
        self.assertEqual(('STARTING', 1), (self.b.row()['state'], self.b.row()['pending']))
        self.assertEqual('PENDING', self.b.saved()[0]['status'])

    def test_same_initializing_original_can_be_observed_repeatedly(self):
        held_originals = []
        def progress(index):
            held_originals.append(self.f.held()['proof_initializing'])
            if index == 3:
                self.publish('STARTED')
        self.on_sleep = progress
        with self.waiting():
            self.f.record()
        self.assertEqual(3, len(self.sleeps))
        self.assertTrue(all(value is held_originals[0] for value in held_originals))
        self.assertIs(held_originals[0], self.f.held()['proof'][7])

    def test_missing_timeout_has_no_expired_observation(self):
        self.initial = None
        observed = []
        def expire(index):
            observed.append((self.f.creator.m.poll.call_count, self.runtime.process_adapter.identity.call_count))
            self.b.base.f.clock = self.b.base.f.deadline
        self.on_sleep = expire
        self.reject()
        self.assertEqual([0.1], self.sleeps)
        self.assertEqual(observed[0], (self.f.creator.m.poll.call_count, self.runtime.process_adapter.identity.call_count))

    def test_initializing_timeout_preserves_original(self):
        self.on_sleep = lambda index: setattr(self.b.base.f, 'clock', self.b.base.f.deadline)
        self.reject()
        self.assertEqual(self.f.proof_raw, self.f.held()['proof_initializing'][0])

    def test_wrong_initializing_nonce_is_not_waited(self):
        self.change = lambda value: value.update(startNonce='wrong')
        self.reject()
        self.assertEqual([], self.sleeps)
        self.assertNotIn('proof_initializing', self.f.held())

    def test_wrong_initializing_identity_is_not_waited(self):
        self.change = lambda value: value['identity'].update(pid=123)
        self.reject()
        self.assertEqual([], self.sleeps)

    def test_initializing_with_startup_payload_is_rejected(self):
        self.change = lambda value: value['startup'].update(ready=False)
        self.reject()
        self.assertEqual([], self.sleeps)

    def test_initializing_original_cannot_be_replaced(self):
        def replace(index):
            if index > 1:
                self.publish('STARTED')
                return
            value = json.loads(self.path().read_bytes())
            value['updatedAt'] = 'changed'
            self.path().write_bytes(canonical(value))
        self.on_sleep = replace
        self.reject()
        self.assertEqual(1, len(self.sleeps))
        self.assertNotEqual(self.path().read_bytes(), self.f.held()['proof_initializing'][0])

    def test_published_initializing_cannot_disappear(self):
        self.on_sleep = lambda index: self.path().unlink() if index == 1 else self.publish('STARTED')
        self.reject()
        self.assertEqual(1, len(self.sleeps))
        self.assertIsNotNone(self.f.held()['proof_initializing'])

    def test_process_exit_while_waiting_keeps_resources(self):
        self.on_sleep = lambda index: setattr(self.f.creator.m.poll, 'return_value', 0)
        self.reject()
        self.assertEqual([0.1], self.sleeps)

    def test_real_epoch_commit_while_waiting_prevents_next_read(self):
        original = offline_control.regular_bytes
        reads = []
        def read(path, *args, **kwargs):
            if path.name == 'proof.json':
                reads.append(True)
            return original(path, *args, **kwargs)
        def commit(index):
            self.b.commit('epoch')
            self.publish('STARTED')
        self.on_sleep = commit
        with patch.object(offline_control, 'regular_bytes', side_effect=read):
            self.reject()
        self.assertEqual([0.1], self.sleeps)
        self.assertEqual([True], reads)

    def test_public_renew_while_waiting_does_not_extend_deadline(self):
        original_deadline = self.b.base.f.deadline
        def renew(index):
            self.b.base.pool.submit(self.runtime.registry.execute, 'renew',
                dict(self.b.token, ttlSeconds=600), operation_id='proof-wait-renew').result(10)
            self.publish('STARTED')
        self.on_sleep = renew
        with self.waiting():
            self.f.record()
        self.assertEqual(original_deadline, self.f.held()['deadline'])
        self.assertEqual(1, self.f.creator.generated)

    def test_initializing_slot_cannot_be_refixed(self):
        def replace(index):
            self.f.held()['proof_initializing'] = tuple(list(self.f.held()['proof_initializing']))
            self.publish('STARTED')
        self.on_sleep = replace
        self.reject()
        self.assertEqual(1, len(self.sleeps))

    def test_missing_after_lstat_is_not_retried(self):
        original = offline_control.regular_bytes
        fired = []
        def read(path, *args, **kwargs):
            if path.name == 'proof.json' and not fired:
                fired.append(True)
                path.unlink()
            return original(path, *args, **kwargs)
        with patch.object(offline_control, 'regular_bytes', side_effect=read):
            self.reject()
        self.assertEqual([True], fired)
        self.assertEqual([], self.sleeps)

    def test_expired_after_lstat_prevents_body_read(self):
        original = Path.lstat
        reads = []
        read = offline_control.regular_bytes
        def stat(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.name == 'proof.json':
                self.b.base.f.clock = self.b.base.f.deadline
            return result
        def body(path, *args, **kwargs):
            if path.name == 'proof.json':
                reads.append(True)
            return read(path, *args, **kwargs)
        with patch.object(Path, 'lstat', stat), patch.object(offline_control, 'regular_bytes', side_effect=body):
            self.reject()
        self.assertEqual([], reads)

    def test_oversized_initializing_is_not_waited(self):
        self.change = lambda value: value.update(padding='x' * (16 * 1024 * 1024))
        self.reject()
        self.assertEqual([], self.sleeps)

    def test_malformed_initializing_is_not_retried(self):
        def malformed(kind, stage, operation):
            if (kind, stage, operation.round) == ('write', 'finished', 'bound'):
                self.path().write_bytes(b'{')
        self.f.f.hook = malformed
        self.reject()
        self.assertEqual([], self.sleeps)

    def test_expired_body_read_has_no_further_native_observation(self):
        original = offline_control.regular_bytes
        observed = []
        def read(path, *args, **kwargs):
            raw = original(path, *args, **kwargs)
            if path.name == 'proof.json':
                observed.append((self.f.creator.m.poll.call_count, self.runtime.process_adapter.identity.call_count))
                self.b.base.f.clock = self.b.base.f.deadline
            return raw
        with patch.object(offline_control, 'regular_bytes', side_effect=read):
            self.reject()
        self.assertEqual(1, len(observed))
        self.assertEqual(observed[0], (self.f.creator.m.poll.call_count, self.runtime.process_adapter.identity.call_count))
        self.assertEqual([], self.sleeps)

    def poll_deadline(self, boundary):
        expired, identities = [], []
        poll = self.f.creator.m.poll
        identity = self.runtime.process_adapter.identity
        def observed():
            result = poll()
            frame = sys._getframe(1)
            while frame is not None and frame.f_code.co_name != 'owner':
                frame = frame.f_back
            if frame is not None and not expired:
                caller = frame.f_back
                match = (boundary == 'wait' and caller.f_code.co_name == 'record_start'
                         or boundary == 'save' and caller.f_code.co_name == 'proof_files'
                         and not caller.f_locals['final']
                         or boundary == 'final' and caller.f_code.co_name == 'proof_files'
                         and caller.f_locals['final'])
                if match:
                    expired.append(True)
                    self.b.base.f.clock = self.b.base.f.deadline
            return result
        def identified(pid):
            if expired:
                identities.append(pid)
            return identity(pid)
        with patch.object(self.f.creator.m, 'poll', side_effect=observed), patch.object(
                self.runtime.process_adapter, 'identity', side_effect=identified):
            self.reject()
        self.assertEqual([True], expired)
        self.assertEqual([], identities)

    def test_poll_deadline_prevents_identity_while_waiting(self):
        self.poll_deadline('wait')
        self.assertEqual([], self.sleeps)

    def test_poll_deadline_prevents_identity_before_save(self):
        self.poll_deadline('save')

    def test_poll_deadline_prevents_identity_in_final_bundle(self):
        # この境界はcommit後なので保存済みproofも保持する。
        with patch.object(self, 'reject', side_effect=self.f.reject), self.waiting():
            self.poll_deadline('final')
        self.assertIn('startupProofJson', self.f.saved()[0])

    def parse_deadline(self, phase):
        self.initial = phase
        expired, calls, paths, reads = [], [], [], []
        decode, proof = offline_control._json, self.runtime._proof
        plain, read = runtime_control.plain_path, Path.read_bytes
        def parsed(raw):
            value = decode(raw)
            if raw == self.f.proof_raw and value.get('phase') == phase and not expired:
                expired.append(True)
                self.b.base.f.clock = self.b.base.f.deadline
            return value
        def checked(*args, **kwargs):
            if expired:
                calls.append(True)
            return proof(*args, **kwargs)
        def path(value):
            if expired:
                paths.append(value)
            return plain(value)
        def body(value, *args, **kwargs):
            if expired:
                reads.append(value)
            return read(value, *args, **kwargs)
        with patch.object(offline_control, '_json', side_effect=parsed), patch.object(
                self.runtime, '_proof', side_effect=checked), patch.object(
                runtime_control, 'plain_path', side_effect=path), patch.object(Path, 'read_bytes', body):
            self.reject()
        self.assertEqual([True], expired)
        self.assertEqual(([], [], []), (calls, paths, reads))
        self.assertEqual([], self.sleeps)

    def test_initializing_parse_deadline_prevents_proof_observation(self):
        self.parse_deadline('INITIALIZING')

    def test_started_parse_deadline_prevents_proof_observation(self):
        self.parse_deadline('STARTED')

    def test_proof_path_deadline_prevents_next_path_observation(self):
        plain = runtime_control.plain_path
        expired, after = [], []
        def path(value):
            if expired:
                after.append(value)
            result = plain(value)
            frame = sys._getframe(1)
            while frame is not None and frame.f_code.co_name != '_proof':
                frame = frame.f_back
            if frame is not None and not expired:
                expired.append(True)
                self.b.base.f.clock = self.b.base.f.deadline
            return result
        with patch.object(runtime_control, 'plain_path', side_effect=path):
            self.reject()
        self.assertEqual([True], expired)
        self.assertEqual([], after)
        self.assertEqual([], self.sleeps)

    def test_proof_properties_parse_deadline_prevents_root_observation(self):
        self.initial = 'STARTED'
        properties, plain = snapshot_data.properties, runtime_control.plain_path
        expired, after = [], []
        def parsed(raw):
            value = properties(raw)
            frame = sys._getframe(1)
            while frame is not None and frame.f_code.co_name != '_proof':
                frame = frame.f_back
            if frame is not None:
                expired.append(True)
                self.b.base.f.clock = self.b.base.f.deadline
            return value
        def path(value):
            if expired:
                after.append(value)
            return plain(value)
        with patch.object(snapshot_data, 'properties', side_effect=parsed), patch.object(
                runtime_control, 'plain_path', side_effect=path):
            self.reject()
        self.assertEqual([True], expired)
        self.assertEqual([], after)

    def test_final_current_deadline_prevents_proof_reread(self):
        expired, after = [], []
        original = offline_control.regular_bytes
        def profile(frame, event, value):
            caller = frame.f_back
            if event == 'return' and frame.f_code.co_name == 'current' and caller is not None:
                if caller.f_code.co_name == 'proof_files' and caller.f_locals['final']:
                    expired.append(True)
                    self.b.base.f.clock = self.b.base.f.deadline
        def read(path, *args, **kwargs):
            if expired and path.name == 'proof.json':
                after.append(path)
            return original(path, *args, **kwargs)
        previous = sys.getprofile()
        sys.setprofile(profile)
        try:
            with self.waiting(), patch.object(offline_control, 'regular_bytes', side_effect=read):
                self.f.reject()
        finally:
            sys.setprofile(previous)
        self.assertEqual([True], expired)
        self.assertEqual([], after)
        self.assertIn('startupProofJson', self.f.saved()[0])

    def callback_deadline(self, boundary):
        expired, actions = [], []
        dumps, copy = offline_control.json.dumps, offline_control.deepcopy
        def serialized(value, *args, **kwargs):
            result = dumps(value, *args, **kwargs)
            if boundary == 'serialization' and isinstance(value, dict) and 'startupProofJson' in value and not expired:
                expired.append(True)
                self.b.base.f.clock = self.b.base.f.deadline
            return result
        def copied(value, *args, **kwargs):
            result = copy(value, *args, **kwargs)
            if boundary == 'copy' and isinstance(value, dict) and value.get('status') == 'OFFLINE_START_PROOF_RECORDED':
                expired.append(True)
                self.b.base.f.clock = self.b.base.f.deadline
            return result
        def profile(frame, event, value):
            if expired and event == 'call' and frame.f_code.co_name in (
                    'identity','poll','sockets','plain_path','regular_bytes','read_bytes','read_text','lstat','resolve','_transaction'):
                actions.append(frame.f_code.co_name)
        with self.waiting(), patch.object(offline_control.json, 'dumps', side_effect=serialized), patch.object(
                offline_control, 'deepcopy', side_effect=copied), ExitStack() as stack:
            seen = set()
            for adapter in (self.runtime.process_adapter,self.f.f.original.c.adapter,self.b.base.p.reader.adapter):
                if id(adapter) in seen:
                    continue
                seen.add(id(adapter))
                identity = adapter.identity
                def identified(pid, original=identity):
                    if expired:
                        actions.append('identity')
                    return original(pid)
                stack.enter_context(patch.object(adapter,'identity',side_effect=identified))
            previous = sys.getprofile()
            sys.setprofile(profile)
            try:
                with self.assertRaises(RegistryError):
                    self.f.record()
            finally:
                sys.setprofile(previous)
        self.assertEqual([True], expired)
        self.assertEqual([], actions)
        self.f.retained()
        self.assertEqual(boundary == 'copy', 'startupProofJson' in self.f.saved()[0])

    def test_serialization_deadline_prevents_inherited_observation(self):
        self.callback_deadline('serialization')

    def test_final_copy_deadline_prevents_inherited_observation(self):
        self.callback_deadline('copy')

    def test_growth_after_lstat_is_bounded_and_not_waited(self):
        original = Path.lstat
        fired = []
        def stat(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.name == 'proof.json' and not fired:
                fired.append(True)
                path.write_bytes(b'x' * (16 * 1024 * 1024 + 1))
            return result
        with patch.object(Path, 'lstat', stat):
            self.reject()
        self.assertEqual([True], fired)
        self.assertEqual([], self.sleeps)

    def test_current_change_during_initializing_read_refuses_wait(self):
        original = offline_control.regular_bytes
        fired = []
        def read(path, *args, **kwargs):
            raw = original(path, *args, **kwargs)
            if path.name == 'proof.json' and not fired:
                fired.append(True)
                self.f.f.original.current_path.write_bytes(b'{}')
            return raw
        with patch.object(offline_control, 'regular_bytes', side_effect=read):
            self.reject()
        self.assertEqual([True], fired)
        self.assertEqual([], self.sleeps)

    def test_real_epoch_commit_after_initializing_validation_refuses_wait(self):
        original = self.runtime._proof
        fired = []
        def proof(*args, **kwargs):
            value = original(*args, **kwargs)
            if kwargs.get('initializing'):
                fired.append(True)
                self.b.commit('epoch')
            return value
        with patch.object(self.runtime, '_proof', side_effect=proof):
            self.reject()
        self.assertEqual([True], fired)
        self.assertEqual([], self.sleeps)

    def test_process_exit_after_initializing_validation_refuses_wait(self):
        original = self.runtime._proof
        def proof(*args, **kwargs):
            value = original(*args, **kwargs)
            if kwargs.get('initializing'):
                self.f.creator.m.poll.return_value = 0
            return value
        with patch.object(self.runtime, '_proof', side_effect=proof):
            self.reject()
        self.assertEqual([], self.sleeps)

    def test_wait_identity_change_is_rejected(self):
        identity = self.runtime.process_adapter.identity
        def change(index):
            def swapped(pid):
                value = identity(pid)
                if pid == self.f.creator.m.pid:
                    value = dict(value, startId='another-start')
                return value
            self.runtime.process_adapter.identity = swapped
            self.publish('STARTED')
        self.on_sleep = change
        try:
            self.reject()
        finally:
            self.runtime.process_adapter.identity = identity
        self.assertEqual(1, len(self.sleeps))

    def test_initializing_binding_survives_to_final_return(self):
        original = []
        def changed():
            initial = self.f.held()['proof_initializing']
            original.append(initial)
            self.f.held()['proof_initializing'] = tuple(list(initial))
        with self.waiting():
            self.f.final_copy(changed)
        self.assertEqual(1, len(original))
        self.assertIs(original[0], self.f.held()['proof'][7])
        self.assertIn('startupProofJson', self.f.saved()[0])

    def test_wrong_initializing_generation_is_not_waited(self):
        self.change = lambda value: value.update(generationId='another-generation')
        self.reject()
        self.assertEqual([], self.sleeps)


if __name__ == '__main__':
    unittest.main()

"""固定worldの現在Ready原文を、元offline終端へ結ぶ読取境界を確認する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from . import offline_control
from . import test_offline_finish_close as finish, test_offline_owner_close as owner_close
from .build_inputs import canonical, digest
from .registry import RegistryError


class ReadyOriginalTests(unittest.TestCase):
    def prepare(self, change=lambda state: None):
        original = owner_close.OfflineOwnerCloseTests.configure

        def configure(case, *args):
            original(case, *args)
            after = case.after_effect

            def published():
                after()
                opened = json.loads(case.c.request)
                world = Path(opened['worldRoot'])
                self.data = world / 'sushieric'
                self.current_path = self.data / 'run-state/current.json'
                self.world_path = self.data / 'world-id'
                self.state = dict(format=1, worldUuid=case.current['world'], generation=case.current['generation'],
                    runUuid=case.current['run'], stateRevision=case.current['revision'],
                    paths=dict(worldRoot=opened['worldRoot'], database=str(self.data / 'server.sqlite'), shopDirectory=opened['shopDirectory']),
                    originSha256='a' * 64, previousStateSha256=case.current['previous'],
                    payload=dict(type='io.github.sushiericworkspace.sushiericdatamodel.database.run.WorldRunPhase.Ready',
                                 sealSha256='b' * 64, admissionSha256='c' * 64))
                change(self.state)
                self.raw = canonical(self.state)
                case.current['hash'] = case.reply['stateSha256'] = digest(self.raw)
                self.state_path = self.data / 'run-state/states' / (digest(self.raw) + '.json')
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.world_path.write_bytes(case.current['world'].encode('utf-8'))
                self.current_path.write_bytes(self.raw)
                self.state_path.write_bytes(self.raw)

            case.after_effect = published

        self.base = finish.OfflineFinishCloseTests()
        self.addCleanup(self.base.doCleanups)
        with patch.object(owner_close.OfflineOwnerCloseTests, 'configure', configure):
            self.base.setUp()
        self.base.finish()
        self.f, self.c, self.runtime, self.token = self.base.f, self.base.c, self.base.runtime, self.base.token
        self.ready = {name: json.loads(self.c.close_response)[name] for name in ('runUuid', 'stateSha256', 'stateRevision')}
        self.ready['worldUuid'] = self.base.opened['worldUuid']
        self.before = deepcopy(self.f.saved())
        self.counts = self.f.counts()

    def read(self, **changes):
        args = dict(open_operation_id=self.base.opened['operationId'], ready=self.ready, deadline=self.f.deadline)
        args.update(changes)
        return self.runtime._offline_read_ready_original(self.token, **args)

    def rejected(self, **changes):
        with self.assertRaises((RegistryError, ValueError, OSError)):
            self.read(**changes)
        self.assertEqual(self.before, self.f.saved())
        self.assertEqual(self.counts, self.f.counts())
        self.base.completed()

    def test_matches_original_bytes_without_start_permission_or_chain_claim(self):
        self.prepare()
        result = self.read()
        self.assertEqual('OFFLINE_READY_ORIGINAL_MATCHED', result['status'])
        self.assertEqual(self.raw, result['stateJson'].encode('utf-8'))
        self.assertEqual(self.ready, result['ready'])
        self.assertFalse((self.data / 'run-state/receipts').exists())
        self.assertEqual(self.before, self.f.saved())
        self.assertEqual(self.counts, self.f.counts())
        with self.assertRaises(RegistryError) as raised:
            self.runtime.execute('start', dict(self.token, timeoutSeconds=2), operation_id='ready-original-start')
        self.assertEqual('CONTROL_BLOCKED', raised.exception.code)
        self.base.completed()

    def test_current_hash_must_match_independent_ready(self):
        self.prepare()
        self.current_path.write_bytes(self.raw + b' ')
        self.state_path.write_bytes(self.raw + b' ')
        self.rejected()

    def test_immutable_bytes_must_match_current(self):
        self.prepare()
        self.state_path.write_bytes(self.raw + b' ')
        self.rejected()

    def test_world_id_must_match(self):
        self.prepare()
        self.world_path.write_bytes(b'00000000-0000-4000-8000-000000000099')
        self.rejected()

    def test_generation_must_match_even_when_hash_matches(self):
        self.prepare(lambda state: state.update(generation='00000000-0000-4000-8000-000000000099'))
        self.rejected()

    def test_revision_float_is_rejected_even_when_hash_matches(self):
        self.prepare(lambda state: state.update(stateRevision=float(state['stateRevision'])))
        self.rejected()

    def test_format_boolean_is_rejected_even_when_hash_matches(self):
        self.prepare(lambda state: state.update(format=True))
        self.rejected()

    def test_paths_must_match_even_when_hash_matches(self):
        self.prepare(lambda state: state['paths'].update(database=state['paths']['database'] + '.other'))
        self.rejected()

    def test_phase_must_be_ready_even_when_hash_matches(self):
        self.prepare(lambda state: state['payload'].update(type='Running'))
        self.rejected()

    def test_missing_current_is_rejected(self):
        self.prepare()
        self.current_path.unlink()
        self.rejected()

    def test_hardlinked_current_is_rejected(self):
        self.prepare()
        other = self.current_path.with_name('linked.json')
        os.link(self.current_path, other)
        self.rejected()

    def size_race(self, occurrence):
        self.prepare()
        original_read, original_open = offline_control.regular_bytes, Path.open
        seen, reads = [], []

        def read(path, **kwargs):
            if path == self.world_path:
                seen.append(path)
                if len(seen) == occurrence:
                    path.write_bytes(self.ready['worldUuid'].encode('utf-8') + b' ' * 256)
            return original_read(path, **kwargs)

        @contextmanager
        def opened(path, mode='r', *args, **kwargs):
            with original_open(path, mode, *args, **kwargs) as source:
                if path == self.world_path and mode == 'rb':
                    def bounded(size=-1):
                        raw = source.read(size)
                        reads.append((size, len(raw), kwargs.get('buffering', -1)))
                        return raw
                    yield SimpleNamespace(read=bounded)
                else:
                    yield source

        with patch.object(offline_control, 'regular_bytes', side_effect=read), patch.object(Path, 'open', opened):
            self.rejected()
        self.assertEqual([(129, 36, 0)] * (occurrence - 1) + [(129, 129, 0)], reads)

    def test_growth_after_initial_size_check_is_bounded(self):
        self.size_race(1)

    def test_growth_after_reread_size_check_is_bounded(self):
        self.size_race(2)

    def test_empty_ready_is_rejected_before_original_read(self):
        self.prepare()
        with patch.object(offline_control, 'regular_bytes', wraps=offline_control.regular_bytes) as read:
            self.rejected(ready={})
        read.assert_not_called()

    def test_deadline_after_original_read_has_no_result(self):
        self.prepare()
        original = offline_control.regular_bytes
        def read(path, **kwargs):
            value = original(path, **kwargs)
            if path == self.current_path:
                self.f.clock = self.f.deadline
            return value
        with patch.object(offline_control, 'regular_bytes', side_effect=read), \
             patch.object(offline_control, 'digest', wraps=offline_control.digest) as hashed:
            self.rejected()
        self.assertFalse(any(call.args[0] == self.raw for call in hashed.call_args_list))

    def test_deadline_before_original_read_has_no_file_read(self):
        self.prepare()
        original = self.runtime._active_generation
        def active(row):
            result = original(row)
            self.f.clock = self.f.deadline
            return result
        with patch.object(self.runtime, '_active_generation', side_effect=active), \
             patch.object(offline_control, 'regular_bytes', wraps=offline_control.regular_bytes) as read:
            self.rejected()
        read.assert_not_called()

    def test_result_copy_expiry_has_no_result(self):
        self.prepare()
        original = offline_control.deepcopy
        def copy(value):
            result = original(value)
            if isinstance(value, dict) and value.get('status') == 'OFFLINE_READY_ORIGINAL_MATCHED':
                self.f.clock = self.f.deadline
            return result
        with patch.object(offline_control, 'deepcopy', side_effect=copy):
            self.rejected()

    def test_current_change_during_result_copy_is_rejected(self):
        self.prepare()
        original = offline_control.deepcopy
        def copy(value):
            result = original(value)
            if isinstance(value, dict) and value.get('status') == 'OFFLINE_READY_ORIGINAL_MATCHED':
                self.current_path.write_bytes(self.raw + b' ')
            return result
        with patch.object(offline_control, 'deepcopy', side_effect=copy):
            self.rejected()

    def test_caller_ready_change_during_read_uses_entry_snapshot(self):
        self.prepare()
        expected = deepcopy(self.ready)
        original = offline_control.regular_bytes
        def read(path, **kwargs):
            value = original(path, **kwargs)
            if path == self.current_path:
                self.ready.clear()
            return value
        with patch.object(offline_control, 'regular_bytes', side_effect=read):
            self.assertEqual(expected, self.read()['ready'])
        self.base.completed()

    def test_final_transaction_expiry_has_no_result(self):
        self.prepare()
        original = self.runtime._transaction
        seen = []
        read_file = offline_control.regular_bytes
        def observe(path, **kwargs):
            value = read_file(path, **kwargs)
            seen.append(path)
            return value
        @contextmanager
        def transaction(*args, **kwargs):
            with original(*args, **kwargs) as connection:
                yield connection
            if self.current_path in seen:
                self.f.clock = self.f.deadline
        with patch.object(self.runtime, '_transaction', side_effect=transaction), \
             patch.object(offline_control, 'regular_bytes', side_effect=observe):
            self.rejected()

    def test_real_registry_change_during_original_read_is_rejected(self):
        self.prepare()
        entered, resume = threading.Event(), threading.Event()
        original = offline_control.regular_bytes
        def read(path, **kwargs):
            value = original(path, **kwargs)
            if path == self.current_path and not entered.is_set():
                entered.set()
                self.assertTrue(resume.wait(10))
            return value
        saved = self.f.saved()
        saved['writerClosed']['state'] = 'UNKNOWN'
        with patch.object(offline_control, 'regular_bytes', side_effect=read), ThreadPoolExecutor() as workers:
            future = workers.submit(self.read)
            try:
                self.assertTrue(entered.wait(10))
                self.f.save(saved)
            finally:
                resume.set()
            with self.assertRaises(RegistryError):
                future.result()
        self.assertEqual(saved, self.f.saved())
        self.assertEqual((('LEASED_STOPPED', 0), 'COMPLETED', 0), self.base.state())
        self.assertEqual(self.counts, self.f.counts())

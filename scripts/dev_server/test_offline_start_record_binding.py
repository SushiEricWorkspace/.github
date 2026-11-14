"""新記録の最終read退出後にも元helperの完成bindingを照合する。"""
from contextlib import contextmanager
from copy import deepcopy
import unittest
from unittest.mock import patch

from . import offline_control
from .registry import RegistryError
from . import test_offline_start_record as records


class InheritedBindingTests(unittest.TestCase):
    def setUp(self):
        self.f = records.StartRecordTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def final_read(self, change=None):
        transaction = self.f.runtime._transaction
        regular, resources = offline_control.regular_bytes, self.f.offline._terminal_resources
        reads, original = [], []
        after = False
        @contextmanager
        def checked(instance, *, read_only=False):
            nonlocal after
            with transaction(instance, read_only=read_only) as db:
                yield db
            held = self.f.offline.start_records.get(self.f.key)
            if read_only and held is not None and held['recorded']:
                reads.append(True)
                if len(reads)==2:
                    reader = self.f.b.base.p.reader
                    original.append((reader, reader.process, reader.pipe, reader.request,
                                     reader.ready_check_response, held, held['packet']))
                    after = True
                    if change is not None:change(reader)
        def read(*args, **kwargs):
            self.assertFalse(after, '最終read退出後にファイルを再読しません')
            return regular(*args, **kwargs)
        def native(*args, **kwargs):
            self.assertFalse(after, '最終read退出後に元資源を再観測しません')
            return resources(*args, **kwargs)
        with patch.object(self.f.runtime, '_transaction', checked), \
             patch.object(offline_control, 'regular_bytes', side_effect=read), \
             patch.object(self.f.offline, '_terminal_resources', side_effect=native):
            if change is None:
                result = self.f.record()
            else:
                with self.assertRaises(RegistryError):self.f.record()
                result = None
        self.assertEqual(2, len(reads))
        reader, process, pipe, request, response, held, packet = original[0]
        self.assertIs(reader, self.f.offline.readers[self.f.b.base.key]['reader'])
        self.assertIs(process, reader.process)
        self.assertIs(pipe, reader.pipe)
        self.assertIs(held, self.f.offline.start_records[self.f.key])
        self.assertIs(packet, held['packet'])
        self.assertTrue(all(self.f.rows()))
        if change is not None:
            self.f.retained(True)
            with self.assertRaises(RegistryError):held['binding']()
        return result, request, response

    def test_unchanged_binding_returns_without_io_after_final_read(self):
        result, _, _ = self.final_read()
        self.assertEqual('OFFLINE_START_RECORDED', result[0]['status'])
        self.assertTrue(self.f.offline.start_records[self.f.key]['returned'])

    def test_release_progress_change_after_final_read_refuses_return(self):
        self.final_read(lambda reader:reader.ready_check_release.update(pipe='UNKNOWN'))

    def test_approval_value_change_after_final_read_refuses_return(self):
        self.final_read(lambda reader:self.f.b.approval.update(changed=True))

    def test_approval_object_change_after_final_read_refuses_return(self):
        self.final_read(lambda reader:self.f.runtime.managed.approvals.update(
            {self.f.b.token['instanceId']:deepcopy(self.f.b.approval)}))

    def test_retained_request_change_after_final_read_refuses_return(self):
        self.final_read(lambda reader:setattr(reader, 'request', reader.request+b' '))

    def test_helper_handle_change_after_final_read_refuses_return(self):
        self.final_read(lambda reader:setattr(reader.process, 'handle', 98765))

    def test_artifact_object_change_after_final_read_refuses_return(self):
        self.final_read(lambda reader:setattr(reader, 'ready_check_artifact', deepcopy(reader.ready_check_artifact)))

    def test_helper_deadline_change_after_final_read_refuses_return(self):
        self.final_read(lambda reader:setattr(reader, 'ready_check_deadline', reader.ready_check_deadline+1))

    def test_helper_ready_read_change_after_final_read_refuses_return(self):
        self.final_read(lambda reader:reader.record['readyRead'].update(changed=True))

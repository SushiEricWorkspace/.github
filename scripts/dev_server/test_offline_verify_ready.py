"""元終端・現在原文・実読取部品を実Registryの短い照合へ結ぶ。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import shutil
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import offline_control, offline_process, runtime_process
from . import test_offline_ready_original as originals, test_offline_ready_process as readers
from .build_inputs import canonical, digest
from .registry import RegistryError


OPERATION = '12345678-1234-4234-8234-123456789abc'
OTHER = '12345678-1234-4234-8234-123456789abd'


class VerifyReadyTests(unittest.TestCase):
    def setUp(self):
        self.o = originals.ReadyOriginalTests()
        self.addCleanup(self.o.doCleanups)
        self.o.prepare()
        self.p = readers.ReadyProcessTests()
        self.addCleanup(self.p.doCleanups)
        self.p.setUp()
        self.f, self.runtime, self.token = self.o.f, self.o.runtime, self.o.token
        self.offline = self.runtime._offline
        self.key = self.token['instanceId'], OPERATION
        self.terminal = self.offline.retired[(self.token['instanceId'], self.o.base.opened['operationId'])]
        bundle = self.o.c.bundle = self.runtime._active_generation(self.terminal['row']) / 'runtime'
        bundle.rename(bundle.with_name('runtime-before-ready-fixture'))
        shutil.copytree(self.p.bundle, bundle)
        self.p.manifest.update(artifactId=self.terminal['row']['artifact'], schemaVersion=2)
        (bundle/'runtime.json').write_bytes(canonical(self.p.manifest))
        (bundle/'COMPLETE').write_text(self.p.manifest['artifactId'], encoding='utf-8')
        self.p.bundle = bundle
        self.runtime.process_adapter = self.p.adapter
        self.runtime.managed.transport.native = self.p.native
        process = self.p.reader.process
        self.enterContext(patch.object(self.p.adapter, 'OwnedProcess', return_value=process))
        def create(*args, **kwargs):
            reader = offline_process.OfflineProcess(*args, **kwargs)
            self.p.reader = reader
            return reader
        self.enterContext(patch.object(offline_control, 'OfflineProcess', side_effect=create))
        self.enterContext(patch.object(offline_control, 'verify_bundle', side_effect=lambda _:deepcopy(self.p.manifest)))
        self.enterContext(patch.object(offline_control.time, 'monotonic', lambda:self.f.clock))
        self.pool = self.enterContext(ThreadPoolExecutor(max_workers=1))

    def read(self, **changes):
        args = dict(operation_id=OPERATION, open_operation_id=self.o.base.opened['operationId'],
                    ready=self.o.ready, deadline=self.f.deadline)
        args.update(changes)
        return self.runtime._offline_verify_ready(self.token, **args)

    def held(self):
        return self.offline.readers[self.key]

    def commit(self, what='closed'):
        def change():
            with self.o.base.runtime.registry.transaction() as db:
                if what == 'closed':
                    saved = self.f.saved()
                    saved['writerClosed']['state'] = 'UNKNOWN'
                    db.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                               (json.dumps(saved), self.o.base.opened['operationId']))
                elif what == 'epoch':
                    db.execute('UPDATE instances SET epoch=epoch+1 WHERE id=?', (self.token['instanceId'],))
                elif what == 'reservation':
                    row = self.terminal['row']
                    db.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',
                               ('competing-read','artifact:'+row['artifact'],row['id'],row['lease'],row['epoch']))
        self.pool.submit(change).result(10)

    def rejected(self, **changes):
        with self.assertRaises((RegistryError, OSError, EOFError, TimeoutError)):
            self.read(**changes)

    def no_retry(self):
        effects = self.p.effects()
        self.rejected()
        self.rejected(operation_id=OTHER)
        self.assertEqual(effects, self.p.effects())

    def test_official_reader_is_bound_to_current_original_terminal_and_release(self):
        result = self.read()
        held = self.held()
        self.assertIs(self.p.reader, held['reader'])
        self.assertIs(self.terminal, held['terminal'])
        self.assertEqual('OFFLINE_READY_VERIFIED', result['status'])
        self.assertEqual(self.o.raw, result['stateJson'].encode('utf-8'))
        self.assertEqual(self.o.ready, result['ready'])
        self.assertEqual(canonical(result), held['result'])
        self.assertTrue(held['completed'])
        self.assertEqual(self.f.deadline, held['deadline'])
        self.assertEqual([self.f.deadline]*2, self.p.deadlines)
        self.assertEqual((1,1,1,1), self.p.effects())
        self.assertEqual(self.o.before, self.f.saved())
        self.o.base.completed()
        self.o.base.start_blocked()
        result['reader']['ready']['stateRevision'] += 1
        self.assertNotEqual(canonical(result), held['result'])
        self.rejected()

    def test_invalid_ready_has_no_generation(self):
        for value in ({}, dict(self.o.ready,stateRevision=float(self.o.ready['stateRevision'])),
                      dict(self.o.ready,stateSha256='0'*64)):
            with self.subTest(value=value):self.rejected(ready=value)
        self.assertEqual((0,0,0,0), self.p.effects())

    def test_changed_current_before_entry_has_no_generation(self):
        self.o.current_path.write_bytes(self.o.raw+b' ')
        self.rejected()
        self.assertEqual((0,0,0,0), self.p.effects())

    def test_wrong_artifact_has_no_generation(self):
        self.p.manifest['artifactId'] = '0'*64
        self.rejected()
        self.assertEqual((0,0,0,0), self.p.effects())

    def test_caller_snapshot_is_used_after_input_mutation(self):
        ready = deepcopy(self.o.ready)
        self.p.after_send = self.o.ready.clear
        self.assertEqual(ready, self.read()['ready'])

    def test_commit_during_artifact_inventory_prevents_generation(self):
        original = offline_process.regular_bytes
        fired = []
        def read(path, **kwargs):
            raw = original(path, **kwargs)
            if path.name == 'helper.jar' and self.key in self.offline.readers and not fired:
                fired.append(True);self.commit()
            return raw
        with patch.object(offline_process, 'regular_bytes', side_effect=read):self.rejected()
        self.assertEqual([True], fired)
        self.assertEqual(0, self.p.effects()[0])
        self.assertIs(self.p.reader, self.held()['reader'])
        self.no_retry()

    def test_commit_during_current_reread_prevents_generation(self):
        original = offline_control.regular_bytes
        fired = []
        def read(path, **kwargs):
            raw = original(path, **kwargs)
            if path == self.o.current_path and self.key in self.offline.readers and not fired:
                fired.append(True);self.commit('epoch')
            return raw
        with patch.object(offline_control, 'regular_bytes', side_effect=read):self.rejected()
        self.assertEqual([True], fired)
        self.assertEqual(0, self.p.effects()[0])
        self.no_retry()

    def test_response_commit_rejects_before_release_and_retains_raw(self):
        self.p.after_receive = self.commit
        self.rejected()
        self.assertIsNotNone(self.p.reader.ready_check_response)
        self.assertEqual((1,1,1,0), self.p.effects())
        self.no_retry()

    def test_reservation_commit_rejects_before_send(self):
        start = self.p.reader.process.start
        def created(*args, **kwargs):
            start(*args, **kwargs)
            self.commit('reservation')
        with patch.object(self.p.reader.process, 'start', side_effect=created):self.rejected()
        self.assertEqual(1, self.p.effects()[0])
        self.assertEqual(0, self.p.effects()[2])
        self.no_retry()

    def test_original_change_after_response_rejects_before_release(self):
        self.p.after_receive = lambda:self.o.current_path.write_bytes(self.o.raw+b' ')
        self.rejected()
        self.assertEqual(0, self.p.closes)
        self.no_retry()

    def test_commit_during_handle_release_keeps_unknown_and_pipe(self):
        self.p.close_hook = self.commit
        self.rejected()
        self.assertEqual({'handle':'UNKNOWN','pipe':'NOT_STARTED'},self.p.reader.ready_check_release)
        self.assertIsNotNone(self.p.reader.pipe.input)
        self.no_retry()

    def test_partial_close_never_regenerates_or_releases_pipe(self):
        self.p.close_failure = True
        self.rejected()
        self.assertEqual({'handle':'UNKNOWN','pipe':'NOT_STARTED'},self.p.reader.ready_check_release)
        self.no_retry()

    def test_response_loss_never_resends(self):
        self.p.failure = 'lost'
        self.rejected()
        self.assertIsNotNone(self.p.reader.request)
        self.assertIsNone(self.p.reader.ready_check_response)
        self.no_retry()

    def final_copy(self, change, *, release=None):
        original = offline_control.deepcopy
        fired = []
        def copy(value):
            copied = original(value)
            if isinstance(value,dict) and value.get('status') == 'OFFLINE_READY_VERIFIED':
                fired.append(True);change()
            return copied
        with patch.object(offline_control,'deepcopy',side_effect=copy):self.rejected()
        self.assertEqual([True],fired)
        self.assertIsNotNone(self.held()['result'])
        self.assertFalse(self.held().get('completed',False))
        self.assertEqual({'handle':'RELEASED','pipe':'RELEASED'} if release is None else release,
                         self.p.reader.ready_check_release)
        self.no_retry()

    def test_final_copy_commit_refuses_success(self):self.final_copy(self.commit)

    def test_final_copy_current_change_refuses_success(self):
        self.final_copy(lambda:self.o.current_path.write_bytes(self.o.raw+b' '))

    def test_final_copy_reader_replacement_refuses_success(self):
        self.final_copy(lambda:self.held().update(reader=object()))

    def test_final_copy_input_replacement_refuses_success(self):
        self.final_copy(lambda:self.held().update(input=b'changed'))

    def test_final_copy_helper_result_change_refuses_success(self):
        self.final_copy(lambda:setattr(self.p.reader,'ready_check_result',b'changed'))

    def final_copy_raw_change(self, attribute):
        originals = {}
        def change():
            originals.update(slot=self.held(), reader=self.p.reader,
                             request=self.p.reader.request, response=self.p.reader.ready_check_response)
            setattr(self.p.reader, attribute, getattr(self.p.reader, attribute) + b' ')
        self.final_copy(change)
        self.assertIs(originals['slot'], self.held())
        self.assertIs(originals['reader'], self.held()['reader'])
        observed = json.loads(self.p.reader.ready_check_result)
        for name in ('request', 'response'):
            self.assertEqual(originals[name], self.held()[name])
            self.assertEqual(digest(originals[name]), observed[name + 'Sha256'])

    def test_final_copy_request_raw_change_refuses_success(self):
        self.final_copy_raw_change('request')

    def test_final_copy_response_raw_change_refuses_success(self):
        self.final_copy_raw_change('ready_check_response')

    def final_copy_completion_change(self, change, *, release=None):
        originals = {}
        def changed():
            held, reader = self.held(), self.p.reader
            originals.update(slot=held, reader=reader, process=reader.process,
                             pipe=reader.pipe, resources=reader.creation_resources,
                             input=held['input'], original=held['original'],
                             request=held['request'], response=held['response'])
            change()
        self.final_copy(changed, release=release)
        held, reader = self.held(), self.p.reader
        self.assertIs(originals['slot'], held)
        self.assertIs(originals['reader'], held['reader'])
        self.assertIs(originals['process'], reader.process)
        self.assertIs(originals['pipe'], reader.pipe)
        self.assertIs(originals['resources'], reader.creation_resources)
        for name in ('input', 'original', 'request', 'response'):
            self.assertEqual(originals[name], held[name])
        self.assertEqual(originals['request'], reader.request)
        self.assertEqual(originals['response'], reader.ready_check_response)
        observed = json.loads(reader.ready_check_result)
        self.assertEqual(digest(held['request']), observed['requestSha256'])
        self.assertEqual(digest(held['response']), observed['responseSha256'])

    def test_final_copy_release_progress_change_refuses_success(self):
        self.final_copy_completion_change(lambda:self.p.reader.ready_check_release.update(pipe='UNKNOWN'),
                                         release={'handle':'RELEASED','pipe':'UNKNOWN'})

    def test_final_copy_helper_input_change_refuses_success(self):
        self.final_copy_completion_change(lambda:setattr(self.p.reader,'ready_check_input',b'changed'))

    def test_final_copy_ready_read_record_change_refuses_success(self):
        self.final_copy_completion_change(lambda:self.p.reader.record['readyRead'].update(status='UNKNOWN'))

    def final_copy_binding_change(self, change):
        originals = {}
        def changed():
            held, reader = self.held(), self.p.reader
            originals.update(slot=held, binding=held['binding'], objects=held['binding'][0],
                             request=held['request'], response=held['response'], result=held['result'])
            change()
        self.final_copy(changed)
        held = self.held()
        self.assertIs(originals['slot'], held)
        self.assertIs(originals['binding'], held['binding'])
        for original, current in zip(originals['objects'], held['binding'][0]):
            self.assertIs(original, current)
        for name in ('request', 'response', 'result'):
            self.assertEqual(originals[name], held[name])
        observed = json.loads(self.p.reader.ready_check_result)
        self.assertEqual(digest(held['request']), observed['requestSha256'])
        self.assertEqual(digest(held['response']), observed['responseSha256'])

    def test_final_copy_resource_binding_change_refuses_success(self):
        def change():
            reader = self.p.reader
            reader.pipe = SimpleNamespace(input=12345, output=23456)
            reader.creation_resources = reader.process, reader.pipe
            reader.process.handle = 97531
        self.final_copy_binding_change(change)

    def test_final_copy_person_binding_change_refuses_success(self):
        def change():
            reader = self.p.reader
            reader.identity['pid'] += 1
            reader.data_identity['pid'] += 1
            reader.parent_data['startId'] = 'changed'
            reader.session, reader.nonce = 'changed-session', 'changed-nonce'
            reader.record['identity'] = deepcopy(reader.identity)
            reader.record['controlIdentity'] = deepcopy(reader.data_identity)
        self.final_copy_binding_change(change)

    def test_final_copy_artifact_binding_and_deadline_change_refuses_success(self):
        def change():
            reader = self.p.reader
            reader.ready_check_artifact = dict(artifactId=reader.ready_check_artifact['artifactId'])
            reader.ready_check_deadline += 100
        self.final_copy_binding_change(change)

    def test_final_copy_artifact_content_change_refuses_success(self):
        self.final_copy_binding_change(lambda:self.p.reader.ready_check_artifact['files'].clear())

    def test_final_copy_artifact_object_replacement_with_same_content_refuses_success(self):
        self.final_copy_binding_change(lambda:setattr(self.p.reader,'ready_check_artifact',
                                                      deepcopy(self.p.reader.ready_check_artifact)))

    def test_final_copy_handle_fd_values_change_refuses_success(self):
        def change():
            reader = self.p.reader
            reader.process.handle = 97531
            reader.pipe.input = 12345
            reader.ready_check_fds = 12345, 23456
            reader.ready_check_creation_fds = 1, 2, 3, 4
        self.final_copy_binding_change(change)

    def test_final_copy_kept_deadline_change_refuses_success(self):
        self.final_copy_binding_change(lambda:setattr(self.p.reader,'ready_check_deadline',self.f.deadline + 100))

    def test_binding_is_fixed_before_outer_ready_reread(self):
        original = self.offline.read_ready_original
        fired = []
        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            if self.p.reader.ready_check_binding is not None and not fired:
                fired.append(True)
                self.p.reader.nonce = 'changed-after-helper'
            return result
        with patch.object(self.offline,'read_ready_original',side_effect=read):self.rejected()
        self.assertEqual([True], fired)
        self.assertIsNotNone(self.held()['binding'])
        self.assertFalse(self.held().get('completed',False))
        self.assertEqual({'handle':'RELEASED','pipe':'RELEASED'},self.p.reader.ready_check_release)
        self.no_retry()

    def helper_handoff_change(self, change):
        original = offline_process.OfflineProcess.verify_ready
        originals = {}
        def completed(reader, *args):
            returned = original(reader, *args)
            originals.update(slot=self.held(), binding=returned[1],
                             request=reader.request, response=reader.ready_check_response,
                             input=self.held()['input'], original=self.held()['original'])
            self.pool.submit(change, reader, returned[1]).result(10)
            return returned
        with patch.object(offline_process.OfflineProcess,'verify_ready',completed):self.rejected()
        held = self.held()
        self.assertIs(originals['slot'], held)
        self.assertIs(self.p.reader, held['reader'])
        self.assertIs(originals['binding'], held['binding'])
        self.assertIs(self.p.reader.process, held['binding'][0][0])
        self.assertIs(self.p.reader.pipe, held['binding'][0][1])
        self.assertIs(self.p.reader.creation_resources, held['binding'][0][2])
        self.assertFalse(held.get('completed',False))
        self.assertNotIn('result',held)
        self.assertEqual(originals['input'],held['input'])
        self.assertEqual(originals['original'],held['original'])
        self.assertEqual({'handle':'RELEASED','pipe':'RELEASED'},self.p.reader.ready_check_release)
        observed = json.loads(self.p.reader.ready_check_result)
        for name in ('request','response'):
            self.assertEqual(originals[name],held[name])
            self.assertEqual(digest(held[name]),observed[name+'Sha256'])
        self.no_retry()

    def test_helper_return_binding_replacement_is_not_adopted(self):
        self.helper_handoff_change(lambda reader,binding:setattr(reader,'ready_check_binding',
                                                                (binding[0],binding[1])))

    def test_completed_helper_cannot_refix_binding_before_adoption(self):
        def change(reader, binding):
            reader.nonce = 'changed-before-adoption'
            with self.assertRaises(AttributeError):reader._ready_binding()
            reader.ready_check_binding = binding[0],binding[1]
        self.helper_handoff_change(change)

    def test_final_copy_worker_controller_change_refuses_success(self):
        self.final_copy(lambda:setattr(self.terminal['workers'][0],'controller',object()))

    def test_final_copy_deadline_refuses_success(self):
        self.final_copy(lambda:setattr(self.f,'clock',self.f.deadline))

    def test_entry_deadline_has_no_generation(self):
        self.f.clock = self.f.deadline
        self.rejected()
        self.assertEqual((0,0,0,0), self.p.effects())

    def test_response_deadline_has_no_release(self):
        self.p.after_receive = lambda:setattr(self.f,'clock',self.f.deadline)
        self.rejected()
        self.assertEqual(0,self.p.closes)
        self.no_retry()

    def test_duplicate_race_never_generates_twice(self):
        # 同じthreadの再入でもRegistryの保持枠取得後は再生成しない。
        self.p.after_send = lambda:self.rejected()
        self.read()
        self.assertEqual((1,1,1,1),self.p.effects())

    def test_concurrent_same_id_has_only_one_reader(self):
        original = offline_process.regular_bytes
        entered, resume = threading.Event(), threading.Event()
        def read(path, **kwargs):
            raw = original(path, **kwargs)
            if path.name == 'helper.jar' and self.key in self.offline.readers and not entered.is_set():
                entered.set();self.assertTrue(resume.wait(20))
            return raw
        with patch.object(offline_process,'regular_bytes',side_effect=read):
            future = self.pool.submit(self.read)
            try:
                self.assertTrue(entered.wait(20))
                self.rejected()
            finally:resume.set()
            self.assertEqual('OFFLINE_READY_VERIFIED',future.result(20)['status'])
        self.assertEqual((1,1,1,1),self.p.effects())

    def test_artifact_replaced_before_reader_snapshot_has_no_generation(self):
        original = offline_process.verify_bundle
        def changed(bundle):
            manifest = original(bundle)
            manifest['artifactId'] = '0'*64
            (bundle/'runtime.json').write_bytes(canonical(manifest))
            (bundle/'COMPLETE').write_text(manifest['artifactId'],encoding='utf-8')
            return manifest
        with patch.object(offline_process,'verify_bundle',side_effect=changed):self.rejected()
        self.assertEqual(0,self.p.effects()[0])
        self.no_retry()

    def preparation_inventory(self, change):
        process = runtime_process.OwnedProcess()
        original = offline_process.regular_bytes
        fired = []
        def read(path, **kwargs):
            raw = original(path, **kwargs)
            if path.name == 'helper.jar' and process.stage == 'JOB_CREATED' and not fired:
                fired.append(True);change()
            return raw
        with patch.object(runtime_process, 'OwnedProcess', return_value=process), \
             patch.object(self.p.adapter, 'OwnedProcess', return_value=process), \
             patch.object(self.p, 'read', side_effect=self.read), \
             patch.object(offline_process, 'regular_bytes', side_effect=read):
            self.p.windows_preparation('inventory')
        self.assertEqual([True],fired)
        self.assertIs(process,self.held()['reader'].process)
        self.assertNotIn('result',self.held())

    @unittest.skipUnless(os.name == 'nt','Windows生成準備を合成')
    def test_actual_preparation_inventory_commit_has_process_zero(self):
        self.preparation_inventory(self.commit)

    @unittest.skipUnless(os.name == 'nt','Windows生成準備を合成')
    def test_actual_preparation_inventory_deadline_has_process_zero(self):
        self.preparation_inventory(lambda:setattr(self.f,'clock',self.f.deadline))

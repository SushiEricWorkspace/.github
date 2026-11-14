"""公式Readyの固定証拠と新しいstart予約を実Registryへ結ぶ。"""
from copy import deepcopy
from contextlib import contextmanager
import json
import threading
import unittest
from unittest.mock import patch

from . import offline_control
from . import test_offline_verify_ready as readers
from .build_inputs import canonical, digest
from .registry import RegistryError


START = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'


class StartReservationTests(unittest.TestCase):
    def setUp(self):
        self.base = readers.VerifyReadyTests()
        self.addCleanup(self.base.doCleanups)
        self.base.setUp()
        self.runtime, self.offline, self.token = self.base.runtime, self.base.offline, self.base.token
        self.profile = b'fixed-approved-profile'
        self.approval = {'artifactId':self.base.terminal['row']['artifact'],
                         'runtimeManifestHash':digest((self.base.p.bundle/'runtime.json').read_bytes())}
        self.runtime.managed.approvals = {self.token['instanceId']:self.approval}
        self.runtime.managed.approved = lambda row,generation:(self.approval,self.profile)
        self.key = self.token['instanceId'], START

    def reserve(self, **changes):
        args = dict(operation_id=START, read_operation_id=readers.OPERATION,
            open_operation_id=self.base.o.base.opened['operationId'], ready=self.base.o.ready,
            deadline=self.base.f.deadline)
        args.update(changes)
        return self.runtime._offline_reserve_start(self.token, **args)

    def row(self):
        with self.runtime.registry.transaction(read_only=True) as db:
            return dict(self.runtime.registry._instance(db,self.token['instanceId']))

    def saved(self):
        with self.runtime.registry.transaction(read_only=True) as db:
            operation = db.execute('SELECT * FROM runtime_operations WHERE id=?',(START,)).fetchone()
            refs = [dict(v) for v in db.execute('SELECT * FROM reservations WHERE instance=?',(self.token['instanceId'],))]
        return dict(operation) if operation else None,refs

    def rejected(self, **changes):
        with self.assertRaises((RegistryError,OSError,EOFError,TimeoutError)):self.reserve(**changes)

    def no_retry(self):
        effects = self.base.p.effects()
        self.rejected()
        self.rejected(operation_id=OTHER,read_operation_id=readers.OTHER)
        self.assertEqual(effects,self.base.p.effects())

    def commit(self, what='epoch'):
        def write():
            with self.runtime.registry.transaction() as db:
                if what=='epoch':
                    db.execute('UPDATE instances SET epoch=epoch+1 WHERE id=?',(self.token['instanceId'],))
                elif what=='owner':
                    db.execute("UPDATE instances SET owner='changed-owner' WHERE id=?",(self.token['instanceId'],))
                elif what=='closed':
                    opened = self.base.o.base.opened['operationId']
                    record = json.loads(db.execute('SELECT response FROM runtime_operations WHERE id=?',(opened,)).fetchone()[0])
                    record['writerClosed']['state'] = 'UNKNOWN'
                    db.execute('UPDATE runtime_operations SET response=? WHERE id=?',(json.dumps(record),opened))
                elif what=='intent':
                    db.execute("UPDATE runtime_operations SET response='{}' WHERE id=?",(START,))
                elif what=='fingerprint':
                    db.execute("UPDATE runtime_operations SET fingerprint='changed' WHERE id=?",(START,))
                elif what=='reservation':
                    db.execute('DELETE FROM reservations WHERE operation=?',(START,))
                elif what=='gate':
                    db.execute('UPDATE run_admission SET revision=revision+1 WHERE run=?',(self.base.terminal['row']['run_id'],))
                elif what=='start':
                    row = self.base.terminal['row']
                    db.execute('INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)',
                               (OTHER,'competing','start',row['id'],'PENDING','{}'))
                    db.execute("UPDATE instances SET state='STARTING',pending=1,run_id='competing-run',runtime_proof=NULL,clean_proof=NULL WHERE id=?",(row['id'],))
                    db.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',
                               (OTHER,'artifact:'+row['artifact'],row['id'],row['lease'],row['epoch']))
        self.base.pool.submit(write).result(10)

    def test_reserves_new_runtime_run_and_keeps_original_ledgers(self):
        old = self.row()
        result, binding = self.reserve()
        row = self.row()
        operation, refs = self.saved()
        intent = json.loads(operation['response'])['offlineStart']
        self.assertEqual('OFFLINE_START_RESERVED',result['status'])
        self.assertEqual(('STARTING',1,result['runId']),(row['state'],row['pending'],row['run_id']))
        self.assertNotEqual(old['run_id'],row['run_id'])
        self.assertEqual(('start','PENDING'),(operation['command'],operation['status']))
        self.assertEqual((old['run_id'],self.base.o.ready),(intent['previousRunId'],intent['ready']))
        self.assertEqual('run-'+intent['nonce'],result['runId'])
        self.assertEqual([START],[v['operation'] for v in refs])
        self.assertEqual(self.base.o.before,self.base.f.saved())
        self.assertEqual((1,1,1,1),self.base.p.effects())
        self.assertEqual([self.base.f.deadline]*2,self.base.p.deadlines)
        self.assertNotIn(result['runId'],self.runtime.processes)
        self.assertIs(binding,self.offline.starts[self.key]['binding'])
        self.assertEqual(operation['response'],binding())
        with self.runtime.registry.transaction(read_only=True) as db:
            self.assertIsNone(db.execute('SELECT 1 FROM runs WHERE id=?',(result['runId'],)).fetchone())
        self.no_retry()

    def test_pending_binding_never_uses_stopped_callback(self):
        _, binding = self.reserve()
        with patch.object(self.offline,'_terminal',side_effect=AssertionError('旧停止照合へ戻りません')):
            binding()

    def test_start_write_transaction_has_no_native_or_file_read(self):
        transaction = self.runtime._transaction
        resources = self.offline._terminal_resources
        regular = offline_control.regular_bytes
        active = []
        @contextmanager
        def checked(instance,*,read_only=False):
            with transaction(instance,read_only=read_only) as db:
                writing = not read_only and 'intent' in self.offline.starts.get(self.key,{})
                if writing:active.append(True)
                try:yield db
                finally:
                    if writing:active.pop()
        def native(*args):
            self.assertFalse(active)
            return resources(*args)
        def read(*args,**kwargs):
            self.assertFalse(active)
            return regular(*args,**kwargs)
        with patch.object(self.runtime,'_transaction',checked), \
             patch.object(self.offline,'_terminal_resources',side_effect=native), \
             patch.object(offline_control,'regular_bytes',side_effect=read):
            self.reserve()[1]()

    def test_old_approval_cannot_admit_existing_start(self):
        self.base.o.base.start_blocked()
        self.reserve()
        with self.assertRaises(RegistryError):
            self.runtime.execute('start',dict(self.token,timeoutSeconds=2),operation_id='old-approval')

    def test_missing_independent_ready_has_no_helper_or_reservation(self):
        self.rejected(ready={})
        self.assertEqual((0,0,0,0),self.base.p.effects())
        self.assertEqual((None,[]),self.saved())
        self.assertNotIn(self.key,self.offline.starts)

    def test_same_operation_ids_have_no_helper(self):
        self.rejected(operation_id=readers.OPERATION)
        self.assertEqual((0,0,0,0),self.base.p.effects())

    def test_existing_start_id_has_no_helper_or_local_intent(self):
        stop = json.loads(self.base.terminal['row']['runtime_proof'])['stopOperationId']
        self.rejected(operation_id=stop)
        self.assertEqual((0,0,0,0),self.base.p.effects())
        self.assertEqual({},self.offline.starts)

    def test_entry_deadline_has_no_helper_or_reservation(self):
        self.base.f.clock = self.base.f.deadline
        self.rejected()
        self.assertEqual((0,0,0,0),self.base.p.effects())
        self.assertEqual((None,[]),self.saved())

    def final_copy(self, change, *, progress=(True,False)):
        original = deepcopy
        fired = []
        def copying(value, *args, **kwargs):
            copied = original(value,*args,**kwargs)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_START_RESERVED' and not fired:
                fired.append(True);change()
            return copied
        with patch.object(offline_control,'deepcopy',side_effect=copying):self.rejected()
        self.assertEqual([True],fired)
        held = self.offline.starts[self.key]
        self.assertEqual(progress,(held['reserved'],held['returned']))
        self.assertIs(self.base.p.reader,self.offline.readers[self.base.key]['reader'])
        self.assertIsNotNone(held['proof'])
        self.assertEqual((1,1,1,1),self.base.p.effects())
        self.no_retry()

    def test_final_copy_epoch_commit_refuses_return_and_keeps_intent(self):
        self.final_copy(self.commit)
        self.assertEqual('PENDING',self.saved()[0]['status'])

    def test_final_copy_closed_commit_refuses_return(self):
        self.final_copy(lambda:self.commit('closed'))

    def test_final_copy_reservation_loss_refuses_return(self):
        self.final_copy(lambda:self.commit('reservation'))

    def test_final_copy_intent_change_refuses_return(self):
        self.final_copy(lambda:self.commit('intent'))

    def test_final_copy_fingerprint_change_refuses_return(self):
        self.final_copy(lambda:self.commit('fingerprint'))

    def test_final_copy_old_gate_change_refuses_return(self):
        self.final_copy(lambda:self.commit('gate'))

    def test_final_copy_current_bytes_change_refuses_return(self):
        self.final_copy(lambda:self.base.o.current_path.write_bytes(self.base.o.raw+b' '))

    def test_final_copy_reader_binding_change_refuses_return(self):
        self.final_copy(lambda:setattr(self.base.p.reader,'nonce','changed'))

    def test_final_copy_terminal_binding_change_refuses_return(self):
        self.final_copy(lambda:self.base.terminal['snapshot'][0][0][0].update(nonce='changed'))

    def test_final_copy_terminal_cannot_refix_its_own_snapshot(self):
        def change():
            terminal = self.base.terminal
            terminal['controller'].nonce = 'changed'
            terminal['snapshot'] = self.offline._release_snapshot(
                terminal['controller'],terminal['workers'],terminal['history'])
        self.final_copy(change)

    def terminal_during_read(self, change):
        original = offline_control.regular_bytes
        armed,fired = [],[]
        def read(path,**kwargs):
            value = original(path,**kwargs)
            if armed and path.name=='current.json' and not fired:
                fired.append(True)
                change()
            return value
        with patch.object(offline_control,'regular_bytes',side_effect=read):
            self.final_copy(lambda:armed.append(True))
        self.assertEqual([True],fired)

    def test_terminal_change_during_post_reservation_file_read_refuses_return(self):
        def change():
            self.base.terminal['controller'].nonce = 'changed-after-native-check'
            self.base.terminal['controller'].process.handle = 97531
        self.terminal_during_read(change)

    def test_termination_raw_change_during_post_reservation_read_refuses_return(self):
        self.terminal_during_read(lambda:setattr(self.base.terminal['controller'],'termination_response',
                                                 self.base.terminal['controller'].termination_response+b' '))

    def test_final_copy_proof_change_refuses_return(self):
        self.final_copy(lambda:self.offline.starts[self.key]['proof'][0]['reader'].update(handlesReleased=False))

    def test_final_copy_proof_slot_replacement_refuses_return(self):
        def change():
            packet = self.offline.starts[self.key]['proof']
            self.offline.starts[self.key]['proof'] = packet[0],packet[1]
        self.final_copy(change)

    def test_final_copy_local_intent_change_refuses_return(self):
        self.final_copy(lambda:self.offline.starts[self.key].update(intent='{}'))

    def test_final_copy_completion_flags_change_refuses_return(self):
        self.final_copy(lambda:self.offline.starts[self.key].update(reserved=False,returned=True),progress=(False,True))

    def test_final_copy_row_run_change_refuses_return(self):
        def change():
            def write():
                with self.runtime.registry.transaction() as db:
                    db.execute("UPDATE instances SET run_id='other-run' WHERE id=?",(self.token['instanceId'],))
            self.base.pool.submit(write).result(10)
        self.final_copy(change)

    def test_final_copy_approval_change_refuses_return(self):
        self.final_copy(lambda:self.approval.update(runtimeManifestHash='0'*64))

    def test_final_copy_deadline_refuses_return(self):
        self.final_copy(lambda:setattr(self.base.f,'clock',self.base.f.deadline))

    def test_post_return_keeps_fixed_binding_and_refuses_replacement(self):
        _, binding = self.reserve()
        held = self.offline.starts[self.key]
        held['binding'] = lambda:None
        with self.assertRaises(RegistryError):binding()
        self.no_retry()

    def test_changed_return_copy_refuses_success(self):
        original = deepcopy
        def copying(value,*args,**kwargs):
            copied = original(value,*args,**kwargs)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_START_RESERVED':
                copied['runId'] = 'changed'
            return copied
        with patch.object(offline_control,'deepcopy',side_effect=copying):self.rejected()
        self.assertTrue(self.offline.starts[self.key]['reserved'])
        self.assertFalse(self.offline.starts[self.key]['returned'])
        self.no_retry()

    def test_registry_commit_after_read_before_reserve_has_reservation_zero(self):
        original = self.runtime.managed.approved
        fired = []
        def approved(*args):
            result = original(*args)
            if not fired:fired.append(True);self.commit()
            return result
        with patch.object(self.runtime.managed,'approved',side_effect=approved):self.rejected()
        self.assertEqual((None,[]),self.saved())
        self.assertEqual((1,1,1,1),self.base.p.effects())
        self.no_retry()

    def test_competing_registry_start_keeps_only_competitor_reservation(self):
        original = self.runtime.managed.approved
        calls = []
        def approved(*args):
            result = original(*args)
            calls.append(True)
            if len(calls)==2:self.commit('start')
            return result
        with patch.object(self.runtime.managed,'approved',side_effect=approved):self.rejected()
        operation, refs = self.saved()
        self.assertIsNone(operation)
        self.assertEqual([OTHER],[v['operation'] for v in refs])
        self.assertEqual('competing-run',self.row()['run_id'])
        self.assertFalse(self.offline.starts[self.key]['reserved'])
        self.no_retry()

    def test_approval_preparation_deadline_has_no_start_reservation(self):
        original = self.runtime.managed.approved
        def approved(*args):
            result = original(*args)
            self.base.f.clock = self.base.f.deadline
            return result
        with patch.object(self.runtime.managed,'approved',side_effect=approved):self.rejected()
        self.assertEqual((None,[]),self.saved())
        self.assertEqual((1,1,1,1),self.base.p.effects())
        self.no_retry()

    def test_completed_proof_changed_before_adoption_has_no_reservation(self):
        original = self.offline._verify_ready
        def verified(*args,**kwargs):
            packet = original(*args,**kwargs)
            packet[0]['reader']['handlesReleased'] = False
            return packet
        with patch.object(self.offline,'_verify_ready',side_effect=verified):self.rejected()
        self.assertEqual((None,[]),self.saved())
        self.assertEqual((1,1,1,1),self.base.p.effects())
        self.no_retry()

    def test_concurrent_start_intents_never_reserve_twice(self):
        original = self.offline._verify_ready
        entered, resume = threading.Event(),threading.Event()
        def verify(*args,**kwargs):
            entered.set()
            self.assertTrue(resume.wait(20))
            return original(*args,**kwargs)
        with patch.object(self.offline,'_verify_ready',side_effect=verify):
            future = self.base.pool.submit(self.reserve)
            try:
                self.assertTrue(entered.wait(20))
                self.rejected(operation_id=OTHER,read_operation_id=readers.OTHER)
            finally:resume.set()
            self.assertEqual('OFFLINE_START_RESERVED',future.result(20)[0]['status'])
        self.assertEqual((1,1,1,1),self.base.p.effects())

    def renew(self):
        old = self.row()
        self.base.pool.submit(self.runtime.registry.execute,'renew',dict(self.token,ttlSeconds=600),
                              operation_id='renew-start-proof').result(10)
        renewed = self.row()
        self.assertGreater(renewed['expires'],old['expires'])
        self.assertEqual({k:v for k,v in old.items() if k!='expires'},
                         {k:v for k,v in renewed.items() if k!='expires'})
        return old,renewed

    def test_renew_during_approval_preserves_start_binding(self):
        original = self.runtime.managed.approved
        renewed = []
        def approved(*args):
            result = original(*args)
            if not renewed:renewed.append(self.renew())
            return result
        with patch.object(self.runtime.managed,'approved',side_effect=approved):
            try:result,binding = self.reserve()
            except RegistryError as error:self.fail(f'正常renewで受付を失効: {error.code}')
        self.assertEqual('OFFLINE_START_RESERVED',result['status'])
        self.assertEqual(renewed[0][1]['expires'],self.row()['expires'])
        self.assertEqual(self.saved()[0]['response'],binding())
        self.assertEqual([self.base.f.deadline]*2,self.base.p.deadlines)
        self.assertEqual((1,1,1,1),self.base.p.effects())

    def test_renew_after_reservation_uses_current_expiry_without_extending_deadline(self):
        _,binding = self.reserve()
        old,renewed = self.renew()
        with patch.object(self.runtime.registry,'clock',return_value=old['expires']+1):
            try:checked = binding()
            except RegistryError as error:self.fail(f'正常renewでcallbackを失効: {error.code}')
            self.assertEqual(self.saved()[0]['response'],checked)
        self.assertEqual(renewed['expires'],self.row()['expires'])
        self.assertEqual(self.base.f.deadline,self.offline.starts[self.key]['deadline'])
        self.base.f.clock = self.base.f.deadline
        with self.assertRaises(RegistryError) as caught:binding()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.no_retry()

    def test_callback_rejects_current_lease_expiry(self):
        _,binding = self.reserve()
        _,renewed = self.renew()
        with patch.object(self.runtime.registry,'clock',return_value=renewed['expires']):
            with self.assertRaises(RegistryError) as caught:binding()
        self.assertEqual('LEASE_EXPIRED',caught.exception.code)
        self.assertEqual('PENDING',self.saved()[0]['status'])
        self.no_retry()

    def before_write(self, change):
        transaction = self.runtime._transaction
        fired = []
        @contextmanager
        def checked(instance,*,read_only=False):
            if not read_only and 'intent' in self.offline.starts.get(self.key,{}) and not fired:
                fired.append(True);change()
            with transaction(instance,read_only=read_only) as db:yield db
        with patch.object(self.runtime,'_transaction',checked):self.rejected()
        self.assertEqual([True],fired)
        self.assertEqual((None,[]),self.saved())
        self.assertFalse(self.offline.starts[self.key]['reserved'])
        self.assertFalse(self.offline.starts[self.key]['returned'])
        self.assertEqual((1,1,1,1),self.base.p.effects())
        self.no_retry()

    def test_before_write_approval_value_change_has_no_reservation(self):
        self.before_write(lambda:self.approval.update(runtimeManifestHash='0'*64))

    def test_before_write_approval_object_replacement_has_no_reservation(self):
        self.before_write(lambda:self.runtime.managed.approvals.update({self.token['instanceId']:dict(self.approval)}))

    def test_before_write_fixed_owner_commit_has_no_reservation(self):
        self.before_write(lambda:self.commit('owner'))

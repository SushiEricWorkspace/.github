"""確定game退出から独立Readyを次Cへ渡す実Registry試験。"""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from . import offline_control, offline_process, runtime_control
from . import test_game_exit_stop as stops
from . import test_offline_process as processes
from .build_inputs import canonical, digest
from .registry import RegistryError


class ReadyPipe(processes.Pipe):
    def send(self, request, deadline, verify):
        verify()
        self.sent.append(deepcopy(request))
        case = self.current
        process = case.peer.adapter.processes[-1]
        if case.failure == 'before':
            process.code = 1
            raise RegistryError('PROOF_MISMATCH', 'fixture Data作用前拒否')
        case.assertEqual('Ready', case.case.data_phase)
        expected = {key:request[field] for key,field in (
            ('worldUuid','worldUuid'), ('runUuid','runUuid'),
            ('stateSha256','expectedStateSha256'), ('stateRevision','expectedRevision'))}
        if canonical(expected) != canonical(case.ready):
            process.code = 1
            raise RegistryError('PROOF_MISMATCH', 'fixture Data Ready binding不一致')
        case.case.data_phase = 'OfflineIdle'
        self.effects += 1
        case.after_send()
        if case.failure == 'after':
            process.code = 1
            raise OSError('fixture Data owner取得後失敗')

    def receive(self, deadline, verify):
        verify()
        case = self.current
        if case.failure == 'lost':
            case.peer.adapter.processes[-1].code = 1
            raise EOFError('fixture OPEN応答喪失')
        request = self.sent[-1]
        response = {key:request[key] for key in ('operationId','requestHash','sessionId','nonce')}
        response.update(status='OPEN', stateSha256='e'*64,
                        stateRevision=request['expectedRevision']+1, runUuid=processes.RUN)
        case.after_receive()
        return response


class GameReadyTests(unittest.TestCase):
    def setUp(self):
        case = stops.StopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.stop_case(direct=True)
        GameReadyTests.configure(self, case)

    def configure(self, case):
        self.case, self.runtime, self.f = case, case.runtime, case.f
        self.instance, self.peer = case.instance, case.peer
        self.token = deepcopy(self.f.token)
        self.ready = deepcopy(case.ready)
        self.slot, self.originals, self.binding = self.runtime._game_exit(case.session['run'], case.session['stop_operation'])
        self.stop_id = case.session['stop_operation']
        self.operation = 'ready-next-open'
        with self.runtime._transaction(self.instance, read_only=True) as db:
            self.revision = db.execute('SELECT revision FROM run_admission WHERE run=?', (case.session['run'],)).fetchone()[0]
        identity = self.peer.adapter.identity
        self.peer.adapter.identity = lambda pid: None if pid == case.m.pid else identity(pid)
        self.pipes = []
        self.failure = None
        self.assertEqual('Ready',case.data_phase)
        self.after_send = self.after_receive = lambda: None
        ReadyPipe.current = self
        self.enterContext(patch.object(offline_process, 'ProxyPipe', ReadyPipe))

    def open(self, ready=None):
        return self.runtime._offline_open(self.token, operation_id=self.operation, revision=self.revision,
            ready=self.ready if ready is None else ready, deadline=20)

    def query(self, sql, args=()):
        return self.case.source.query(sql,args)

    def effects(self):
        return sum(p.starts for p in self.peer.adapter.processes[1:]), sum(len(p.sent) for p in self.pipes), sum(p.effects for p in self.pipes)

    def snapshot(self):
        return self.query('SELECT * FROM runtime_operations ORDER BY id'), self.query('SELECT * FROM reservations ORDER BY operation,reference'), self.f.state()

    def rejected(self, change, ready=None):
        change()
        before = self.snapshot()
        with self.assertRaises(RegistryError):
            self.open(ready)
        self.assertEqual((0,0,0),self.effects())
        self.assertEqual(before,self.snapshot())
        self.assertEqual('Ready',self.case.data_phase)
        self.assertFalse(self.case.m.closed)
        self.case.c.process.close.assert_not_called()

    def pending(self):
        self.assertEqual('PENDING',self.query('SELECT status FROM runtime_operations WHERE id=?',(self.operation,))[0]['status'])
        self.assertEqual(1,len(self.query('SELECT * FROM reservations WHERE operation=?',(self.operation,))))
        self.assertEqual(1,self.f.state()['pending'])

    def no_retry(self):
        before=self.effects(),self.snapshot()
        with self.assertRaises(RegistryError):self.open()
        self.assertEqual(before,(self.effects(),self.snapshot()))

    def replace_proof(self, change):
        with self.runtime._transaction(self.instance) as db:
            row=self.f.registry._instance(db,self.instance)
            proof=json.loads(row['runtime_proof']);change(proof)
            db.execute('UPDATE instances SET runtime_proof=? WHERE id=?',(json.dumps(proof),self.instance))
            db.execute('UPDATE runtime_operations SET response=? WHERE id=?',(json.dumps(proof),self.stop_id))

    def result_change(self, field, value):
        observed=json.loads(self.slot['result']);observed[field]=value
        self.slot['result']=self.case.c.game_exit_result=canonical(observed)
        self.case.c.record['gameExit']=observed
        self.slot['observer_snapshot']=canonical(self.case.c.record)
        self.replace_proof(lambda p:p['gameExit'].update(observationSha256=digest(self.slot['result']),
            observerRecordSha256=digest(self.slot['observer_snapshot'])))

    def test_public_stop_then_independent_ready_reaches_next_c_open(self):
        self.case.clock=11
        expected=deepcopy(self.ready)
        response=self.open()
        self.assertEqual('OPEN',response['status'])
        self.assertEqual((1,1,1),self.effects())
        self.assertEqual('OfflineIdle',self.case.data_phase)
        self.assertEqual(expected,self.ready)
        request=self.pipes[0].sent[0]
        self.assertEqual(expected['stateSha256'],request['expectedStateSha256'])
        self.assertEqual(expected['stateRevision'],request['expectedRevision'])
        self.assertEqual(expected['runUuid'],request['runUuid'])
        self.assertEqual(expected['stateRevision']+1,response['stateRevision'])
        self.assertFalse(self.runtime._offline.controllers[self.instance].startup_acknowledged)
        self.pending();self.no_retry()
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        self.assertIs(self.slot,self.runtime._game_exit(self.case.session['run'],self.stop_id)[0])

    def test_local_reference_without_completed_stop_is_rejected(self):
        with self.runtime._transaction(self.instance) as db:
            db.execute("UPDATE runtime_operations SET status='PENDING' WHERE id=?",(self.stop_id,))
        self.rejected(lambda:None)

    def test_missing_terminal_resource_is_rejected(self):
        self.rejected(lambda:self.runtime.game_exits.pop((self.case.session['run'],self.stop_id)))

    def test_deadline_before_preflight_preserves_originals(self):
        self.case.clock=20
        self.rejected(lambda:None)

    def test_changed_observation_between_preflight_and_reservation_has_no_effect(self):
        identity=self.runtime.process_adapter.identity
        def changed(pid):
            result=identity(pid)
            if pid==self.case.m.pid:self.slot['result']+=b' '
            return result
        with patch.object(self.runtime.process_adapter,'identity',side_effect=changed):
            self.rejected(lambda:None)

    def test_changed_observation_after_generation_rejects_before_ipc(self):
        original=self.runtime.process_adapter.OwnedProcess
        def changed():
            process=original()
            start=process.start
            def started(*args,**kwargs):
                start(*args,**kwargs)
                self.slot['result']+=b' '
            process.start=started
            return process
        with patch.object(self.runtime.process_adapter,'OwnedProcess',side_effect=changed):
            with self.assertRaises(RegistryError):self.open()
        self.assertEqual((1,0,0),self.effects())
        self.pending();self.no_retry()

    def test_deadline_after_response_keeps_pending_without_recording_success(self):
        self.after_receive=lambda:setattr(self.case,'clock',21)
        with self.assertRaises(RegistryError) as caught:self.open()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual((1,1,1),self.effects())
        self.assertNotIn('startupResponseSha256',json.loads(self.query('SELECT response FROM runtime_operations WHERE id=?',(self.operation,))[0]['response']))
        self.pending();self.no_retry()

    def test_result_commit_deadline_has_no_return_or_extra_effect(self):
        original=self.f.registry._instance
        def observed(db,instance):
            row=original(db,instance)
            db.set_trace_callback(lambda sql:setattr(self.case,'clock',21)
                if sql.startswith('UPDATE runtime_operations SET response=') and self.operation in sql else None)
            return row
        with patch.object(self.f.registry,'_instance',side_effect=observed):
            with self.assertRaises(RegistryError) as caught:self.open()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual((1,1,1),self.effects())
        self.assertIn('startupResponseSha256',json.loads(self.query('SELECT response FROM runtime_operations WHERE id=?',(self.operation,))[0]['response']))
        self.pending();self.no_retry()

    def test_deadline_during_reservation_checks_has_effect_zero(self):
        original=self.f.registry._instance
        def observed(db,instance):
            row=original(db,instance)
            if db.execute('PRAGMA query_only').fetchone()[0]==0:
                db.set_trace_callback(lambda sql:setattr(self.case,'clock',21)
                    if sql.startswith('SELECT 1 FROM operations WHERE id=') and self.operation in sql else None)
            return row
        with patch.object(self.f.registry,'_instance',side_effect=observed):
            self.rejected(lambda:None)

    def test_deadline_during_final_response_hash_prevents_success_record(self):
        original=offline_control.digest
        def hashed(raw):
            value=original(raw)
            controller=self.runtime._offline.controllers.get(self.instance) if self.runtime._offline else None
            if controller is not None and controller.ready and raw==controller.response:
                self.case.clock=21
            return value
        with patch.object(offline_control,'digest',side_effect=hashed):
            with self.assertRaises(RegistryError) as caught:self.open()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual((1,1,1),self.effects())
        self.assertNotIn('startupResponseSha256',json.loads(self.query('SELECT response FROM runtime_operations WHERE id=?',(self.operation,))[0]['response']))
        self.pending();self.no_retry()

    def test_observation_changed_in_final_transaction_prevents_success_record(self):
        original=self.f.registry._instance
        def changed(db,instance):
            row=original(db,instance)
            controller=self.runtime._offline.controllers.get(instance) if self.runtime._offline else None
            if controller is not None and controller.ready and db.execute('PRAGMA query_only').fetchone()[0]==0:
                self.slot['result']+=b' '
            return row
        with patch.object(self.f.registry,'_instance',side_effect=changed):
            with self.assertRaises(RegistryError):self.open()
        self.assertEqual((1,1,1),self.effects())
        self.assertNotIn('startupResponseSha256',json.loads(self.query('SELECT response FROM runtime_operations WHERE id=?',(self.operation,))[0]['response']))
        self.pending();self.no_retry()

    def test_ready_numeric_type_mismatch_with_matching_refs_is_rejected(self):
        def changed():
            response=json.loads(self.case.c.game_exit_response)
            response['stateRevision']=float(response['stateRevision'])
            self.case.c.game_exit_response=canonical(response)
            self.result_change('ready',dict(self.ready,stateRevision=float(self.ready['stateRevision'])))
            self.result_change('responseSha256',digest(self.case.c.game_exit_response))
            self.replace_proof(lambda p:p['gameExit'].update(responseSha256=digest(self.case.c.game_exit_response)))
        self.rejected(changed)

    def test_final_serialization_deadline_prevents_success_record(self):
        original=offline_control.json.dumps
        def serialized(value,*args,**kwargs):
            raw=original(value,*args,**kwargs)
            if type(value) is dict and set(value)=={'binding','startupResponseSha256'}:
                self.case.clock=21
            return raw
        with patch.object(offline_control.json,'dumps',side_effect=serialized):
            with self.assertRaises(RegistryError) as caught:self.open()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual((1,1,1),self.effects())
        self.assertNotIn('startupResponseSha256',json.loads(self.query('SELECT response FROM runtime_operations WHERE id=?',(self.operation,))[0]['response']))
        self.pending();self.no_retry()

    def test_pid_lookup_deadline_prevents_next_c_creation(self):
        original=self.runtime.process_adapter.identity
        def observed(pid):
            value=original(pid)
            controller=self.runtime._offline.controllers.get(self.instance)
            if pid==self.case.m.pid and controller is not None and controller.pipe is not None:
                self.case.clock=21
            return value
        with patch.object(self.runtime.process_adapter,'identity',side_effect=observed):
            with self.assertRaises(RegistryError) as caught:self.open()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual((0,0,0),self.effects())
        self.pending();self.no_retry()

    def preparation_deadline(self, boundary):
        before=self.runtime._game_exit(self.case.session['run'],self.stop_id)
        if boundary=='command':
            original=offline_process.offline_controller_command
            def delayed(bundle):
                value=original(bundle);self.case.clock=21
                return value
            self.enterContext(patch.object(offline_process,'offline_controller_command',side_effect=delayed))
        elif boundary=='journal':
            original=offline_process.write_json
            def recorded(path,value):
                original(path,value)
                if value.get('role')=='c' and value.get('creation') is None:self.case.clock=21
            self.enterContext(patch.object(offline_process,'write_json',side_effect=recorded))
        else:
            original=offline_process.make_private
            def private(path,**options):
                original(path,**options)
                if path.name=='stderr.log':self.case.clock=21
            self.enterContext(patch.object(offline_process,'make_private',side_effect=private))
        with self.assertRaises(RegistryError) as caught:self.open()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual((0,0,0),self.effects())
        controller=self.runtime._offline.controllers[self.instance]
        self.assertIsNone(controller.process.pid)
        after=self.runtime._game_exit(self.case.session['run'],self.stop_id)
        self.assertIs(before[0],after[0]);self.assertEqual(before[1:],after[1:])
        self.assertFalse(self.case.m.closed);self.case.c.process.close.assert_not_called()
        self.pending();self.no_retry()

    def test_command_preparation_deadline_has_zero_start(self):self.preparation_deadline('command')
    def test_initial_journal_deadline_has_zero_start(self):self.preparation_deadline('journal')
    def test_stderr_preparation_deadline_has_zero_start(self):self.preparation_deadline('stderr')

    def failure_case(self, failure):
        self.failure=failure
        with self.assertRaises((RegistryError,OSError,EOFError)):self.open()
        self.assertEqual((1,1,0 if failure=='before' else 1),self.effects())
        self.assertEqual(1,self.peer.adapter.processes[-1].code)
        self.assertEqual('Ready' if failure=='before' else 'OfflineIdle',self.case.data_phase)
        self.pending();self.no_retry()

    def test_data_rejection_has_effect_zero(self):self.failure_case('before')
    def test_data_failure_after_owner_has_no_additional_effect(self):self.failure_case('after')
    def test_open_response_loss_has_no_additional_effect(self):self.failure_case('lost')


def ready_case(field,value):
    def test(self):
        ready=dict(self.ready);ready[field]=value(self) if callable(value) else value
        self.rejected(lambda:None,ready)
    return test

for field,value in (('worldUuid','00000000-0000-4000-8000-999999999999'),('runUuid','00000000-0000-4000-8000-999999999999'),
                    ('stateSha256','f'*64),('stateRevision',lambda s:s.ready['stateRevision']+1)):
    setattr(GameReadyTests,'test_independent_ready_'+field+'_mismatch',ready_case(field,value))


def retained_case(name,change):
    def test(self):self.rejected(lambda:change(self))
    return test

for name,change in (
    ('candidate',lambda s:s.slot['candidate'].update(stateRevision=999)),
    ('observation',lambda s:s.slot.update(result=s.slot['result']+b' ')),
    ('request',lambda s:setattr(s.case.c,'request',s.case.c.request+b' ')),
    ('response',lambda s:setattr(s.case.c,'game_exit_response',s.case.c.game_exit_response+b' ')),
    ('record',lambda s:s.case.c.record.update(commandHash='0'*64)),
    ('descriptor',lambda s:s.runtime.game_exits[(s.case.session['run'],s.stop_id)][1].update(descriptor=b'changed')),
    ('requested',lambda s:s.runtime.game_exits[(s.case.session['run'],s.stop_id)][1].update(save_request=b'0000changed')),
    ('completion',lambda s:s.runtime.game_exits[(s.case.session['run'],s.stop_id)][1].update(completion_response=b'0000changed')),
    ('stop',lambda s:s.slot.update(stop=('old',s.slot['stop'][1],s.slot['stop'][2])))):
    setattr(GameReadyTests,'test_retained_'+name+'_mismatch',retained_case(name,change))


def result_case(field,value):
    def test(self):self.rejected(lambda:self.result_change(field,value(self) if callable(value) else value))
    return test

for field,value in (('status','UNKNOWN'),('exitCode',1),('allChildrenExited',False),
                    ('operationId','other-stop'),('requestSha256','0'*64),('responseSha256','0'*64),
                    ('ready',lambda s:dict(s.ready,stateSha256='f'*64))):
    setattr(GameReadyTests,'test_result_'+field+'_mismatch_with_matching_refs',result_case(field,value))


class GameReadyRaceTests(unittest.TestCase):
    def setUp(self):
        GameReadyTests.setUp(self)

    def race(self,winner):
        from . import test_offline_control_concurrency as concurrency
        from . import test_runtime_control as runtimes
        self.old_pid=self.case.m.pid
        self.runtime.process_adapter=self.f.adapter
        self.runtime.managed=runtimes.FakeManaged(self.runtime)
        with patch.object(runtime_control.time,'sleep',lambda _:None):
            concurrency.OfflineConcurrencyTests.race(self,winner)

    def test_next_open_first_preserves_real_reservation_and_blocks_start(self):self.race('offline')
    def test_start_first_preserves_real_reservation_and_blocks_next_open(self):self.race('start')


def terminal_binding_case(field):
    def test(self):
        def changed():
            key=(self.case.session['run'],self.stop_id)
            slot,originals,raw=self.runtime.game_exits[key]
            binding=json.loads(raw);binding[field]='other'
            self.runtime.game_exits[key]=(slot,originals,canonical(binding))
        self.rejected(changed)
    return test

for field in ('instanceId','runId','stopOperationId','stopFingerprint','controlOperationId'):
    setattr(GameReadyTests,'test_terminal_binding_'+field+'_mismatch',terminal_binding_case(field))


def terminal_stop_case(index):
    def test(self):
        def changed():
            value=list(self.slot['stop']);value[index]='other';self.slot['stop']=tuple(value)
        self.rejected(changed)
    return test

for index,field in ((1,'fingerprint'),(2,'controlOperation')):
    setattr(GameReadyTests,'test_terminal_stop_'+field+'_mismatch',terminal_stop_case(index))


class SelectedReadyIntegrationTests(unittest.TestCase):
    def test_selected_supply_stop_then_explicit_ready_reaches_next_open(self):
        from . import test_selected_game_exit as selections
        selected=selections.SelectedGameExitTests();selected.setUp()
        self.addCleanup(selected.doCleanups)
        selected.run_case()
        runtime=selected.runtime
        self.assertIs(runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        self.assertNotIn(selected.case.instance,selected.managed.sessions)
        raw=selected.session['game_exit_input']['raw']
        slot,originals,binding=runtime._game_exit(selected.session['run'],'public-stop')
        self.assertEqual(raw,slot['input']);self.assertEqual(raw,originals['game_exit_input'])
        self.assertEqual('Ready',selected.case.data_phase)
        next_open=GameReadyTests();self.addCleanup(next_open.doCleanups)
        next_open.configure(selected.case)
        expected=deepcopy(next_open.ready)
        selected.case.clock=json.loads(raw)['deadline']+1
        self.assertLess(selected.case.clock,20)
        response=next_open.open()
        self.assertEqual('OPEN',response['status'])
        self.assertEqual((1,1,1),next_open.effects())
        self.assertEqual('OfflineIdle',selected.case.data_phase)
        request=next_open.pipes[0].sent[0]
        self.assertEqual(expected,{key:request[field] for key,field in (
            ('worldUuid','worldUuid'),('runUuid','runUuid'),
            ('stateSha256','expectedStateSha256'),('stateRevision','expectedRevision'))})
        held,retained,current=runtime._game_exit(selected.session['run'],'public-stop')
        self.assertIs(slot,held);self.assertEqual(originals,retained);self.assertEqual(binding,current)
        self.assertEqual('COMPLETED',selected.case.source.query('SELECT status FROM runtime_operations WHERE id=?',('public-stop',))[0]['status'])
        next_open.pending();next_open.no_retry()

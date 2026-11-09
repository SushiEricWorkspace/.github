"""観測済みgame退出証拠と既存stopの結束を実Registryで検査する。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import threading
import unittest
import gc
import weakref
from contextlib import contextmanager
from unittest.mock import Mock, patch

from . import mod_control_runtime as target
from . import runtime_control
from . import test_game_exit_connection as connection
from . import test_game_exit_inputs as inputs
from . import test_managed_save_evidence as saves
from . import test_offline_process as processes
from .registry import RegistryError


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.base=connection.ObserverTests();self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.base.observe()
        self.managed,self.session=self.base.managed,self.base.session
        self.slot=self.session['game_exit'];self.c=self.slot['observer']

    def evidence(self):
        with self.base.case.f.transaction(read_only=True) as db:
            self.managed.save_evidence(db,'slot-a',self.session,self.base.deadline)
            return self.managed.game_exit_evidence('slot-a',self.session,self.base.deadline)

    def rejected(self, change):
        before=self.base.effects(),self.base.case.base.unchanged_effects()
        change()
        with self.assertRaises(RegistryError):self.evidence()
        self.assertEqual(before,(self.base.effects(),self.base.case.base.unchanged_effects()))
        self.base.m.close.assert_not_called();self.c.process.close.assert_not_called()
        self.base.no_retry()

    def result_change(self, key, value):
        result=json.loads(self.slot['result']);result[key]=value
        self.slot['result']=self.c.game_exit_result=target.canonical(result)
        self.c.record['gameExit']=result
        self.slot['observer_snapshot']=target.canonical(self.c.record)

    def test_only_hash_references_and_explicit_candidate_are_returned(self):
        value=self.evidence()
        self.assertEqual({'candidate','observationSha256','requestSha256','responseSha256','observerRecordSha256'},value.keys())
        self.assertEqual(self.base.candidate,value['candidate'])
        self.assertEqual(target.digest(self.slot['result']),value['observationSha256'])
        self.assertEqual(target.digest(self.c.request),value['requestSha256'])
        self.assertEqual(target.digest(self.c.game_exit_response),value['responseSha256'])
        self.assertEqual(target.digest(self.slot['observer_snapshot']),value['observerRecordSha256'])
        value['candidate']['stateRevision']+=1
        self.assertEqual(self.base.candidate,self.evidence()['candidate'])
        self.assertEqual((1,1,1,1),self.base.effects())

    def test_unfinished_slot_is_not_accepted(self):self.rejected(lambda:self.slot.pop('result'))
    def test_original_session_is_required(self):self.rejected(lambda:self.managed.sessions.pop('slot-a'))
    def test_observer_object_is_not_replaceable(self):
        other=Mock(pid=self.c.process.pid,group=self.c.process.group)
        self.rejected(lambda:setattr(self.c,'process',other))
    def test_observer_record_is_fixed(self):self.rejected(lambda:self.c.record['creation'].update(pid=999))
    def test_observer_frame_is_fixed(self):self.rejected(lambda:self.c.game_exit_frame.update(expectedRevision=99))
    def test_request_bytes_are_fixed(self):self.rejected(lambda:setattr(self.c,'request',self.c.request+b' '))
    def test_response_bytes_are_fixed(self):self.rejected(lambda:setattr(self.c,'game_exit_response',self.c.game_exit_response+b' '))
    def test_candidate_revision_is_fixed(self):self.rejected(lambda:self.slot['candidate'].update(stateRevision=99))
    def test_original_state_bytes_are_fixed(self):
        self.rejected(lambda:self.slot.update(inputs=(self.slot['inputs'][0],b'changed',self.slot['inputs'][2])))
    def test_original_proof_bytes_are_fixed(self):
        self.rejected(lambda:self.slot.update(inputs=(self.slot['inputs'][0],self.slot['inputs'][1],b'changed')))
    def test_deadline_after_local_binding_is_rejected(self):
        self.base.clock=self.base.deadline+1
        with self.assertRaises(RegistryError) as caught:self.evidence()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual((1,1,1,1),self.base.effects())


    def test_original_m_object_is_fixed(self):
        old,raw=self.base.runtime.game_processes[self.session['run']]
        replacement=Mock(pid=old.pid,group=old.group)
        self.rejected(lambda:self.base.runtime.game_processes.update({self.session['run']:(replacement,raw)}))

    def test_observer_launch_record_is_fixed(self):
        self.rejected(lambda:self.c.record.update(commandHash='0'*64))

    def test_local_result_bytes_are_fixed(self):
        self.rejected(lambda:setattr(self.c,'game_exit_result',self.c.game_exit_result+b' '))

    def test_deadline_after_m_binding_is_rejected(self):
        original=self.base.runtime._game_process
        calls=[0]
        def observe(*args):
            value=original(*args);calls[0]+=1
            if calls[0]==2:self.base.clock=self.base.deadline+1
            return value
        with patch.object(self.base.runtime,'_game_process',side_effect=observe):
            with self.assertRaises(RegistryError) as caught:self.evidence()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual((1,1,1,1),self.base.effects())

def result_case(key,value):
    def test(self):self.rejected(lambda:self.result_change(key,value(self) if callable(value) else value))
    return test

for key,value in (
    ('status','UNKNOWN'),('exitCode',1),('allChildrenExited',False),('operationId','old-operation'),
    ('requestSha256','0'*64),('responseSha256','0'*64),('gameGroup','other'),('observerGroup','other'),
    ('gameOwner',lambda s:dict(s.base.session['context']['owner'],startId='other')),
    ('observer',lambda s:dict(s.c.data_identity,startId='other'))):
    setattr(EvidenceTests,'test_result_'+key+'_mismatch_with_matching_outer_record',result_case(key,value))

def identity_case(field,value):
    def test(self):
        self.rejected(lambda:self.c.data_identity.update({field:value}))
    return test

for field,value in (('pid',999),('startId','other'),('hostHash','0'*64),('bootId','other')):
    setattr(EvidenceTests,'test_observer_'+field+'_mismatch',identity_case(field,value))


class StopTests(unittest.TestCase):
    def setUp(self):
        self.source=saves.SaveEvidenceRegistryTests();self.source.setUp();self.addCleanup(self.source.doCleanups)
        self.base,self.f,self.runtime,self.managed=self.source.base,self.source.f,self.source.runtime,self.source.managed
        self.instance=self.source.instance
        self.peer=processes.OfflineProcessTests();self.peer.setUp();self.addCleanup(self.peer.doCleanups)

    @property
    def c(self):return self.session['game_exit']['observer']

    def publish(self):
        state,proof=inputs.originals(self.session)
        proof_raw=inputs.encode_original(proof);state['payload']['saveProofSha256']=target.digest(proof_raw)
        state_raw=inputs.encode_original(state)
        candidate={k:state[k] for k in ('worldUuid','runUuid','stateRevision')}
        candidate['stateSha256']=target.digest(state_raw)
        root=Path(target.wire.decode_descriptor(self.session['raw'])['worldRoot'])/'sushieric/run-state'
        for p,raw in ((root/'states'/(candidate['stateSha256']+'.json'),state_raw),
                      (root/'saveproofs'/(state['payload']['saveProofSha256']+'.json'),proof_raw)):
            p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
        connection.configure(self,self.runtime,self.managed,self.session,candidate,self.peer)

    def observe(self):return self.managed.observe_game_exit(self.instance,self.session,self.candidate,self.deadline)

    def stop_case(self, *, direct=False, change=None, timeout=False, unfinished=False, overlap=False, loss=False, after=False, committed_timeout=False):
        self.session=self.base.start()
        reserved,proceed=threading.Event(),threading.Event()
        original=self.runtime._direct_stop if direct else self.runtime._stop
        before_final,release_final=threading.Event(),threading.Event()
        def held(*args):
            reserved.set()
            if not proceed.wait(10):raise TimeoutError('試験のstop予約境界')
            result=original(*args)
            if not direct and change:
                before_final.set()
                if not release_final.wait(10):raise TimeoutError('試験のstop最終照合境界')
            return result
        method='_direct_stop' if direct else '_stop'
        self.enterContext(patch.object(self.runtime,method,side_effect=held))
        future=None
        with ThreadPoolExecutor(max_workers=1) as pool:
            if direct:
                self.base.stop_ready.set();self.base.save_ready.set()
                self.assertTrue(reserved.wait(10))
            else:
                future=pool.submit(self.source.stop)
                self.assertTrue(reserved.wait(10))
                self.base.stop_ready.set();self.base.save_ready.set()
                self.source.join(self.session)
            self.assertTrue(self.session['done'].is_set());self.assertIsNone(self.session['error'])
            self.publish()
            if unfinished:
                self.session['game_exit']={'candidate':deepcopy(self.candidate)}
            else:
                if loss or after:
                    self.failure='lost' if loss else 'after'
                    with self.assertRaises(EOFError if loss else OSError):self.observe()
                    self.assertEqual(1,self.c.process.code)
                else:self.observe()
                self.assertEqual(1,self.pipes[0].effects)
            before=self.source.query('SELECT * FROM reservations ORDER BY operation,reference')
            run_before=self.source.query('SELECT record FROM runs WHERE id=?',(self.session['run'],))[0]['record']
            if overlap:
                with self.assertRaises(RegistryError):self.f.start('overlapping-start')
                self.assertEqual(before,self.source.query('SELECT * FROM reservations ORDER BY operation,reference'))
                self.assertEqual(1,len(self.f.adapter.created))
            if direct and change or timeout:
                original_evidence=self.managed.game_exit_evidence
                count=[0]
                def evidence(*args):
                    value=original_evidence(*args);count[0]+=1
                    if timeout and count[0]==3:self.clock=self.deadline+1
                    elif direct and change and count[0]==2:self.c.record['identity']['startId']='changed'
                    return value
                self.enterContext(patch.object(self.managed,'game_exit_evidence',side_effect=evidence))
            if committed_timeout:
                original_transaction=self.f.registry.transaction
                @contextmanager
                def transaction(*args,**kwargs):
                    with original_transaction(*args,**kwargs) as db:
                        db.set_trace_callback(lambda sql:setattr(self,'clock',self.deadline+1)
                            if sql.startswith("UPDATE runtime_operations SET status='COMPLETED',response=") and 'gameExit' in sql else None)
                        yield db
                self.enterContext(patch.object(self.f.registry,'transaction',side_effect=transaction))
            try:
                proceed.set()
                if not direct and change:
                    self.assertTrue(before_final.wait(10))
                    self.c.record['identity']['startId']='changed'
            finally:release_final.set()
            if direct:
                self.source.join(self.session)
                error=self.session['error']
                result=None
            else:
                if change or timeout or unfinished or loss or after or committed_timeout:
                    with self.assertRaises(RegistryError) as caught:future.result(10)
                    error=caught.exception;result=None
                else:result=future.result(10);error=None
            parent=self.source.query('SELECT * FROM runtime_operations WHERE id=?',(self.session['stop_operation'],))[0]
            if committed_timeout:
                self.assertIsInstance(error,RegistryError);self.assertEqual('STOP_TIMEOUT',error.code)
                self.assertEqual('COMPLETED',parent['status'])
                self.assertEqual(target.digest(self.session['game_exit']['result']),json.loads(parent['response'])['gameExit']['observationSha256'])
                self.assertEqual('RECOVERY_REQUIRED' if direct else 'LEASED_STOPPED',self.f.state()['state']);self.assertEqual(0,self.f.state()['pending'])
                self.assertNotIn(self.instance,self.managed.sessions)
                self.assertEqual([],self.source.query('SELECT * FROM reservations'))
            elif change or timeout or unfinished or loss or after:
                self.assertIsInstance(error,RegistryError)
                self.assertEqual('STOP_TIMEOUT' if timeout else 'CONTROL_UNKNOWN' if unfinished or loss or after else 'PROCESS_MISMATCH',error.code)
                self.assertNotEqual('COMPLETED',parent['status'])
                self.assertEqual(before,self.source.query('SELECT * FROM reservations ORDER BY operation,reference'))
                self.assertEqual(1,self.f.state()['pending'])
                self.assertIs(self.managed.sessions[self.instance],self.session)
                if unfinished or loss or after:
                    self.assertEqual(run_before,self.source.query('SELECT record FROM runs WHERE id=?',(self.session['run'],))[0]['record'])
                    if unfinished:self.assertEqual([],self.peer.adapter.processes)
                    else:
                        self.assertEqual(1,self.pipes[0].effects)
                        with self.assertRaises(RegistryError):self.observe()
                        self.assertEqual(1,self.pipes[0].effects)
                else:
                    self.assertEqual(1,self.pipes[0].effects)
                    with self.assertRaises(RegistryError):self.observe()
                    self.assertEqual(1,len(self.peer.adapter.processes))
            else:
                self.assertIsNone(error)
                self.assertEqual('COMPLETED',parent['status'])
                saved=json.loads(parent['response'])
                self.assertEqual(self.candidate,saved['gameExit']['candidate'])
                self.assertEqual(target.digest(self.session['game_exit']['result']),saved['gameExit']['observationSha256'])
                self.assertNotIn('ready',saved['gameExit']);self.assertNotIn('gameOwner',saved['gameExit'])
                if result is not None:self.assertEqual(saved,result)
                self.assertEqual('LEASED_STOPPED',self.f.state()['state']);self.assertEqual(0,self.f.state()['pending'])
                self.assertEqual([],self.source.query('SELECT * FROM reservations'))
                self.assertNotIn(self.instance,self.managed.sessions)
            self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
            released = not direct and error is None
            self.assertEqual(released, self.m.closed)
            if not unfinished:
                self.assertEqual(int(released), self.c.process.close.call_count)
            self.assertEqual(1,len(self.f.adapter.created));self.assertEqual(0,self.f.rcon_calls)

    def test_public_stop_commits_bound_game_exit_and_blocks_start(self):self.stop_case(overlap=True)
    def test_direct_stop_commits_bound_game_exit_and_blocks_start(self):self.stop_case(direct=True,overlap=True)
    def test_public_final_binding_change_preserves_reservation(self):self.stop_case(change=True)
    def test_direct_final_binding_change_preserves_reservation(self):self.stop_case(direct=True,change=True)
    def test_public_final_deadline_preserves_reservation(self):self.stop_case(timeout=True)
    def test_direct_final_deadline_preserves_reservation(self):self.stop_case(direct=True,timeout=True)
    def test_public_unfinished_observer_has_no_stop_record_effect(self):self.stop_case(unfinished=True)
    def test_direct_unfinished_observer_has_no_stop_record_effect(self):self.stop_case(direct=True,unfinished=True)

    def test_public_commit_crosses_deadline_keeps_result_and_releases_session(self):self.stop_case(committed_timeout=True)
    def test_direct_commit_crosses_deadline_keeps_result_and_releases_session(self):self.stop_case(direct=True,committed_timeout=True)
    def test_public_response_loss_has_no_additional_stop_effect(self):self.stop_case(loss=True)
    def test_direct_response_loss_has_no_additional_stop_effect(self):self.stop_case(direct=True,loss=True)
    def test_public_data_failure_has_no_additional_stop_effect(self):self.stop_case(after=True)
    def test_direct_data_failure_has_no_additional_stop_effect(self):self.stop_case(direct=True,after=True)

    def retained_case(self, *, direct=False, timeout=False):
        captured={}
        original_release=self.managed.release
        def release(instance,run_id):
            active=self.managed.sessions[instance]
            slot=active['game_exit']
            captured.update(key=(run_id,active['stop_operation']),observer=weakref.ref(slot['observer']),
                process=weakref.ref(slot['observer_process']),snapshot=slot['snapshot'],observer_snapshot=slot['observer_snapshot'],
                originals={'descriptor':active['raw'],'startup_request':self.managed.startup.sessions[instance]['request'],
                    'stop_request':active['stop_request'],'save_request':active['save_request'],
                    'completion_response':active['completion_response']},
                binding={'instanceId':instance,'runId':run_id,'stopOperationId':active['stop_operation'],
                    'stopFingerprint':active['stop_fingerprint'],'controlOperationId':active['control_operation']})
            return original_release(instance,run_id)
        with patch.object(self.managed,'release',side_effect=release):
            self.stop_case(direct=direct,committed_timeout=timeout)
        self.session=None
        self.base.session=None
        self.peer.adapter.processes.clear()
        self.pipes.clear()
        for name in ('_stop','_direct_stop'):
            method=getattr(self.runtime,name)
            if isinstance(method,Mock):method.reset_mock()
        gc.collect()
        self.assertIsNone(self.session);self.assertIsNone(self.base.session)
        self.assertEqual([],self.peer.adapter.processes)
        self.assertIn(captured['key'],self.runtime.game_exits)
        slot,originals,binding=self.runtime._game_exit(*captured['key'])
        self.assertIsNotNone(captured['observer']());self.assertIsNotNone(captured['process']())
        self.assertIs(captured['observer'](),slot['observer'])
        self.assertIs(captured['process'](),slot['observer_process'])
        self.assertIs(slot['observer'].process,slot['observer_process'])
        self.assertEqual(captured['snapshot'],slot['snapshot'])
        self.assertEqual(captured['observer_snapshot'],slot['observer_snapshot'])
        self.assertEqual(captured['originals'],originals);self.assertEqual(captured['binding'],binding)
        originals.clear();binding.clear()
        self.assertEqual(captured['originals'],self.runtime._game_exit(*captured['key'])[1])
        self.assertEqual(captured['binding'],self.runtime._game_exit(*captured['key'])[2])
        self.assertNotIn(self.instance,self.managed.sessions)
        self.assertNotIn(self.instance,self.managed.startup.sessions)
        released = not direct and not timeout
        self.assertEqual(int(released), slot['observer_process'].close.call_count)
        self.assertEqual(released, self.m.closed)
        for key in ((captured['key'][0],'other-stop'),('other-run',captured['key'][1])):
            with self.assertRaises(RegistryError):self.runtime._game_exit(*key)

    def test_public_retains_c_after_caller_drops_session(self):self.retained_case()
    def test_direct_retains_c_after_caller_drops_session(self):self.retained_case(direct=True)
    def test_public_commit_timeout_retains_c_after_caller_drops_session(self):self.retained_case(timeout=True)
    def test_direct_commit_timeout_retains_c_after_caller_drops_session(self):self.retained_case(direct=True,timeout=True)

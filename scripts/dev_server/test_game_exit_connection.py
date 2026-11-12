"""同stopの原本と元Mを実OfflineProcessへ渡す。OS/pipe/固定媒体だけ合成する。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from . import mod_control_runtime as target
from . import offline_process
from . import test_game_exit_inputs as inputs
from . import test_offline_process as processes
from . import test_offline_game_exit_process as exits
from . import runtime_control
from .registry import RegistryError


class Pipe(exits.GamePipe):
    def send(self, request, deadline, verify):
        verify()
        if self.current.failure == 'before':
            self.current.c.process.code = 1
            raise RegistryError('PROOF_MISMATCH','fixture Data作用前拒否')
        assert request['gameOwner'] == self.current.session['context']['owner']
        assert request['controller'] == self.current.session['context']['management']['controller']
        super().send(request,deadline,verify)
        if self.current.failure == 'after':
            self.current.c.process.code = 1
            raise OSError('fixture Data Ready作用後失敗')

    def receive(self, deadline, verify):
        verify()
        if self.current.failure == 'lost':
            self.current.c.process.code = 1
            raise EOFError('fixture Ready作用後の応答喪失')
        return super().receive(deadline,verify)


def configure(test, runtime, managed, session, candidate, peer):
    test.runtime,test.managed,test.session,test.candidate=runtime,managed,session,deepcopy(candidate)
    test.peer=peer;test.pipes=peer.pipes
    test.failure=test.response_change=None;test.observer_code=0
    test.after_send=lambda:None;test.after_receive=lambda:None
    test.data_phase='SavedPendingExit'
    test.ready=dict(worldUuid=candidate['worldUuid'],runUuid=candidate['runUuid'],
                    stateSha256='d'*64,stateRevision=candidate['stateRevision']+1)
    test.clock=0;test.deadline=10;session['stop_deadline']=10
    test.enterContext(patch.object(target.time,'monotonic',lambda:test.clock))
    test.enterContext(patch.object(target.time,'sleep',side_effect=AssertionError('実sleep禁止')))
    parent=session['context']['management']['controller']
    peer.native.host,peer.native.boot=parent['hostHash'],parent['bootId']
    peer.adapter.parent.update(pid=parent['pid'],startId=parent['startId'],bootId=parent['bootId'])
    original_adapter=runtime.process_adapter
    m,born=runtime.game_processes[session['run']]
    test.m=m;test.born=born
    old_identity=peer.adapter.identity
    def identity(pid):
        if pid==parent['pid']:return deepcopy(peer.adapter.parent)
        if pid==m.pid:raise AssertionError('終了した元Mの旧PID再照会禁止')
        return old_identity(pid)
    peer.adapter.identity=identity
    old_create=peer.adapter.OwnedProcess
    def create():
        p=old_create()
        p.members=lambda:([p.pid] if p.code is None else [])+p.extra
        p.close=Mock()
        return p
    peer.adapter.OwnedProcess=create
    runtime.process_adapter=peer.adapter
    managed.transport.native=peer.native
    test.enterContext(patch.object(offline_process,'os',SimpleNamespace(getpid=lambda:parent['pid'],environ=processes.os.environ)))
    test.enterContext(patch.object(offline_process,'write_json',side_effect=lambda path,value:path.write_bytes(target.canonical(value))))
    Pipe.current=test
    test.enterContext(patch.object(offline_process,'ProxyPipe',Pipe))
    artifact=json.loads(born)['run']['artifactId']
    test.bundle_check=test.enterContext(patch.object(target,'verify_bundle',return_value={'artifactId':artifact}))
    test.data_check=test.enterContext(patch.object(target,'require_data_generation'))
    test.original_adapter=original_adapter


class ObserverTests(unittest.TestCase):
    def setUp(self):
        peer=processes.OfflineProcessTests();peer.setUp();self.addCleanup(peer.doCleanups)
        self.case=inputs.GameExitInputsTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        f=self.case.f
        self.enterContext(patch.object(f.runtime,'_active_generation',return_value=peer.root))
        self.enterContext(patch.object(f.runtime,'_paths',return_value=peer.root/'instance'))
        self.case.process.poll.return_value=0;self.case.process.members.return_value=[]
        configure(self,f.runtime,self.case.managed,self.case.session,self.case.candidate,peer)

    @property
    def c(self):return self.session['game_exit']['observer']

    def observe(self):
        return self.managed.observe_game_exit('slot-a',self.session,self.candidate,self.deadline)

    def effects(self):
        return sum(p.starts for p in self.peer.adapter.processes),len(self.pipes),sum(len(p.sent) for p in self.pipes),sum(p.effects for p in self.pipes)

    def no_retry(self):
        before=self.effects()
        with self.assertRaises(RegistryError) as caught:self.observe()
        self.assertEqual('CONTROL_PENDING',caught.exception.code)
        self.assertEqual(before,self.effects())

    def test_fixed_originals_reach_single_c_without_registry_promotion_or_release(self):
        expected=self.managed.game_exit_inputs('slot-a',self.session,self.candidate,self.deadline)
        before=self.case.base.parent(),self.case.base.unchanged_effects()
        result=self.observe()
        request=self.pipes[0].sent[0]
        self.assertEqual(expected[0],{k:request[k] for k in expected[0]})
        self.assertEqual(expected,self.session['game_exit']['inputs'])
        self.assertEqual(self.session['stop_operation'],request['operationId'])
        self.assertEqual(target.digest(target.canonical({'command':'observe-game-exit','stopFingerprint':self.session['stop_fingerprint'],'inputs':expected[0]})),request['requestHash'])
        self.assertEqual('observe-game-exit',request['command'])
        self.assertIs(self.m,self.c.game_exit_process)
        self.assertEqual(json.loads(self.born)['run']['identity'],self.c.game_exit_identity)
        self.assertEqual((1,1,1,1),self.effects())
        self.assertEqual('Ready',self.data_phase)
        self.assertEqual('GAME_EXIT_OBSERVED',result['status'])
        self.assertEqual(target.canonical(result),self.session['game_exit']['result'])
        self.assertEqual(before,(self.case.base.parent(),self.case.base.unchanged_effects()))
        self.assertFalse(self.c.ready)
        self.m.close.assert_not_called();self.c.process.close.assert_not_called()
        result['ready']['stateRevision']+=100
        self.assertEqual(self.ready,json.loads(self.session['game_exit']['result'])['ready'])
        self.no_retry()

    def test_recorded_reference_mismatch_rejects_before_c_generation(self):
        self.session['save_evidence']['responseBodySha256']='0'*64
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual((0,0,0,0),self.effects());self.no_retry()

    def test_wrong_session_rejects_before_io_or_generation(self):
        self.managed.sessions['slot-a']=dict(self.session)
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual((0,0,0,0),self.effects())

    def test_wrong_artifact_rejects_before_generation(self):
        self.bundle_check.return_value={'artifactId':'0'*64}
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual((0,0,0,0),self.effects());self.no_retry()

    def test_missing_data_generation_rejects_before_generation(self):
        self.data_check.side_effect=RegistryError('ARTIFACT_MISMATCH','fixture Data世代')
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual((0,0,0,0),self.effects());self.no_retry()

    def test_m_nonzero_has_no_c_or_data_effect(self):
        self.m.poll.return_value=1
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual((0,0,0,0),self.effects());self.no_retry()

    def test_m_child_timeout_keeps_original_handle_without_c(self):
        self.m.members.return_value=[99999]
        with patch.object(target.time,'sleep',side_effect=lambda _:setattr(self,'clock',10)):
            with self.assertRaises(RegistryError) as caught:self.observe()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual((0,0,0,0),self.effects());self.no_retry()

    def test_failure_after_publication_is_not_retried(self):
        self.failure='after'
        with self.assertRaises(OSError):self.observe()
        self.assertEqual((1,1,1,1),self.effects())
        self.assertEqual('Ready',self.data_phase);self.assertEqual(1,self.c.process.code)
        self.assertNotIn('result',self.session['game_exit']);self.no_retry()

    def test_response_loss_preserves_ready_and_nonzero_peer(self):
        self.failure='lost'
        with self.assertRaises(EOFError):self.observe()
        self.assertEqual((1,1,1,1),self.effects())
        self.assertEqual('Ready',self.data_phase);self.assertEqual(1,self.c.process.code)
        self.assertNotIn('result',self.session['game_exit']);self.no_retry()

    def test_data_pre_effect_rejection_has_zero_effect_and_nonzero_peer(self):
        self.failure='before'
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(0,self.effects()[-1]);self.assertEqual(1,self.c.process.code)
        self.assertEqual('SavedPendingExit',self.data_phase);self.no_retry()

    def test_final_callback_deadline_crossing_keeps_local_c_evidence(self):
        original=self.managed.game_exit_inputs
        def checked(*args):
            value=original(*args)
            if 'game_exit' in self.session and 'observer' in self.session['game_exit'] and self.c.game_exit_result is not None:self.clock=10
            return value
        with patch.object(self.managed,'game_exit_inputs',side_effect=checked):
            with self.assertRaises(RegistryError) as caught:self.observe()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertIsNotNone(self.c.game_exit_result)
        self.assertNotIn('result',self.session['game_exit']);self.no_retry()


    def test_original_proof_bytes_change_after_effect_is_not_promoted(self):
        self.after_send=lambda:self.case.files.update({self.case.proof_path:self.case.proof_raw+b' '})
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual('Ready',self.data_phase)
        self.assertEqual((1,1,1,1),self.effects())
        self.assertNotIn('result',self.session['game_exit']);self.no_retry()

    def result_deadline(self, crossing):
        original=target.canonical;seen=[]
        def encoded(value):
            raw=original(value)
            if isinstance(value,dict) and value.get('status')=='GAME_EXIT_OBSERVED' and 'game_exit' in self.session and 'observer' in self.session['game_exit'] and self.c.game_exit_result is not None:
                seen.append(1)
                if len(seen)==crossing:self.clock=10
            return raw
        with patch.object(target,'canonical',side_effect=encoded):
            with self.assertRaises(RegistryError) as caught:self.observe()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual((1,1,1,1),self.effects())
        self.assertEqual(crossing==2,'result' in self.session['game_exit'])
        self.no_retry()

    def test_deadline_before_local_result_has_no_result(self):
        self.result_deadline(1)

    def test_deadline_after_local_result_preserves_it_without_returning_success(self):
        self.result_deadline(2)


    def test_retry_after_pre_effect_failure_does_not_rebind_original(self):
        original=dict(self.session['save_evidence'])
        self.session['save_evidence']['responseBodySha256']='0'*64
        with self.assertRaises(RegistryError):self.observe()
        self.session['save_evidence']=original
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual((0,0,0,0),self.effects())

    def test_original_m_object_replacement_after_effect_is_rejected(self):
        def change():
            replacement=Mock(pid=self.m.pid,group=self.m.group)
            self.runtime.game_processes[self.session['run']]=(replacement,self.born)
        self.after_send=change
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual((1,1,1,1),self.effects())
        self.assertNotIn('result',self.session['game_exit']);self.no_retry()

    def test_local_observer_evidence_mismatch_is_not_promoted(self):
        original=offline_process.OfflineProcess.observe_game_exit
        def changed(observer,*args,**kwargs):
            value=original(observer,*args,**kwargs)
            observer.game_exit_result=b'{}'
            return value
        with patch.object(offline_process.OfflineProcess,'observe_game_exit',changed):
            with self.assertRaises(RegistryError):self.observe()
        self.assertNotIn('result',self.session['game_exit']);self.no_retry()


def changed_stop(field):
    def test(self):
        def change(*args):
            self.session[field]+='-other'
            return {'artifactId':json.loads(self.born)['run']['artifactId']}
        self.bundle_check.side_effect=change
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual((0,0,0,0),self.effects());self.no_retry()
    return test


for field in ('stop_operation','stop_fingerprint','control_operation'):
    setattr(ObserverTests,'test_original_'+field+'_change_before_launch',changed_stop(field))


def changed_slot(field):
    def test(self):
        def change(*args):
            self.session['game_exit'][field]=None
            return {'artifactId':json.loads(self.born)['run']['artifactId']}
        self.bundle_check.side_effect=change
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual((0,0,0,0),self.effects());self.no_retry()
    return test


for field in ('snapshot','inputs','process','stop','candidate'):
    setattr(ObserverTests,'test_held_slot_'+field+'_change_before_launch',changed_slot(field))


class ObserverRegistryTests(unittest.TestCase):
    def setUp(self):
        self.case=inputs.GameExitInputsRegistryTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        peer=processes.OfflineProcessTests();peer.setUp();self.addCleanup(peer.doCleanups)
        configure(self,self.case.runtime,self.case.base.managed,self.case.session,self.case.candidate,peer)

    @property
    def c(self):return self.session['game_exit']['observer']

    def observe(self):return self.managed.observe_game_exit(self.case.case.instance,self.session,self.candidate,self.deadline)

    def test_competing_observer_and_start_keep_original_transaction_and_pending(self):
        entered,release=threading.Event(),threading.Event()
        def held():
            entered.set()
            if not release.wait(10):raise TimeoutError('試験の観測C応答')
        self.after_send=held
        before=self.case.case.query('SELECT * FROM reservations ORDER BY operation,reference')
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner=pool.submit(self.observe)
            try:
                self.assertTrue(entered.wait(10))
                with self.assertRaises(RegistryError):self.observe()
                with self.assertRaises(RegistryError):self.case.f.start('game-exit-start')
                self.assertEqual(1,len(self.peer.adapter.processes))
            finally:release.set()
            self.assertEqual('GAME_EXIT_OBSERVED',winner.result(10)['status'])
        self.assertEqual(before,self.case.case.query('SELECT * FROM reservations ORDER BY operation,reference'))
        self.assertEqual('STOPPING',self.case.f.state()['state']);self.assertEqual(1,self.case.f.state()['pending'])
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        self.assertFalse(self.m.closed);self.c.process.close.assert_not_called()

    def test_registry_reference_change_after_data_effect_preserves_unknown_and_sources(self):
        def change():
            with self.runtime._transaction(self.case.case.instance) as connection:
                connection.execute('DELETE FROM reservations WHERE reference=?',('stop:'+self.session['stop_operation'],))
        self.after_send=change
        original=self.session['save_request'],self.session['completion_response']
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual('Ready',self.data_phase)
        self.assertNotIn('result',self.session['game_exit'])
        self.assertEqual(original,(self.session['save_request'],self.session['completion_response']))
        self.assertEqual(1,len(self.peer.adapter.processes));self.assertEqual(1,self.pipes[0].effects)
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(1,self.pipes[0].effects)
        self.assertEqual('STOPPING',self.case.f.state()['state']);self.assertEqual(1,self.case.f.state()['pending'])

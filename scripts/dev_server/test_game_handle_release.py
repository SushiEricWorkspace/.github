"""確定stopに束縛した元M/Cの単発解放と、解放済みhandleを使わない次OPENを検証する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import errno
import importlib.util
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch

from . import database_proxy_pipe, offline_control, runtime_control, runtime_process, supervisor
from . import test_game_ready_open as ready
from . import test_selected_game_exit as selected
from .build_inputs import canonical
from .build_lock import BuildLockError
from .registry import RegistryError


class HandleReleaseTests(unittest.TestCase):
    def setUp(self):
        self.base=ready.GameReadyTests();self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.configure(self.base)

    def configure(self, base):
        self.base=base
        self.runtime,self.f,self.slot=base.runtime,base.f,base.slot
        self.token,self.stop_id=deepcopy(base.token),base.stop_id
        self.operation='release-game-handles'
        self.m,self.c=base.case.m,self.slot['observer_process']
        self.order=[]
        pipe=database_proxy_pipe.ProxyPipe()
        pipe.close_child_ends()
        self.slot['observer'].pipe=pipe
        runtime,run,stop=self.runtime,base.case.session['run'],self.stop_id
        def cleanup_pipe():
            runtime._game_exit(run,stop)[0]['observer'].pipe.close_after_exit()
        self.addCleanup(cleanup_pipe)
        original=self.m.close
        def close_m():
            original();self.order.append('m')
            self.m.poll=Mock(side_effect=AssertionError('解放済みMのpoll'))
            self.m.members=Mock(side_effect=AssertionError('解放済みMのmembers'))
        def close_c():
            self.order.append('c')
            self.c.poll=Mock(side_effect=AssertionError('解放済みCのpoll'))
            self.c.members=Mock(side_effect=AssertionError('解放済みCのmembers'))
        self.m.close=Mock(side_effect=close_m)
        self.c.close=Mock(side_effect=close_c)
        self.originals=deepcopy(base.originals)
        self.snapshot=base.snapshot()

    def release(self, **changes):
        values=dict(operation_id=self.operation,stop_operation_id=self.stop_id,deadline=20)
        values.update(changes)
        return self.runtime._release_game_handles(self.token,**values)

    def counts(self):
        return self.m.close.call_count,self.c.close.call_count,self.base.effects()

    def preserved(self):
        held,originals,binding=self.runtime._game_exit(self.base.case.session['run'],self.stop_id)
        self.assertIs(self.slot,held)
        self.assertIs(self.m,held['process']);self.assertIs(self.c,held['observer_process'])
        self.assertEqual(self.originals,originals)
        self.assertEqual(self.base.binding,binding)
        self.assertEqual('Ready',self.base.case.data_phase)

    def pending(self):
        operation=self.base.query('SELECT * FROM runtime_operations WHERE id=?',(self.operation,))[0]
        self.assertEqual('PENDING',operation['status'])
        self.assertEqual(1,self.f.state()['pending'])
        self.assertEqual(1,len(self.base.query('SELECT * FROM reservations WHERE operation=?',(self.operation,))))
        self.preserved()

    def no_retry(self):
        before=self.counts(),self.base.snapshot()
        for operation in (self.operation,'another-release'):
            with self.assertRaises(RegistryError):self.release(operation_id=operation)
        self.assertEqual(before,(self.counts(),self.base.snapshot()))

    def rejected(self, change, **values):
        change();before=self.base.snapshot()
        with self.assertRaises(RegistryError):self.release(**values)
        self.assertEqual((0,0,(0,0,0)),self.counts())
        self.assertEqual(before,self.base.snapshot())

    def test_release_then_explicit_ready_uses_only_fixed_snapshots(self):
        proof=self.f.state()['runtime_proof']
        result=self.release()
        self.assertTrue(result['handlesReleased'])
        self.assertEqual(['m','c'],self.order)
        self.assertEqual((1,1,(0,0,0)),self.counts())
        self.assertEqual('RELEASED',self.slot['handle_release']['m'])
        self.assertEqual('RELEASED',self.slot['handle_release']['c'])
        self.assertTrue(self.slot['handle_release']['completed'])
        self.assertEqual('COMPLETED',self.base.query('SELECT status FROM runtime_operations WHERE id=?',(self.operation,))[0]['status'])
        self.assertEqual([],self.base.query('SELECT * FROM reservations'))
        self.assertEqual(0,self.f.state()['pending'])
        self.assertEqual(proof,self.f.state()['runtime_proof'])
        self.preserved();self.no_retry()
        response=self.base.open()
        self.assertEqual('OPEN',response['status'])
        self.assertEqual('OfflineIdle',self.base.case.data_phase)
        self.assertEqual((1,1,(1,1,1)),self.counts())

    def test_wrong_stop_has_effect_zero(self):self.rejected(lambda:None,stop_operation_id='old-stop')
    def test_expired_deadline_has_effect_zero(self):self.rejected(lambda:setattr(self.base.case,'clock',20))
    def test_old_token_has_effect_zero(self):self.rejected(lambda:self.token.update(leaseId='other'))
    def test_unfinished_stop_has_effect_zero(self):
        def change():
            with self.runtime._transaction(self.token['instanceId']) as db:
                db.execute("UPDATE runtime_operations SET status='PENDING' WHERE id=?",(self.stop_id,))
        self.rejected(change)
    def test_m_object_mismatch_has_effect_zero(self):
        self.rejected(lambda:self.slot.update(process=Mock(pid=self.m.pid,group=self.m.group)))
    def test_m_snapshot_mismatch_has_effect_zero(self):self.rejected(lambda:self.slot.update(snapshot=self.slot['snapshot']+b' '))
    def test_c_object_mismatch_has_effect_zero(self):
        self.rejected(lambda:self.slot.update(observer_process=Mock(pid=self.c.pid,group=self.c.group)))
    def test_c_record_mismatch_has_effect_zero(self):self.rejected(lambda:self.slot['observer'].record.update(commandHash='f'*64))
    def test_local_result_without_proof_has_effect_zero(self):
        self.rejected(lambda:self.base.replace_proof(lambda value:value.pop('gameExit')))
    def test_prior_attempt_has_effect_zero(self):self.rejected(lambda:self.slot.update(handle_release={'m':'UNKNOWN'}))

    def close_failure(self, role, after=False):
        process=self.m if role=='m' else self.c
        original=process.close.side_effect
        def failed():
            if after:original()
            raise OSError('fixture部分解放不明')
        process.close.side_effect=failed
        with self.assertRaises(OSError):self.release()
        self.assertEqual((1,0,(0,0,0)) if role=='m' else (1,1,(0,0,0)),self.counts())
        self.assertEqual('UNKNOWN',self.slot['handle_release'][role])
        self.assertEqual('NOT_STARTED' if role=='m' else 'RELEASED',self.slot['handle_release']['c' if role=='m' else 'm'])
        self.pending();self.no_retry()

    def test_m_close_failure_preserves_unknown(self):self.close_failure('m')
    def test_c_close_failure_preserves_released_m(self):self.close_failure('c')
    def test_m_partial_close_failure_has_no_retry(self):self.close_failure('m',True)
    def test_c_partial_close_failure_has_no_retry(self):self.close_failure('c',True)

    def close_deadline(self, role):
        process=self.m if role=='m' else self.c
        original=process.close.side_effect
        def late():original();self.base.case.clock=21
        process.close.side_effect=late
        with self.assertRaises(RegistryError) as caught:self.release()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual((1,0,(0,0,0)) if role=='m' else (1,1,(0,0,0)),self.counts())
        self.assertEqual('RELEASED' if role=='m' else 'UNKNOWN',self.slot['handle_release'][role])
        self.pending();self.no_retry()
    def test_m_close_crosses_deadline(self):self.close_deadline('m')
    def test_c_close_crosses_deadline(self):self.close_deadline('c')

    def before_close_deadline(self, role):
        original=self.f.registry.transaction
        @contextmanager
        def transaction(*args,**kwargs):
            with original(*args,**kwargs) as db:
                trigger=kwargs.get('read_only') and 'handle_release' in self.slot and (role=='m' or self.m.close.called)
                yield db
            if trigger:self.base.case.clock=21
        with patch.object(self.f.registry,'transaction',side_effect=transaction):
            with self.assertRaises(RegistryError) as caught:self.release()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual((0,0,(0,0,0)) if role=='m' else (1,0,(0,0,0)),self.counts())
        self.pending();self.no_retry()
    def test_deadline_after_m_checks_prevents_close(self):self.before_close_deadline('m')
    def test_deadline_after_c_checks_prevents_close(self):self.before_close_deadline('c')

    def test_final_serialization_deadline_keeps_pending(self):
        original=json.dumps
        def dumps(value,*args,**kwargs):
            result=original(value,*args,**kwargs)
            if isinstance(value,dict) and value.get('handlesReleased') is True:self.base.case.clock=21
            return result
        with patch.object(offline_control.json,'dumps',side_effect=dumps):
            with self.assertRaises(RegistryError) as caught:self.release()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual((1,1,(0,0,0)),self.counts())
        self.pending();self.no_retry()

    def test_final_binding_change_keeps_closed_objects_and_reservation(self):
        pipe=self.slot['observer'].pipe;original=pipe.close_after_exit
        def changed(*args,**kwargs):
            original(*args,**kwargs);self.slot['observer'].record['commandHash']='f'*64
        self.enterContext(patch.object(pipe,'close_after_exit',side_effect=changed))
        with self.assertRaises(RegistryError):self.release()
        self.assertEqual((1,1,(0,0,0)),self.counts())
        self.pending();self.no_retry()

    def test_commit_deadline_keeps_completed_without_reclosing(self):
        original=self.f.registry.transaction
        @contextmanager
        def transaction(*args,**kwargs):
            with original(*args,**kwargs) as db:
                db.set_trace_callback(lambda sql:setattr(self.base.case,'clock',21)
                    if sql.startswith("UPDATE runtime_operations SET status='COMPLETED',response=") and self.operation in sql else None)
                yield db
        with patch.object(self.f.registry,'transaction',side_effect=transaction):
            with self.assertRaises(RegistryError) as caught:self.release()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual('COMPLETED',self.base.query('SELECT status FROM runtime_operations WHERE id=?',(self.operation,))[0]['status'])
        self.assertEqual(0,self.f.state()['pending'])
        self.assertEqual([],self.base.query('SELECT * FROM reservations'))
        self.assertTrue(self.slot['handle_release']['completed'])
        self.preserved();self.no_retry()
        response=self.runtime._offline_open(self.token,operation_id='open-after-release-timeout',revision=self.base.revision,
            ready=self.base.ready,deadline=40)
        self.assertEqual('OPEN',response['status'])
        self.assertEqual((1,1,(1,1,1)),self.counts())


    def test_commit_response_loss_has_no_additional_close_or_retry(self):
        original=self.f.registry.transaction
        @contextmanager
        def transaction(*args,**kwargs):
            with original(*args,**kwargs) as db:
                yield db
                done=db.execute('SELECT status FROM runtime_operations WHERE id=?',(self.operation,)).fetchone()
                lose=not kwargs.get('read_only') and done is not None and done[0]=='COMPLETED'
            if lose:raise EOFError('fixture確定後の応答喪失')
        with patch.object(self.f.registry,'transaction',side_effect=transaction):
            with self.assertRaises(EOFError):self.release()
        self.assertEqual((1,1,(0,0,0)),self.counts())
        self.assertEqual('COMPLETED',self.base.query('SELECT status FROM runtime_operations WHERE id=?',(self.operation,))[0]['status'])
        self.assertEqual([],self.base.query('SELECT * FROM reservations'))
        self.assertEqual(0,self.f.state()['pending'])
        self.preserved();self.no_retry()

    def test_progress_replacement_after_m_close_keeps_original_progress(self):
        original=self.m.close.side_effect
        captured=[]
        def changed():
            captured.append(self.slot['handle_release']);original()
            self.slot['handle_release']=dict(captured[0])
        self.m.close.side_effect=changed
        with self.assertRaises(RegistryError):self.release()
        self.assertEqual((1,0,(0,0,0)),self.counts())
        self.assertEqual('RELEASED',captured[0]['m'])
        self.pending();self.no_retry()


    def test_reservation_rechecks_preflight_revision_before_any_close(self):
        original=self.runtime._game_process
        changed=[]
        def process(run):
            value=original(run)
            if not changed:
                changed.append(True)
                with self.runtime._transaction(self.token['instanceId']) as db:
                    db.execute('UPDATE run_admission SET revision=revision+1 WHERE run=?',(run['runId'],))
            return value
        with patch.object(self.runtime,'_game_process',side_effect=process):self.rejected(lambda:None)

    def test_deadline_after_reservation_queries_has_effect_zero(self):
        original=self.f.registry._instance
        def row(db,instance):
            value=original(db,instance)
            if not db.execute('PRAGMA query_only').fetchone()[0]:
                db.set_trace_callback(lambda sql:setattr(self.base.case,'clock',21)
                    if sql.startswith('SELECT 1 FROM operations WHERE id=') and self.operation in sql else None)
            return value
        with patch.object(self.f.registry,'_instance',side_effect=row):self.rejected(lambda:None)

    def test_two_preflights_cannot_reclose_after_first_completes(self):
        barrier=threading.Barrier(2);completed=threading.Event();local=threading.local()
        original=offline_control.digest;original_process=self.runtime._game_process
        def process(run):
            value=original_process(run);local.prepared=True
            return value
        def digest(raw):
            value=original(raw)
            if getattr(local,'prepared',False) and raw==self.slot['observer_snapshot'] and not getattr(local,'rendezvous',False):
                local.rendezvous=True;barrier.wait(10)
                if local.role=='second':self.assertTrue(completed.wait(10))
            return value
        def call(role):
            local.role=role
            try:return self.release(operation_id=self.operation if role=='first' else 'second-release')
            finally:
                if role=='first':completed.set()
        with patch.object(self.runtime,'_game_process',side_effect=process),patch.object(offline_control,'digest',side_effect=digest),patch.object(runtime_control.time,'sleep',lambda _:None),ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(call,'first');second=pool.submit(call,'second')
            self.assertTrue(first.result(10)['handlesReleased'])
            with self.assertRaises(RegistryError):second.result(10)
        self.assertEqual((1,1,(0,0,0)),self.counts())
        self.assertEqual([],self.base.query('SELECT * FROM runtime_operations WHERE id=?',('second-release',)))
        self.assertEqual([],self.base.query('SELECT * FROM reservations'))
        self.assertEqual(0,self.f.state()['pending'])
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        self.preserved();self.no_retry()


    def pipe_state(self):
        return self.runtime._game_exit(self.base.case.session['run'],self.stop_id)[0]['observer'].pipe

    def test_parent_pipe_fds_are_closed_in_retained_product_state(self):
        fds=(self.pipe_state().input,self.pipe_state().output)
        self.release()
        self.slot=self.base.slot=None
        self.base.case.session.pop('game_exit')
        pipe=self.pipe_state()
        self.assertIsNone(pipe.input);self.assertIsNone(pipe.output)
        self.assertIsNone(pipe.child_input);self.assertIsNone(pipe.child_output)
        for fd in fds:
            with self.assertRaises(OSError) as caught:os.fstat(fd)
            self.assertEqual(errno.EBADF,caught.exception.errno)
        held=self.runtime._game_exit(self.base.case.session['run'],self.stop_id)[0]
        self.assertEqual('RELEASED',held['handle_release']['c'])
        self.assertEqual('OPEN',self.base.open()['status'])

    def pipe_failure(self, field):
        pipe=self.pipe_state();failing=getattr(pipe,field);original=os.close
        calls=[]
        def close(fd):
            if fd in (pipe.input,pipe.output):calls.append(fd)
            if fd==failing:raise OSError('fixture片端close失敗')
            return original(fd)
        with patch.object(database_proxy_pipe.os,'close',side_effect=close):
            with self.assertRaises(OSError):self.release()
            before=list(calls);self.no_retry();self.assertEqual(before,calls)
        self.assertEqual((1,1,(0,0,0)),self.counts())
        self.assertEqual('RELEASED',self.slot['handle_release']['m'])
        self.assertEqual('UNKNOWN',self.slot['handle_release']['c'])
        self.assertEqual(failing,getattr(self.pipe_state(),field))
        os.fstat(failing)
        if field=='output':self.assertIsNone(self.pipe_state().input)
        self.pending()
    def test_input_close_failure_preserves_pipe_and_unknown(self):self.pipe_failure('input')
    def test_output_close_failure_preserves_remaining_fd(self):self.pipe_failure('output')

    def test_deadline_after_first_fd_keeps_second_without_reclose(self):
        pipe=self.pipe_state();first,second=pipe.input,pipe.output;original=os.close
        calls=[]
        def close(fd):
            original(fd)
            if fd in (first,second):calls.append(fd)
            if fd==first:self.base.case.clock=21
        with patch.object(database_proxy_pipe.os,'close',side_effect=close):
            with self.assertRaises(RegistryError) as caught:self.release()
            self.assertEqual('STOP_TIMEOUT',caught.exception.code)
            self.assertEqual([first],calls);self.no_retry();self.assertEqual([first],calls)
        self.assertIsNone(self.pipe_state().input);self.assertEqual(second,self.pipe_state().output)
        self.assertEqual('UNKNOWN',self.slot['handle_release']['c'])
        self.pending()

    def test_pipe_check_deadline_prevents_first_fd_close(self):
        pipe=self.pipe_state();fds=(pipe.input,pipe.output);original=self.f.registry.transaction
        @contextmanager
        def transaction(*args,**kwargs):
            with original(*args,**kwargs) as db:
                yield db
            if kwargs.get('read_only') and self.c.close.called:self.base.case.clock=21
        with patch.object(self.f.registry,'transaction',side_effect=transaction):
            with self.assertRaises(RegistryError) as caught:self.release()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual(fds,(self.pipe_state().input,self.pipe_state().output))
        self.assertEqual('UNKNOWN',self.slot['handle_release']['c'])
        self.pending();self.no_retry()

    def test_deadline_after_final_fd_preserves_released_without_success(self):
        pipe=self.pipe_state();last=pipe.output;original=os.close
        def close(fd):
            original(fd)
            if fd==last:self.base.case.clock=21
        with patch.object(database_proxy_pipe.os,'close',side_effect=close):
            with self.assertRaises(RegistryError) as caught:self.release()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertIsNone(self.pipe_state().input);self.assertIsNone(self.pipe_state().output)
        self.assertEqual('RELEASED',self.slot['handle_release']['c'])
        self.pending();self.no_retry()


def identity_case(role,field,value):
    def test(self):
        def changed():
            if role=='m':
                if field=='pid':self.m.pid=value
                else:self.m.group=value
            else:self.slot['observer'].data_identity[field]=value
        self.rejected(changed)
    return test

for role,field,value in (
    ('m','pid',999),('m','group','other'),
    ('c','pid',999),('c','startId','other'),('c','hostHash','0'*64),('c','bootId','other')):
    setattr(HandleReleaseTests,'test_'+role+'_'+field+'_binding_rejects_before_close',identity_case(role,field,value))

def original_case(field,value):
    def test(self):
        def changed():
            originals=self.runtime.game_exits[(self.base.case.session['run'],self.stop_id)][1]
            originals[field]=value
        self.rejected(changed)
    return test

for field,value in (('descriptor',b'changed'),('save_request',b'0000changed'),('completion_response',b'0000changed')):
    setattr(HandleReleaseTests,'test_original_'+field+'_mismatch_has_effect_zero',original_case(field,value))


class SelectedReleaseIntegrationTests(unittest.TestCase):
    def test_selected_stop_release_then_new_open_retains_original_input(self):
        case=selected.SelectedGameExitTests();case.setUp();self.addCleanup(case.doCleanups)
        case.run_case()
        base=ready.GameReadyTests();self.addCleanup(base.doCleanups);base.configure(case.case)
        raw=case.session['game_exit_input']['raw']
        held, originals, _ = case.runtime._game_exit(case.session['run'], 'public-stop')
        self.assertTrue(held['handle_release']['completed'])
        self.assertEqual(raw, originals['game_exit_input'])
        case.case.clock=json.loads(raw)['deadline']+1
        result=base.open()
        self.assertEqual('OPEN',result['status'])
        self.assertEqual('OfflineIdle',case.case.data_phase)
        self.assertTrue(case.case.m.closed)
        held['observer_process'].close.assert_called_once()
        self.assertIs(case.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)


class ReleaseRaceTests(unittest.TestCase):
    def setUp(self):
        self.helper=HandleReleaseTests();self.helper.setUp();self.addCleanup(self.helper.doCleanups)

    def race(self,winner):
        from . import test_runtime_control as runtimes
        h=self.helper;r=h.runtime;f=h.f;instance=h.token['instanceId']
        r.process_adapter=f.adapter;r.managed=runtimes.FakeManaged(r)
        loser='start' if winner=='release' else 'release'
        barrier=threading.Barrier(2)
        holding,blocked,acted,finish=(threading.Event() for _ in range(4))
        local=threading.local()
        original_process=r._game_process;original_endpoints=runtime_control.instance_endpoints
        original_instance=f.registry._instance;original_lock=supervisor.private_lock
        original_m=h.m.close.side_effect
        initial_mods=len(f.adapter.created)
        def rendezvous(role):
            if getattr(local,'preflight',False):return
            local.preflight=True;barrier.wait(10)
            if role==loser:self.assertTrue(holding.wait(10))
        def process(run):
            value=original_process(run)
            if getattr(local,'role',None)=='release':rendezvous('release')
            return value
        def endpoints(declaration):
            value=original_endpoints(declaration)
            if getattr(local,'role',None)=='start':rendezvous('start')
            return value
        def row(connection,instance_id):
            value=original_instance(connection,instance_id)
            if getattr(local,'role',None)==winner and not getattr(local,'writing',False) and not connection.execute('PRAGMA query_only').fetchone()[0]:
                local.writing=True;holding.set();self.assertTrue(blocked.wait(10))
            return value
        @contextmanager
        def lock(path):
            entered=False
            try:
                with original_lock(path):entered=True;yield
            except BuildLockError as error:
                if not entered and getattr(local,'role',None)==loser and holding.is_set() and path.name=='instance-'+instance+'.lock' and isinstance(error.__cause__,OSError) and error.__cause__.errno in (errno.EACCES,errno.EAGAIN):blocked.set()
                raise
        def m_close():
            acted.set();self.assertTrue(finish.wait(10));original_m()
        def mod_start(process):
            acted.set();self.assertTrue(finish.wait(10))
        def call(role):
            local.role=role
            with r._lock_wait(30):
                if role=='release':return h.release(deadline=30)
                return r.execute('start',dict(f.token,timeoutSeconds=30),operation_id='start-race')
        h.m.close.side_effect=m_close;f.adapter.start_fault=mod_start
        with patch.object(r,'_game_process',side_effect=process),patch.object(runtime_control,'instance_endpoints',side_effect=endpoints),patch.object(f.registry,'_instance',side_effect=row),patch.object(supervisor,'private_lock',side_effect=lock),patch.object(runtime_control.time,'sleep',lambda _:None),ThreadPoolExecutor(max_workers=2) as pool:
            futures={role:pool.submit(call,role) for role in ('release','start')}
            try:
                self.assertTrue(acted.wait(10));self.assertTrue(blocked.is_set())
                with self.assertRaises(RegistryError) as caught:futures[loser].result(10)
                self.assertEqual('BUSY' if loser=='start' else 'WRITER_PENDING',caught.exception.code)
                with f.registry.transaction(read_only=True) as db:
                    current=f.registry._instance(db,instance)
                    self.assertEqual(1,current['pending'])
                    op=h.operation if winner=='release' else 'start-race'
                    self.assertEqual('PENDING',db.execute('SELECT status FROM runtime_operations WHERE id=?',(op,)).fetchone()[0])
                    self.assertEqual([op],[row[0] for row in db.execute('SELECT operation FROM reservations WHERE instance=?',(instance,))])
                self.assertEqual((1,0) if winner=='release' else (0,1),(h.m.close.call_count,len(f.adapter.created)-initial_mods))
            finally:finish.set();barrier.abort()
            result=futures[winner].result(10)
        self.assertTrue(result['handlesReleased'] if winner=='release' else result['running'])
        self.assertEqual((1,1) if winner=='release' else (0,0),(h.m.close.call_count,h.c.close.call_count))
        self.assertEqual(0 if winner=='release' else 1,len(f.adapter.created)-initial_mods)
        self.assertIs(r._transaction.__func__,runtime_control.RuntimeControl._transaction)
        h.no_retry()
    def test_release_first_keeps_reservation_and_blocks_start(self):self.race('release')
    def test_start_first_blocks_all_close_effects(self):self.race('start')


@unittest.skipUnless(os.name=='nt','Windowsのclose呼出しをfakeに置換する試験')
class WindowsCloseFaultTests(unittest.TestCase):
    def test_job_close_failure_is_not_reported_as_released(self):
        process=runtime_process.OwnedProcess();process.handle=11;process.job=22
        with patch.object(runtime_process._winapi,'CloseHandle') as process_close,patch.object(runtime_process.kernel,'CloseHandle',return_value=0),self.assertRaises(OSError):process.close()
        process_close.assert_called_once_with(11)


class MacCloseFaultTests(unittest.TestCase):
    def test_existing_wait_zero_contract_propagates_unknown(self):
        spec=importlib.util.spec_from_file_location('dev_server.mac_release_fault_test',runtime_process.__file__)
        adapter=importlib.util.module_from_spec(spec)
        with patch('os.name','posix'):spec.loader.exec_module(adapter)
        process=adapter.OwnedProcess();process.process=Mock()
        process.close();process.process.wait.assert_called_once_with(timeout=0)
        process.process.wait.side_effect=OSError('wait不明')
        with self.assertRaises(OSError):process.close()

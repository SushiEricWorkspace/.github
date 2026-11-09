"""P内部の明示候補入力と既存stopを実Registryで決定的に接続する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import threading
import time
import unittest
from unittest.mock import patch

from . import mod_control_runtime as target
from . import runtime_control
from . import test_game_exit_stop as stops
from . import test_runtime_control as legacy
from .registry import RegistryError


class SelectedGameExitTests(unittest.TestCase):
    def setUp(self):
        self.case=stops.StopTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.runtime,self.managed,self.f=self.case.runtime,self.case.managed,self.case.f
        self.session=self.case.base.start();self.case.session=self.session
        self.binding=dict(run_id=self.session['run'],operation_id='public-stop',
                          descriptor_sha256=target.wire.sha256(self.session['raw']))
        self.deadline=time.monotonic()+30

    def select(self, **changes):
        return self.managed.select_game_exit(self.f.token,deadline=self.deadline,**dict(self.binding,**changes))

    def supply(self, **changes):
        return self.managed.supply_game_exit(self.f.token,deadline=self.case.deadline,
                    **dict(dict(self.binding,candidate=deepcopy(self.case.candidate)),**changes))

    def effects(self):
        return len(self.case.peer.adapter.processes),len(self.case.peer.pipes),sum(p.effects for p in self.case.peer.pipes)

    def run_case(self, mode='ok', *, overlap=False):
        self.select()
        selected=self.session['game_exit_input']
        reserved,proceed=threading.Event(),threading.Event()
        waiting,allow_observer=threading.Event(),threading.Event()
        original=self.runtime._stop
        def held(*args):
            reserved.set()
            if not proceed.wait(10):raise TimeoutError('試験の予約境界')
            value=original(*args)
            if mode=='after_observe':selected['binding']=target.canonical(dict(json.loads(selected['binding']),runId='other'))
            if mode=='lost_input':self.session['game_exit'].pop('input')
            return value
        with patch.object(self.runtime,'_stop',side_effect=held),ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.case.source.stop)
            try:
                self.assertTrue(reserved.wait(10))
                self.case.base.stop_ready.set();self.case.base.save_ready.set()
                self.case.source.join(self.session)
                self.assertTrue(self.session['done'].is_set());self.assertIsNone(self.session['error'])
                self.case.publish()
                before=self.case.source.query('SELECT * FROM reservations ORDER BY operation,reference')
                run_before=self.case.source.query('SELECT record FROM runs WHERE id=?',(self.session['run'],))[0]['record']
                originals=tuple(self.session[k] for k in ('raw','stop_request','save_request','completion_response'))
                if mode=='waiting':
                    wait=selected['event'].wait
                    def await_input(timeout):
                        waiting.set()
                        value=wait(timeout)
                        if not allow_observer.wait(10):raise TimeoutError('試験の候補待機境界')
                        return value
                    self.enterContext(patch.object(selected['event'],'wait',side_effect=await_input))
                    proceed.set();self.assertTrue(waiting.wait(10))
                if overlap:
                    with self.assertRaises(RegistryError):self.f.start('selected-overlap-start')
                    self.assertEqual(before,self.case.source.query('SELECT * FROM reservations ORDER BY operation,reference'))
                    self.assertEqual(1,len(self.f.adapter.created))
                if mode in ('before','after','lost'):self.case.failure=mode
                if mode=='missing':
                    def expired(timeout):self.case.clock=11;return False
                    self.enterContext(patch.object(selected['event'],'wait',side_effect=expired))
                elif mode in ('supply_final','supply_commit','supply_signal'):
                    if mode=='supply_final':
                        original_evidence=self.managed.save_evidence;count=[0]
                        def evidence(*args):
                            value=original_evidence(*args);count[0]+=1
                            if count[0]==4:self.case.clock=11
                            return value
                        self.enterContext(patch.object(self.managed,'save_evidence',side_effect=evidence))
                    elif mode=='supply_signal':
                        signal=selected['event'].set
                        def late_signal():signal();self.case.clock=11
                        self.enterContext(patch.object(selected['event'],'set',side_effect=late_signal))
                    else:
                        transaction=self.f.registry.transaction
                        @contextmanager
                        def trace(*args,**kwargs):
                            with transaction(*args,**kwargs) as db:
                                db.set_trace_callback(lambda sql:setattr(self.case,'clock',11) if sql.startswith('UPDATE runtime_operations SET response=') and 'gameExitInputSha256' in sql else None)
                                yield db
                        self.enterContext(patch.object(self.f.registry,'transaction',side_effect=trace))
                    with self.assertRaises(RegistryError) as caught:self.supply()
                    self.assertEqual('STOP_TIMEOUT',caught.exception.code)
                elif mode.startswith('input_'):
                    field=mode[6:]
                    changes={field:'other' if field!='descriptor_sha256' else '0'*64}
                    if field=='candidate':changes={'candidate':dict(self.case.candidate,stateRevision=self.case.candidate['stateRevision']+1)}
                    snapshot=dict(selected),selected['event'].is_set()
                    with self.assertRaises(RegistryError):self.supply(**changes)
                    if field!='candidate':
                        self.assertEqual(snapshot,(dict(selected),selected['event'].is_set()))
                        self.supply()
                else:
                    if mode=='manual':
                        with self.assertRaises(Exception) as rejected:self.case.observe()
                        self.assertIsInstance(rejected.exception,RegistryError)
                        self.assertNotIn('game_exit',self.session)
                        self.assertEqual((0,0,0),self.effects())
                    candidate=deepcopy(self.case.candidate)
                    if mode=='concurrent':
                        entered,release=threading.Event(),threading.Event()
                        original_inputs=self.managed.game_exit_inputs;calls=[0]
                        def blocked(*args):
                            calls[0]+=1;self.assertEqual(1,calls[0])
                            entered.set()
                            if not release.wait(10):raise TimeoutError('試験の供給競合境界')
                            return original_inputs(*args)
                        with patch.object(self.managed,'game_exit_inputs',side_effect=blocked),ThreadPoolExecutor(max_workers=1) as supplies:
                            first=supplies.submit(self.supply)
                            try:
                                self.assertTrue(entered.wait(10))
                                with self.assertRaises(RegistryError):self.supply()
                                self.assertEqual((0,0,0),self.effects())
                            finally:release.set()
                            value=first.result(10)
                    else:
                        value=self.managed.supply_game_exit(self.f.token,deadline=self.case.deadline,**self.binding,candidate=candidate)
                    self.assertEqual(target.digest(selected['raw']),value['inputSha256'])
                    self.assertFalse(self.case.peer.adapter.processes)
                    candidate['stateRevision']+=100
                    self.assertEqual(self.case.candidate,json.loads(selected['raw'])['candidate'])
                    if mode=='reference':
                        with self.runtime._transaction(self.case.instance) as db:
                            row=db.execute('SELECT response FROM runtime_operations WHERE id=?',('public-stop',)).fetchone()
                            value=json.loads(row[0]);value['gameExitInputSha256']='0'*64
                            db.execute('UPDATE runtime_operations SET response=? WHERE id=?',(json.dumps(value),'public-stop'))
                    elif mode=='selected_binding':selected['binding']=target.canonical(dict(json.loads(selected['binding']),runId='other'))
                    with self.assertRaises(RegistryError):self.supply()
                self.assertEqual((0,0,0),self.effects())
                if mode=='commit_timeout':
                    transaction=self.f.registry.transaction
                    @contextmanager
                    def trace_commit(*args,**kwargs):
                        with transaction(*args,**kwargs) as db:
                            db.set_trace_callback(lambda sql:setattr(self.case,'clock',11) if sql.startswith("UPDATE runtime_operations SET status='COMPLETED',response=") and 'gameExit' in sql else None)
                            yield db
                    self.enterContext(patch.object(self.f.registry,'transaction',side_effect=trace_commit))
            finally:proceed.set();allow_observer.set()
            if mode in ('ok','waiting','concurrent','commit_timeout','manual') or mode.startswith('input_') and mode!='input_candidate':
                if mode=='commit_timeout':
                    with self.assertRaises(RegistryError) as caught:future.result(10)
                    self.assertEqual('STOP_TIMEOUT',caught.exception.code)
                    result=json.loads(self.case.source.query('SELECT response FROM runtime_operations WHERE id=?',('public-stop',))[0]['response'])
                else:result=future.result(10)
                self.assertIn('gameExit',result)
                self.assertEqual(self.case.candidate,result['gameExit']['candidate'])
                self.assertNotIn('ready',result['gameExit'])
                self.assertEqual((1,1,1),self.effects())
                expected=self.session['game_exit']['inputs'][0]
                request=self.case.pipes[0].sent[0]
                self.assertEqual(expected,{k:request[k] for k in expected})
                self.assertTrue(target.wire.decode_frame_v3(self.session['save_request'])['payload']['requestJson'])
                self.assertEqual('Ready',self.case.data_phase)
                self.assertEqual('LEASED_STOPPED',self.f.state()['state'])
                self.assertNotIn(self.case.instance,self.managed.sessions)
                held,originals_held,_=self.runtime._game_exit(self.session['run'],'public-stop')
                self.assertIs(self.case.c,held['observer'])
                self.assertEqual(selected['raw'],originals_held['game_exit_input'])
                self.assertEqual(selected['raw'],held['input'])
                self.assertEqual([],self.case.source.query('SELECT * FROM reservations'))
            else:
                with self.assertRaises(RegistryError) as caught:future.result(10)
                if mode in ('missing','supply_final','supply_commit','supply_signal'):self.assertEqual('STOP_TIMEOUT',caught.exception.code)
                self.assertEqual(1,self.f.state()['pending'])
                self.assertEqual(before,self.case.source.query('SELECT * FROM reservations ORDER BY operation,reference'))
                parent=self.case.source.query('SELECT * FROM runtime_operations WHERE id=?',('public-stop',))[0]
                self.assertNotEqual('COMPLETED',parent['status'])
                if mode=='supply_final':self.assertNotIn('gameExitInputSha256',json.loads(parent['response']))
                if mode in ('supply_commit','supply_signal'):self.assertEqual(target.digest(selected['raw']),json.loads(parent['response'])['gameExitInputSha256'])
                self.assertIs(self.session,self.managed.sessions[self.case.instance])
                self.assertEqual(originals,tuple(self.session[k] for k in ('raw','stop_request','save_request','completion_response')))
                if mode in ('before','after','lost','after_observe','lost_input'):
                    self.assertEqual(1,len(self.case.peer.adapter.processes))
                    self.assertEqual(0 if mode=='before' else 1,self.effects()[2])
                    if mode not in ('after_observe','lost_input'):self.assertEqual(1,self.case.c.process.code)
                else:self.assertEqual((0,0,0),self.effects())
                if mode not in ('after_observe','lost_input'):self.assertEqual(run_before,self.case.source.query('SELECT record FROM runs WHERE id=?',(self.session['run'],))[0]['record'])
                after=self.effects()
                with self.assertRaises(RegistryError):self.case.source.stop()
                self.assertEqual(after,self.effects())
                with self.assertRaises(RegistryError):self.supply()
                self.assertEqual(after,self.effects())
            self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
            self.assertEqual(1,len(self.f.adapter.created));self.assertEqual(0,self.f.rcon_calls)
            self.assertFalse(self.case.m.closed)
            if self.case.peer.adapter.processes:self.case.c.process.close.assert_not_called()

    def test_selected_stop_waits_then_observes_once_and_blocks_start(self):self.run_case('waiting',overlap=True)
    def test_concurrent_supply_rejects_second_before_io(self):self.run_case('concurrent')
    def test_commit_timeout_keeps_input_and_c_without_active_session(self):self.run_case('commit_timeout')
    def test_missing_input_times_out_without_fallback(self):self.run_case('missing')
    def test_supply_final_deadline_keeps_pending_and_effect_zero(self):self.run_case('supply_final')
    def test_supply_commit_deadline_keeps_input_reference_without_c(self):self.run_case('supply_commit')
    def test_recorded_input_hash_mismatch_rejects_before_c(self):self.run_case('reference')
    def test_selected_binding_change_rejects_before_c(self):self.run_case('selected_binding')
    def test_final_input_change_rejects_after_single_c(self):self.run_case('after_observe')
    def test_final_input_slot_removal_rejects_after_single_c(self):self.run_case('lost_input')
    def test_data_before_failure_has_effect_zero(self):self.run_case('before')
    def test_data_after_failure_has_no_additional_effect(self):self.run_case('after')
    def test_response_loss_has_no_additional_effect(self):self.run_case('lost')

    def test_manual_observe_cannot_bypass_selected_input(self):self.run_case('manual')
    def test_supply_signal_deadline_has_no_success_ack_or_c(self):self.run_case('supply_signal')

    def test_selection_duplicate_keeps_original(self):
        value=self.select();selected=self.session['game_exit_input']
        with self.assertRaises(RegistryError):self.select()
        self.assertIs(selected,self.session['game_exit_input'])
        self.assertEqual(self.session['run'],value['runId'])

    def test_selection_wrong_descriptor_has_no_effect(self):
        with self.assertRaises(RegistryError):self.select(descriptor_sha256='0'*64)
        self.assertNotIn('game_exit_input',self.session)

    def test_selection_wrong_run_has_no_effect(self):
        with self.assertRaises(RegistryError):self.select(run_id='other-run')
        self.assertNotIn('game_exit_input',self.session)

    def test_early_supply_does_not_consume_input(self):
        self.select()
        candidate=dict(worldUuid='00000000-0000-4000-8000-000000000001',runUuid='00000000-0000-4000-8000-000000000002',stateSha256='0'*64,stateRevision=0)
        with self.assertRaises(RegistryError) as caught:
            self.managed.supply_game_exit(self.f.token,**self.binding,candidate=candidate,deadline=self.deadline)
        self.assertEqual('CONTROL_UNKNOWN',caught.exception.code)
        self.assertFalse(self.session['game_exit_input']['event'].is_set())
        self.assertFalse(self.session['game_exit_input']['submitted'])
        self.assertNotIn('game_exit',self.session)
        with self.assertRaises(RegistryError):
            self.managed.supply_game_exit(self.f.token,**self.binding,candidate={},deadline=self.deadline)

    def test_selection_enters_registry_before_control_lock(self):
        entered,release=threading.Event(),threading.Event()
        transaction=self.f.registry.transaction
        @contextmanager
        def held(*args,**kwargs):
            with transaction(*args,**kwargs) as db:
                entered.set()
                if not release.wait(10):raise TimeoutError('試験のRegistry境界')
                yield db
        with patch.object(self.f.registry,'transaction',side_effect=held),ThreadPoolExecutor(max_workers=1) as pool:
            selected=pool.submit(self.select)
            try:
                self.assertTrue(entered.wait(10))
                acquired=self.managed.lock.acquire(blocking=False)
                try:self.assertTrue(acquired)
                finally:
                    if acquired:self.managed.lock.release()
            finally:release.set()
            selected.result(10)
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)


    @contextmanager
    def pending_stop(self):
        self.select()
        selected=self.session['game_exit_input']
        reserved,proceed=threading.Event(),threading.Event()
        original=self.runtime._stop
        def held(*args):
            reserved.set()
            if not proceed.wait(10):raise TimeoutError('試験のstop予約境界')
            return original(*args)
        with patch.object(self.runtime,'_stop',side_effect=held),ThreadPoolExecutor(max_workers=1) as pool:
            stopped=pool.submit(self.case.source.stop)
            try:
                self.assertTrue(reserved.wait(10))
                self.case.base.stop_ready.set();self.case.base.save_ready.set()
                self.case.source.join(self.session)
                self.assertTrue(self.session['done'].is_set());self.assertIsNone(self.session['error'])
                self.case.publish()
                selected['deadline']=self.session['stop_deadline']=self.case.deadline=30
                self.case.clock=9
                yield stopped,selected,proceed
            finally:
                if not selected['event'].is_set():
                    with self.managed.lock:
                        selected['error']=RegistryError('CONTROL_UNKNOWN','試験終了時の未供給入力')
                        selected['event'].set()
                proceed.set()

    def input_snapshot(self, selected):
        return (dict(selected),selected['event'].is_set(),
                self.case.source.query('SELECT * FROM runtime_operations ORDER BY id'),
                self.case.source.query('SELECT * FROM reservations ORDER BY operation,reference'),
                tuple(self.session[k] for k in ('raw','stop_request','save_request','completion_response')),
                dict(self.runtime.game_processes))

    def bad_token(self, field):
        token=dict(self.f.token)
        token[field]=token[field]+1 if field=='epoch' else 'other-'+token[field]
        return token

    def supply_token(self, token, deadline=30):
        return self.managed.supply_game_exit(token,**self.binding,candidate=deepcopy(self.case.candidate),deadline=deadline)

    def assert_stopped_once(self, stopped, selected, proceed):
        proceed.set()
        try:result=stopped.result(10)
        except RegistryError as error:self.fail('正規の候補供給後にstopを拒否した: '+error.code)
        self.assertEqual(self.case.candidate,result['gameExit']['candidate'])
        self.assertEqual((1,1,1),self.effects())
        self.assertTrue(selected['accepted']);self.assertIsNone(selected['error'])
        self.assertEqual(1,len(self.f.adapter.created))
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)

    def rejected_token_case(self, field, *, concurrent=False):
        with self.pending_stop() as (stopped,selected,proceed):
            before=self.input_snapshot(selected)
            bad=self.bad_token(field)
            with patch.object(target,'regular_bytes',wraps=target.regular_bytes) as reads:
                if not concurrent:
                    with self.assertRaises(RegistryError):self.supply_token(bad)
                    self.assertEqual(before,self.input_snapshot(selected))
                    self.assertEqual(0,reads.call_count)
                    self.assertEqual((0,0,0),self.effects())
                    self.supply_token(self.f.token)
                else:
                    entered,release,contending,invalid_done=(threading.Event() for _ in range(4))
                    check=self.f.registry._token
                    def token(row, supplied, **kwargs):
                        if supplied[field]==bad[field]:
                            entered.set()
                            if not release.wait(10):raise TimeoutError('試験の不正token認証境界')
                        return check(row,supplied,**kwargs)
                    def invalid():
                        try:return self.supply_token(bad)
                        finally:invalid_done.set()
                    def contention(seconds):
                        contending.set()
                        if not invalid_done.wait(10):raise TimeoutError('試験の認証tx排他境界')
                    io_entered,io_release=threading.Event(),threading.Event()
                    inputs=self.managed.game_exit_inputs
                    def held_inputs(*args):
                        io_entered.set()
                        if not io_release.wait(10):raise TimeoutError('試験の正規入力I/O境界')
                        return inputs(*args)
                    with patch.object(self.f.registry,'_token',side_effect=token),patch.object(target.time,'sleep',side_effect=contention),patch.object(self.managed,'game_exit_inputs',side_effect=held_inputs),ThreadPoolExecutor(max_workers=2) as suppliers:
                        wrong=suppliers.submit(invalid)
                        try:
                            self.assertTrue(entered.wait(10))
                            self.assertEqual(before,self.input_snapshot_without_db(selected,before))
                            self.assertEqual(0,reads.call_count)
                            valid=suppliers.submit(self.supply_token,self.f.token)
                            self.assertTrue(contending.wait(10))
                            self.assertFalse(selected['submitted']);self.assertFalse(selected['event'].is_set())
                        finally:
                            release.set()
                            if not contending.is_set():io_release.set()
                        with self.assertRaises(RegistryError):wrong.result(10)
                        try:
                            self.assertTrue(io_entered.wait(10))
                            self.assertEqual(before[2:4],self.input_snapshot(selected)[2:4])
                            self.assertEqual(0,reads.call_count)
                        finally:io_release.set()
                        valid.result(10)
                self.assertEqual((0,0,0),self.effects())
            self.assert_stopped_once(stopped,selected,proceed)

    def input_snapshot_without_db(self, selected, previous):
        return (dict(selected),selected['event'].is_set(),previous[2],previous[3],
                tuple(self.session[k] for k in ('raw','stop_request','save_request','completion_response')),
                dict(self.runtime.game_processes))

    def test_reservation_rejection_does_not_consume_input(self):
        with self.pending_stop() as (stopped,selected,proceed):
            with self.runtime._transaction(self.case.instance) as db:
                db.execute("DELETE FROM reservations WHERE reference LIKE 'stop:%'")
            before=self.input_snapshot(selected)
            with patch.object(target,'regular_bytes',wraps=target.regular_bytes) as reads:
                with self.assertRaises(RegistryError):self.supply_token(self.f.token)
                self.assertEqual(before,self.input_snapshot(selected));self.assertEqual(0,reads.call_count)
            self.assertEqual((0,0,0),self.effects())

    def short_deadline_case(self, boundary):
        with self.pending_stop() as (stopped,selected,proceed):
            before=self.case.source.query('SELECT * FROM reservations ORDER BY operation,reference')
            originals=tuple(self.session[k] for k in ('raw','stop_request','save_request','completion_response'))
            waiting,received,consume=threading.Event(),threading.Event(),threading.Event()
            wait=selected['event'].wait
            def await_input(timeout):
                waiting.set()
                value=wait(timeout)
                if boundary in ('notify_timeout','notify_success'):
                    acquired=self.managed.lock.acquire(blocking=False)
                    if acquired:self.managed.lock.release()
                    try:self.assertFalse(acquired,'供給状態の確定前に停止側がcontrol lockを取得した')
                    finally:received.set()
                else:
                    received.set()
                    if boundary=='late_consume' and not consume.wait(10):raise TimeoutError('試験の通知後消費境界')
                return value
            self.enterContext(patch.object(selected['event'],'wait',side_effect=await_input))
            if boundary in ('notify_timeout','notify_success'):
                signal=selected['event'].set
                def notifying():
                    signal()
                    if not received.wait(10):raise TimeoutError('試験の通知と停止threadの重なり')
                    if boundary=='notify_timeout':self.case.clock=11
                self.enterContext(patch.object(selected['event'],'set',side_effect=notifying))
            if boundary=='callback':self.case.after_send=lambda:setattr(self.case,'clock',11)
            if boundary=='final':
                original=self.managed.game_exit_evidence;calls=[0]
                def evidence(*args):
                    value=original(*args);calls[0]+=1
                    if calls[0]==3:self.case.clock=11
                    return value
                self.enterContext(patch.object(self.managed,'game_exit_evidence',side_effect=evidence))
            proceed.set();self.assertTrue(waiting.wait(10))
            try:
                if boundary=='notify_timeout':
                    with self.assertRaises(RegistryError) as caught:self.supply_token(self.f.token,10)
                    self.assertEqual('STOP_TIMEOUT',caught.exception.code)
                    self.assertFalse(selected['accepted'])
                    self.assertIsInstance(selected['error'],RegistryError)
                    self.assertEqual('STOP_TIMEOUT',selected['error'].code)
                else:
                    result=self.supply_token(self.f.token,10)
                    self.assertEqual(target.digest(selected['raw']),result['inputSha256'])
                    self.assertTrue(selected['accepted'])
                    if boundary=='late_consume':
                        self.assertTrue(received.wait(10));self.case.clock=11
            finally:consume.set()
            if boundary=='notify_success':
                self.assert_stopped_once(stopped,selected,proceed)
                self.assertEqual(10,self.session['stop_deadline'])
                return
            error=None
            try:stopped.result(10)
            except RegistryError as caught:error=caught
            effect=1 if boundary in ('callback','final') else 0
            self.assertEqual(effect,self.effects()[2]);self.assertEqual(effect,self.effects()[0])
            self.assertIsInstance(error,RegistryError)
            self.assertEqual('STOP_TIMEOUT',error.code)
            self.assertEqual(10,json.loads(selected['raw'])['deadline'])
            self.assertEqual(before,self.case.source.query('SELECT * FROM reservations ORDER BY operation,reference'))
            self.assertEqual(originals,tuple(self.session[k] for k in ('raw','stop_request','save_request','completion_response')))
            self.assertEqual(1,self.f.state()['pending'])
            parent=self.case.source.query('SELECT * FROM runtime_operations WHERE id=?',('public-stop',))[0]
            self.assertNotEqual('COMPLETED',parent['status'])
            self.assertEqual(target.digest(selected['raw']),json.loads(parent['response'])['gameExitInputSha256'])
            count=self.effects()
            with self.assertRaises(RegistryError):self.supply_token(self.f.token,30)
            with self.assertRaises(RegistryError):self.case.source.stop()
            self.assertEqual(count,self.effects());self.assertFalse(self.case.m.closed)
            self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)

    def test_short_supply_deadline_blocks_late_consumer_after_success(self):self.short_deadline_case('late_consume')
    def test_short_supply_notification_timeout_with_waiting_stop(self):self.short_deadline_case('notify_timeout')
    def test_short_supply_notification_success_with_waiting_stop(self):self.short_deadline_case('notify_success')
    def test_short_supply_deadline_reaches_callback(self):self.short_deadline_case('callback')
    def test_short_supply_deadline_reaches_final_transaction(self):self.short_deadline_case('final')


def input_case(field):
    def test(self):self.run_case('input_'+field)
    return test

for field in ('run_id','operation_id','descriptor_sha256','candidate'):
    setattr(SelectedGameExitTests,'test_supply_'+field+'_mismatch_has_effect_zero',input_case(field))


class LegacyStopTests(unittest.TestCase):
    def test_unselected_stop_keeps_existing_managed_interface(self):
        base=legacy.RuntimeControlTests();base.setUp();self.addCleanup(base.doCleanups)
        base.prepare();base.start()
        self.assertFalse(hasattr(base.registry.runtime.managed,'observe_selected_game_exit'))
        try:result=base.stop()
        except RegistryError as error:self.fail('未選択の既存stopを拒否した: '+error.code)
        self.assertTrue(result['runtimeStopped'])
        self.assertNotIn('gameExit',result)
        self.assertEqual(1,len(base.adapter.created))
        self.assertIs(base.registry.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)


def token_case(field, concurrent):
    def test(self):self.rejected_token_case(field,concurrent=concurrent)
    return test

for field in ('leaseId','epoch','expectedGeneration'):
    setattr(SelectedGameExitTests,'test_rejected_token_'+field+'_preserves_input',token_case(field,False))
    setattr(SelectedGameExitTests,'test_rejected_token_'+field+'_races_valid_supply',token_case(field,True))

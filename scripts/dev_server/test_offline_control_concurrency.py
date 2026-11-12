"""実Registry/instance lockでoffline取得とstartを決定的に競合させる。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import errno
import json
import sys
import threading
import time
import unittest
from unittest.mock import patch

from . import offline_control, runtime_control, supervisor
from . import test_runtime_control as runtime_tests
from .build_lock import BuildLockError
from .registry import RegistryError


@unittest.skipUnless(sys.platform in ('win32','darwin'), '対象OSの実instance lockを使う合成試験')
class OfflineConcurrencyTests(unittest.TestCase):
    def setUp(self):
        f=runtime_tests.RuntimeControlTests('test_prepare_start_stop_replay_restart_and_no_snapshot_clean')
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f=f
        self.generation=f.prepare()
        f.start()
        f.stop()
        self.runtime=f.registry.runtime
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        with self.runtime._transaction(f.token['instanceId']) as connection:
            row=f.registry._instance(connection,f.token['instanceId'])
            # 既存の合成Modの停止記録に、認証済みwireの由来をfixture値として追加する。
            proof=json.loads(row['runtime_proof'])
            proof['stopOperationId']='stop-01'
            connection.execute('UPDATE instances SET runtime_proof=? WHERE id=?',(json.dumps(proof),row['id']))
            connection.execute("UPDATE runtime_operations SET response=? WHERE id='stop-01'",(json.dumps(proof),))
            connection.execute('UPDATE run_admission SET descriptor_hash=? WHERE run=?',('d'*64,row['run_id']))
            self.old_pid=json.loads(connection.execute('SELECT record FROM runs WHERE id=?',(row['run_id'],)).fetchone()[0])['identity']['pid']
            self.revision=connection.execute('SELECT revision FROM run_admission WHERE run=?',(row['run_id'],)).fetchone()[0]
        self.ready=dict(worldUuid='00000000-0000-4000-8000-000000000001',
                        runUuid='00000000-0000-4000-8000-000000000001',stateSha256='a'*64,stateRevision=0)

    def race(self,winner):
        loser='start' if winner=='offline' else 'offline'
        f=self.f
        runtime=self.runtime
        instance=f.token['instanceId']
        barrier=threading.Barrier(2)
        holding,blocked,generated,release=(threading.Event() for _ in range(4))
        local=threading.local()
        controllers=[]
        initial_mods=len(f.adapter.created)
        original_identity=f.adapter.identity
        original_endpoints=runtime_control.instance_endpoints
        original_instance=f.registry._instance
        original_lock=supervisor.private_lock

        def rendezvous(role):
            self.assertEqual(role,local.role)
            if getattr(local,'preflight_done',False): return
            local.preflight_done=True
            barrier.wait(10)
            if role==loser:
                self.assertTrue(holding.wait(10),'先行予約がinstance lockを取得していません')

        def identity(pid):
            result=original_identity(pid)
            if getattr(local,'role',None)=='offline' and pid==self.old_pid:
                rendezvous('offline')
            return result

        def endpoints(declaration):
            result=original_endpoints(declaration)
            if getattr(local,'role',None)=='start': rendezvous('start')
            return result

        def observed_instance(connection,instance_id):
            row=original_instance(connection,instance_id)
            if (getattr(local,'role',None)==winner and not getattr(local,'write_observed',False)
                    and connection.execute('PRAGMA query_only').fetchone()[0]==0):
                local.write_observed=True
                holding.set()
                self.assertTrue(blocked.wait(10),'後行側の実instance lock競合を観測できません')
            return row

        @contextmanager
        def observed_lock(path):
            entered=False
            try:
                with original_lock(path):
                    entered=True
                    yield
            except BuildLockError as error:
                if (not entered and getattr(local,'role',None)==loser and holding.is_set()
                        and path.name=='instance-'+instance+'.lock'
                        and isinstance(error.__cause__,OSError) and error.__cause__.errno in (errno.EACCES,errno.EAGAIN)):
                    blocked.set()
                raise

        class Controller:
            def __init__(self,bundle,diagnostics,*,native,verify,process_adapter):
                self.verify=verify
                self.effects=0
                self.ready=False
                controllers.append(self)

            def start(self,frame,deadline):
                self.verify()
                self.effects+=1
                generated.set()
                if not release.wait(10): raise AssertionError('C合成生成の解除期限')
                self.verify()
                self.ready=True
                return dict(status='OPEN',operationId=frame['operationId'],requestHash=frame['requestHash'],
                            sessionId='private-session',nonce='private-nonce',stateSha256='b'*64,stateRevision=1,
                            runUuid='00000000-0000-4000-8000-000000000002')

        def mod_generated(process):
            self.assertEqual('start',local.role)
            self.assertEqual(winner,local.role,'後行側のMod生成')
            generated.set()
            self.assertTrue(release.wait(10),'Mod合成生成の解除期限')

        def call(role):
            local.role=role
            with runtime._lock_wait(time.monotonic()+30):
                if role=='offline':
                    return runtime._offline_open(f.token,operation_id='offline-race',revision=self.revision,
                                                 ready=self.ready,deadline=time.monotonic()+30)
                return runtime.execute('start',dict(f.token,timeoutSeconds=30),operation_id='start-race')

        f.adapter.start_fault=mod_generated
        with patch.object(f.adapter,'identity',side_effect=identity), \
                patch.object(runtime_control,'instance_endpoints',side_effect=endpoints), \
                patch.object(f.registry,'_instance',side_effect=observed_instance), \
                patch.object(supervisor,'private_lock',side_effect=observed_lock), \
                patch.object(offline_control,'OfflineProcess',Controller), ThreadPoolExecutor(max_workers=2) as workers:
            futures={role:workers.submit(call,role) for role in ('offline','start')}
            try:
                self.assertTrue(generated.wait(10),'先行側の生成境界に到達していません')
                self.assertTrue(blocked.is_set(),'実lock取得拒否を経ていません')
                self.assertTrue(barrier.broken is False)
                with self.assertRaises(RegistryError) as error:
                    futures[loser].result(timeout=10)
                self.assertEqual('BUSY' if loser=='start' else 'WRITER_PENDING',error.exception.code)
                with f.registry.transaction(read_only=True) as connection:
                    row=f.registry._instance(connection,instance)
                    self.assertEqual(('STARTING' if winner=='start' else 'LEASED_STOPPED',1),(row['state'],row['pending']))
                    operation=connection.execute('SELECT * FROM runtime_operations WHERE id=?',(winner+'-race',)).fetchone()
                    self.assertEqual('PENDING',operation['status'])
                    refs=connection.execute('SELECT * FROM reservations WHERE instance=?',(instance,)).fetchall()
                    self.assertEqual([(winner+'-race','artifact:'+f.artifact)],[(r['operation'],r['reference']) for r in refs])
                    self.assertIsNone(connection.execute('SELECT 1 FROM runtime_operations WHERE id=?',(loser+'-race',)).fetchone())
                self.assertEqual((1,0) if winner=='offline' else (0,1),
                                 (sum(c.effects for c in controllers),len(f.adapter.created)-initial_mods))
            finally:
                release.set()
                barrier.abort()
            result=futures[winner].result(timeout=10)

        self.assertIs(runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        if winner=='offline':
            self.assertEqual('OPEN',result['status'])
            with self.assertRaises(RegistryError):
                runtime.execute('start',dict(f.token,timeoutSeconds=30),operation_id='start-race')
            with self.assertRaises(RegistryError):
                runtime._offline_open(f.token,operation_id='offline-race',revision=self.revision,ready=self.ready,deadline=time.monotonic()+30)
            with f.registry.transaction(read_only=True) as connection:
                self.assertEqual('PENDING',connection.execute("SELECT status FROM runtime_operations WHERE id='offline-race'").fetchone()[0])
                self.assertEqual(1,connection.execute("SELECT count(*) FROM reservations WHERE operation='offline-race'").fetchone()[0])
                self.assertEqual(1,f.registry._instance(connection,instance)['pending'])
        else:
            self.assertTrue(result['running'])
            with self.assertRaises(RegistryError):
                runtime._offline_open(f.token,operation_id='offline-race',revision=self.revision,ready=self.ready,deadline=time.monotonic()+30)
            replay=runtime.execute('start',dict(f.token,timeoutSeconds=30),operation_id='start-race')
            self.assertTrue(replay['replayed'])
            with f.registry.transaction(read_only=True) as connection:
                self.assertEqual('COMPLETED',connection.execute("SELECT status FROM runtime_operations WHERE id='start-race'").fetchone()[0])
                self.assertEqual(0,connection.execute("SELECT count(*) FROM reservations WHERE operation='start-race'").fetchone()[0])
                self.assertIsNone(connection.execute("SELECT 1 FROM runtime_operations WHERE id='offline-race'").fetchone())
        self.assertEqual((1,0) if winner=='offline' else (0,1),
                         (sum(c.effects for c in controllers),len(f.adapter.created)-initial_mods))

    def test_offline_first_retains_pending_and_blocks_start_after_real_lock_contention(self):
        self.race('offline')

    def test_start_first_retains_reservation_and_blocks_offline_after_real_lock_contention(self):
        self.race('start')

"""共通M生成contextの寿命と通常startの正本・失敗保全を検証する。"""
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from . import runtime_control as control
from . import test_runtime_control as runtimes
from .registry import RegistryError


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.base = runtimes.RuntimeControlTests()
        self.addCleanup(self.base.doCleanups)
        self.base.setUp()
        self.base.prepare()
        self.runtime = self.base.registry.runtime
        self.clock = [0.0]
        self.enterContext(patch.object(control.time,'monotonic',lambda:self.clock[0]))
        self.logs,self.events = [],[]
        prebind = control.prebind
        def probing(endpoints):
            self.events.append('prebind')
            return prebind(endpoints)
        self.enterContext(patch.object(control,'prebind',side_effect=probing))
        original = runtimes.FakeProcess.start
        def start(process,arguments,cwd,log,nonce,created,**kwargs):
            self.logs.append(log);self.events.append('spawn')
            self.assertFalse(log.closed)
            self.assertIsNotNone(kwargs.get('before_create'))
            return original(process,arguments,cwd,log,nonce,created,**kwargs)
        self.enterContext(patch.object(runtimes.FakeProcess,'start',start))

    def start(self):
        try:return self.base.start()
        except RegistryError as error:self.fail(f'正常startを拒否: {error.code}')

    def retained_failure(self):
        with self.assertRaises(RegistryError):self.base.start()
        row = self.base.state()
        self.assertEqual('RECOVERY_REQUIRED',row['state'])
        self.assertEqual(1,len(self.base.adapter.created))
        self.assertIn(row['run_id'],self.runtime.processes)
        process = self.base.adapter.created[0]
        self.assertIs(process,self.runtime.processes[row['run_id']])
        self.assertFalse(process.closed)
        self.assertTrue(self.logs[0].closed)
        with self.base.registry.transaction(read_only=True) as db:
            self.assertEqual('RECOVERY_REQUIRED',db.execute("SELECT status FROM runtime_operations WHERE id='start-01'").fetchone()[0])
            self.assertEqual(1,db.execute("SELECT count(*) FROM reservations WHERE operation='start-01'").fetchone()[0])
            self.assertEqual(3,db.execute('SELECT count(*) FROM endpoint_reservations').fetchone()[0])
        self.base.assert_code('RECOVERY_REQUIRED',self.base.start)
        self.assertEqual(1,len(self.base.adapter.created))
        return self.runtime._load_run(row),process

    def test_console_stays_open_through_handshake_and_proof(self):
        prepare = self.runtime.managed.prepare
        proof = self.runtime._proof
        def preparing(*args):
            self.events.append('prepare')
            return prepare(*args)
        def handshake(*args):
            self.events.append('handshake')
            self.assertFalse(self.logs[0].closed)
            self.assertEqual(2.0,args[-1])
        def proving(*args,**kwargs):
            self.events.append('proof')
            self.assertFalse(self.logs[0].closed)
            return proof(*args,**kwargs)
        with patch.object(self.runtime.managed,'prepare',side_effect=preparing), \
             patch.object(self.runtime.managed,'handshake',side_effect=handshake), \
             patch.object(self.runtime,'_proof',side_effect=proving):
            result = self.start()
        self.assertTrue(result['running'])
        self.assertEqual(['prepare','prebind','spawn','handshake','proof'],self.events)
        self.assertTrue(self.logs[0].closed)
        row = self.base.state()
        run = self.runtime._load_run(row)
        self.assertEqual(result['runId'],'run-'+run['nonce'])
        self.assertEqual(run['nonce'],self.base.adapter.created[0].group)
        self.assertEqual('RUNNING',row['state'])
        with self.base.registry.transaction(read_only=True) as db:
            self.assertEqual('OPEN',db.execute('SELECT gate FROM run_admission WHERE run=?',(result['runId'],)).fetchone()[0])
            self.assertEqual(0,db.execute('SELECT count(*) FROM reservations').fetchone()[0])
            self.assertEqual(3,db.execute('SELECT count(*) FROM endpoint_reservations').fetchone()[0])

    def test_prepare_failure_has_no_process_and_keeps_reservations(self):
        with patch.object(self.runtime.managed,'prepare',side_effect=RegistryError('PREPARE_FAILED','合成失敗')):
            with self.assertRaises(RegistryError):self.base.start()
        self.assertEqual([],self.base.adapter.created)
        self.assertEqual([],self.logs)
        self.assertEqual('RECOVERY_REQUIRED',self.base.state()['state'])
        with self.base.registry.transaction(read_only=True) as db:
            self.assertEqual(3,db.execute('SELECT count(*) FROM endpoint_reservations').fetchone()[0])
            self.assertEqual(1,db.execute('SELECT count(*) FROM reservations').fetchone()[0])

    def test_creation_failure_records_latest_stage_and_keeps_original_process(self):
        def fail(process):raise OSError('合成生成後失敗')
        self.base.adapter.creation_fault = fail
        run,process = self.retained_failure()
        self.assertEqual('VERIFIED',process.stage)
        self.assertEqual(dict(pid=process.pid,stage='VERIFIED',ownerGroup=process.group),run['creation'])
        self.assertEqual(process.pid,run['identity']['pid'])

    def test_handshake_failure_closes_log_without_closing_process(self):
        with patch.object(self.runtime.managed,'handshake',side_effect=RegistryError('CONTROL_UNKNOWN','合成ACK不明')):
            run,process = self.retained_failure()
        self.assertEqual('RUNNING',run['creation']['stage'])
        self.assertEqual(process.pid,run['creation']['pid'])

    def test_public_renew_commits_in_other_thread_during_prepare(self):
        prepare = self.runtime.managed.prepare
        before = self.base.state()['expires']
        with ThreadPoolExecutor(max_workers=1) as pool:
            def preparing(*args):
                pool.submit(self.base.registry.execute,'renew',dict(self.base.token,ttlSeconds=600),
                            operation_id='creation-renew').result(10)
                return prepare(*args)
            with patch.object(self.runtime.managed,'prepare',side_effect=preparing):result = self.start()
        self.assertGreater(self.base.state()['expires'],before)
        self.assertEqual('RUNNING',self.base.state()['state'])
        self.assertEqual(result['runId'],self.base.state()['run_id'])
        self.assertEqual(1,len(self.base.adapter.created))

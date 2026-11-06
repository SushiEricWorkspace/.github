"""実SQLite正本と合成Cによるoffline予約。OS/DB保存算法は再実装しない。"""

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import offline_control as target
from .build_inputs import canonical, digest
from .registry import RegistryError
from .contracts import ContractError
from . import test_mod_control_startup as startup_tests

WORLD = '00000000-0000-4000-8000-000000000001'
RUN = '00000000-0000-4000-8000-000000000002'


class Controller:
    def __init__(self, fixture, bundle, diagnostics, *, native, verify, process_adapter):
        self.f = fixture
        self.bundle, self.diagnostics, self.native, self.verify, self.adapter = bundle, diagnostics, native, verify, process_adapter
        self.effects = 0
        self.request = None
        self.ready = False
        fixture.controllers.append(self)
        fixture.on_construct()

    def start(self, frame, deadline):
        self.f.assertEqual(0, self.f.fixture.depth)
        self.f.assertIs(self, self.f.runtime._offline.controllers['slot-a'])
        self.verify()
        self.request = deepcopy(frame)
        self.f.before_effect()
        self.verify()
        self.effects += 1
        self.f.after_effect()
        self.verify()
        response = {k: frame[k] for k in ('operationId','requestHash')}
        response.update(status='OPEN',sessionId='private-session',nonce='private-nonce',
                        stateSha256='b'*64,stateRevision=1,runUuid=RUN)
        self.ready = True
        return response


class OfflineControlTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.generation = self.root / 'generation'
        self.generation.mkdir()
        (self.generation/'server.properties').write_bytes(b'level-name=world\n')
        f = startup_tests.StartupTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.fixture = f
        self.db, self.runtime, self.registry = f.db, f.runtime, f.registry
        self.registry.root = self.root
        self.registry.db_lock = threading.RLock()
        self.registry.resource_users = 0
        self.registry.gc_active = self.registry.stopping = False
        self.runtime.managed = SimpleNamespace(transport=SimpleNamespace(native=object()))
        self.runtime._active_generation = Mock(return_value=self.generation)
        self.runtime.process_adapter.identity.return_value = None
        self.run = deepcopy(f.run)
        self.run.update(identity={'pid':321,'startId':'old-start'},jvmAndChildrenExited=True,runtimeSaveCompleted=True,
                        exitCode=0,proofHash='f'*64)
        self.proof = dict(instanceId='slot-a',generation=f.token['expectedGeneration'],runId=self.run['runId'],nonce=self.run['nonce'],
                          runtimeStopped=True,modSaveCompleted=True,jvmAndChildrenExited=True,exitCode=0,
                          proofHash='f'*64,stopOperationId='stop-a')
        with f.transaction() as db:
            db.execute("UPDATE instances SET state='LEASED_STOPPED',pending=0,runtime_proof=? WHERE id='slot-a'",(json.dumps(self.proof),))
            db.execute('UPDATE runs SET record=?',(json.dumps(self.run),))
            db.execute("UPDATE run_admission SET gate='BLOCKED',revision=5,descriptor_hash=?",('d'*64,))
            db.execute("UPDATE runtime_operations SET status='COMPLETED'")
            db.execute('DELETE FROM reservations')
            db.execute("INSERT INTO runtime_operations VALUES ('stop-a','stop-hash','stop','slot-a','COMPLETED',?)",(json.dumps(self.proof),))
        self.controllers = []
        self.on_construct = self.before_effect = self.after_effect = lambda: None
        self.ready = dict(worldUuid=WORLD,runUuid=WORLD,stateSha256='a'*64,stateRevision=0)
        self.enterContext(patch.object(target,'OfflineProcess',lambda *a,**k: Controller(self,*a,**k)))
        self.enterContext(patch.object(target,'verify_bundle',return_value={'artifactId':self.run['artifactId'],'schemaVersion':2}))
        self.seed = sqlite3.connect(':memory:')
        self.db.backup(self.seed)
        self.addCleanup(self.seed.close)

    def reset(self):
        self.seed.backup(self.db)
        self.runtime._offline = None
        self.controllers.clear()
        self.runtime.process_adapter.identity.return_value = None
        self.on_construct = self.before_effect = self.after_effect = lambda: None

    def open(self, **changes):
        options = dict(operation_id='offline-a',revision=5,ready=self.ready,deadline=100)
        options.update(changes)
        return self.runtime._offline_open(deepcopy(self.fixture.token),**options)

    def mutate(self, sql, args=()):
        with self.fixture.transaction() as db: db.execute(sql,args)

    def state(self):
        return dict(self.db.execute("SELECT * FROM instances WHERE id='slot-a'").fetchone())

    def assert_no_owner(self):
        self.assertEqual(0,sum(c.effects for c in self.controllers))

    def assert_pending(self):
        self.assertEqual(1,self.state()['pending'])
        self.assertEqual('PENDING',self.db.execute("SELECT status FROM runtime_operations WHERE id='offline-a'").fetchone()[0])
        self.assertEqual(1,self.db.execute("SELECT count(*) FROM reservations WHERE operation='offline-a'").fetchone()[0])

    def assert_no_retry(self):
        before = (len(self.controllers),sum(c.effects for c in self.controllers))
        with self.assertRaises(RegistryError): self.open()
        self.assertEqual(before,(len(self.controllers),sum(c.effects for c in self.controllers)))
        self.assert_pending()

    def writer(self):
        f = self.fixture
        with f.transaction() as db:
            db.execute("INSERT INTO writer_sessions VALUES ('writer-a','slot-a',?,?,?,?,?,123,'{}','CLOSED',0)",
                       (f.token['leaseId'],f.token['epoch'],f.token['expectedGeneration'],self.run['runId'],self.run['nonce']))
            closed = dict(proxyExited=True,proxyChildrenExited=True,proxyIdentity=dict(hostHash='a'*64,bootId='boot',pid=456,startId='456'))
            db.execute("INSERT INTO writer_operations VALUES ('close-a','hash','writer-a','writer-close','COMPLETED',?)",(json.dumps(closed),))

    def test_registry_reservation_precedes_owner_and_open_stays_pending(self):
        self.writer()
        self.before_effect = self.assert_pending
        response = self.open()
        c = self.controllers[0]
        self.assertEqual('OPEN',response['status'])
        self.assertEqual(1,c.effects)
        self.assert_pending()
        self.assertEqual('BLOCKED',self.fixture.gate()['gate'])
        self.assertEqual(6,self.fixture.gate()['revision'])
        self.assertEqual(str(self.generation/'world'),c.request['worldRoot'])
        self.assertEqual(str(self.generation/'config/SushiEricServerMod/shop_data'),c.request['shopDirectory'])
        self.assertIs(self.runtime.managed.transport.native,c.native)
        saved=json.loads(self.db.execute("SELECT response FROM runtime_operations WHERE id='offline-a'").fetchone()[0])
        self.assertEqual(digest(canonical(saved['binding'])),c.request['drainSha256'])
        self.assertEqual(digest(canonical(response)),saved['startupResponseSha256'])
        self.assertNotIn('private-nonce',json.dumps(saved))
        self.assertNotIn('private-session',json.dumps(saved))
        self.assertEqual(self.ready,saved['binding']['ready'])
        self.assert_no_retry()

    def test_each_lease_generation_state_and_count_mismatch_rejects_before_creation(self):
        cases=[('epoch',99),('lease','old'),('generation',WORLD),('state','RUNNING'),('expires',999),
               ('generation_bound',0),('pending',1),('writers',1),('children',1)]
        for field,value in cases:
            with self.subTest(field=field):
                self.reset()
                self.mutate('UPDATE instances SET '+field+'=?',(value,))
                with self.assertRaises(RegistryError): self.open()
                self.assertEqual([],self.controllers)

    def test_each_gate_binding_mismatch_rejects_before_creation(self):
        for field,value in [('gate','OPEN'),('revision',4),('descriptor_hash',None),('nonce','old'),
                            ('lease','old'),('epoch',99),('generation',WORLD),('start_operation','stop-a')]:
            with self.subTest(field=field):
                self.reset()
                self.mutate('UPDATE run_admission SET '+field+'=?',(value,))
                with self.assertRaises((RegistryError,sqlite3.IntegrityError)): self.open()
                self.assertEqual([],self.controllers)

    def test_each_saved_stop_proof_mismatch_rejects_before_creation(self):
        for field,value in [('instanceId','old'),('generation',WORLD),('runId','old'),('nonce','old'),
                            ('runtimeStopped',False),('modSaveCompleted',False),('jvmAndChildrenExited',False),
                            ('exitCode',False),('proofHash','e'*64),('stopOperationId','missing')]:
            with self.subTest(field=field):
                self.reset()
                proof=self.proof | {field:value}
                self.mutate('UPDATE instances SET runtime_proof=?',(json.dumps(proof),))
                self.mutate("UPDATE runtime_operations SET response=? WHERE id='stop-a'",(json.dumps(proof),))
                with self.assertRaises(RegistryError): self.open()
                self.assertEqual([],self.controllers)

    def test_each_run_exit_proof_mismatch_rejects_before_creation(self):
        for field,value in [('generationId',WORLD),('artifactId','e'*64),('runId','old'),('jvmAndChildrenExited',False),
                            ('runtimeSaveCompleted',False),('exitCode',False),('identity',None)]:
            with self.subTest(field=field):
                self.reset()
                self.mutate('UPDATE runs SET record=?',(json.dumps(self.run | {field:value}),))
                with self.assertRaises(RegistryError): self.open()
                self.assertEqual([],self.controllers)

    def test_each_writer_terminal_proof_mismatch_rejects_before_creation(self):
        cases=[("UPDATE writer_sessions SET status='UNKNOWN'",()),("UPDATE writer_sessions SET pending=1",()),
               ("UPDATE writer_operations SET status='PENDING'",()),("DELETE FROM writer_operations",()),
               ("UPDATE writer_operations SET response=?",(json.dumps({'proxyExited':True,'proxyChildrenExited':False}),))]
        for sql,args in cases:
            with self.subTest(sql=sql):
                self.reset(); self.writer(); self.mutate(sql,args)
                with self.assertRaises(RegistryError): self.open()
                self.assertEqual([],self.controllers)

    def test_old_runtime_pending_and_reference_are_preserved(self):
        for sql in ("UPDATE runtime_operations SET status='PENDING' WHERE id='stop-a'",
                    "INSERT INTO reservations VALUES ('old','writer-call:old','slot-a','old',99)"):
            with self.subTest(sql=sql):
                self.reset(); self.mutate(sql)
                with self.assertRaises(RegistryError): self.open()
                self.assertEqual([],self.controllers)

    def test_old_m_pid_reuse_and_bundle_mismatch_reject_before_reservation(self):
        self.runtime.process_adapter.identity.return_value={'pid':321,'startId':'reused'}
        with self.assertRaises(RegistryError): self.open()
        self.assertEqual(0,self.state()['pending']); self.assertEqual([],self.controllers)
        self.reset()
        with patch.object(target,'verify_bundle',return_value={'artifactId':'different','schemaVersion':2}):
            with self.assertRaises(RegistryError): self.open()
        self.assertEqual(0,self.state()['pending']); self.assertEqual([],self.controllers)

    def test_start_and_release_are_blocked_while_owner_is_held(self):
        self.open()
        self.runtime._start=Mock(side_effect=AssertionError('実起動禁止'))
        with self.assertRaises(RegistryError):
            self.runtime.execute('start',dict(self.fixture.token,timeoutSeconds=1),operation_id='start-b')
        self.runtime._start.assert_not_called()
        with self.fixture.transaction(read_only=True) as db:
            with self.assertRaises(RegistryError): self.registry._safe_release(db,self.state())
        self.assert_pending()

    def test_competing_start_reservation_wins_without_owner_effect(self):
        def active(row):
            self.mutate("UPDATE instances SET state='STARTING',pending=1")
            return self.generation
        self.runtime._active_generation.side_effect=active
        with self.assertRaises(RegistryError): self.open()
        self.assertEqual([],self.controllers)
        self.assertIsNone(self.db.execute("SELECT 1 FROM runtime_operations WHERE id='offline-a'").fetchone())

    def test_gate_or_run_changes_during_preflight_reject_before_owner(self):
        for change in ('descriptor','run'):
            with self.subTest(change=change):
                self.reset()
                def active(row):
                    if change=='descriptor': self.mutate('UPDATE run_admission SET descriptor_hash=?',('e'*64,))
                    else: self.mutate('UPDATE runs SET record=?',(json.dumps(self.run | {'exitedAt':10}),))
                    return self.generation
                self.runtime._active_generation.side_effect=active
                with self.assertRaises(RegistryError): self.open()
                self.assertEqual([],self.controllers)
        self.runtime._active_generation.side_effect=None

    def test_each_reserved_binding_mismatch_prevents_owner_effect(self):
        for change in ('revision','descriptor','run','proof','writers','reservation','operation'):
            with self.subTest(change=change):
                self.reset()
                def alter():
                    if change=='revision': self.mutate('UPDATE run_admission SET revision=revision+1')
                    elif change=='descriptor': self.mutate('UPDATE run_admission SET descriptor_hash=?',('e'*64,))
                    elif change=='run': self.mutate('UPDATE runs SET record=?',(json.dumps(self.run | {'exitedAt':10}),))
                    elif change=='proof': self.mutate('UPDATE instances SET runtime_proof=?',(json.dumps(self.proof | {'nonce':'old'}),))
                    elif change=='writers': self.mutate('UPDATE instances SET writers=1')
                    elif change=='reservation': self.mutate("UPDATE reservations SET epoch=99")
                    else: self.mutate("UPDATE runtime_operations SET fingerprint='old' WHERE id='offline-a'")
                self.on_construct=alter
                with self.assertRaises(RegistryError): self.open()
                self.assertEqual(1,len(self.controllers)); self.assert_no_owner()

    def test_post_owner_response_loss_preserves_original_pending_and_no_retry(self):
        def lost(): raise EOFError('fixture C owner公開後の応答喪失')
        self.after_effect=lost
        with self.assertRaises(EOFError): self.open()
        self.assertEqual(1,self.controllers[0].effects)
        self.assert_pending(); self.assert_no_retry()

    def test_post_owner_binding_failure_has_no_additional_effect(self):
        self.after_effect=lambda: self.mutate('UPDATE run_admission SET revision=revision+1')
        with self.assertRaises(RegistryError): self.open()
        self.assertEqual(1,self.controllers[0].effects)
        self.assert_pending(); self.assert_no_retry()

    def test_post_owner_journal_failure_is_not_promoted_to_completion(self):
        def after():
            self.mutate("CREATE TRIGGER deny_offline_result BEFORE UPDATE OF response ON runtime_operations WHEN OLD.command='offline-open' BEGIN SELECT RAISE(FAIL,'fixture 記録失敗'); END")
        self.after_effect=after
        with self.assertRaises(sqlite3.IntegrityError): self.open()
        self.assertEqual(1,self.controllers[0].effects)
        self.assert_pending(); self.assert_no_retry()

    def test_world_escape_is_rejected_before_reservation_or_owner(self):
        for name in ('../outside',str(self.root/'outside'),'world\\..\\outside'):
            with self.subTest(name=name):
                self.reset()
                (self.generation/'server.properties').write_bytes(('level-name='+name+'\n').encode())
                with self.assertRaises((RegistryError,ContractError)): self.open()
                self.assertEqual([],self.controllers)
                self.assertEqual(0,self.state()['pending'])

    def test_gc_and_supervisor_stop_guard_cover_preflight_and_owner_io(self):
        for flag in ('gc_active','stopping'):
            with self.subTest(flag=flag):
                setattr(self.registry,flag,True)
                with self.assertRaises(RegistryError): self.open()
                self.assertEqual([],self.controllers)
                self.assertEqual(0,self.state()['pending'])
                setattr(self.registry,flag,False)
        def protected():
            self.assertEqual(1,self.registry.resource_users)
            with self.assertRaises(RegistryError):
                with self.registry.resource_use(exclusive=True): self.fail('GC作用禁止')
        self.before_effect=protected
        self.open()
        self.assertEqual(0,self.registry.resource_users)
        self.assert_pending()

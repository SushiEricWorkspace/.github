"""固定Ready証拠と新runのports予約を実Registry transactionで結束する。"""
import json
import threading
import unittest
from unittest.mock import patch

from . import offline_control, runtime_ports
from . import test_offline_start_reservation as starts
from .registry import RegistryError


class StartPortTests(unittest.TestCase):
    def setUp(self):
        self.s = starts.StartReservationTests()
        self.addCleanup(self.s.doCleanups)
        self.s.setUp()
        self.runtime = self.s.runtime
        self.registry = self.runtime.registry
        declaration = next(v for v in self.registry.inventory['instances'] if v['instanceId']==self.s.token['instanceId'])
        self.endpoints = runtime_ports.instance_endpoints(declaration)

    def ports(self):
        with self.registry.transaction(read_only=True) as db:
            return [dict(v) for v in db.execute('SELECT * FROM endpoint_reservations ORDER BY run,service')]

    def commit(self, change):
        def write():
            with self.registry.transaction() as db:change(db)
        self.s.base.pool.submit(write).result(10)

    def competitor(self, *, service=None, instance='competitor', run='competing-run', free=False):
        service = service or next(iter(self.endpoints))
        value = dict(self.endpoints[service],address='0.0.0.0')
        if free:value['port'] = max(v['port'] for v in self.endpoints.values())+10
        self.commit(lambda db:runtime_ports.reserve(db,instance,run,{'competing':value}))

    def test_same_fixed_packet_includes_all_durable_ports_without_m_creation(self):
        with patch.object(self.runtime,'_start',side_effect=AssertionError('M生成は後続')) as create:
            try:
                result,binding = self.s.reserve()
                encoded = binding()
            except RegistryError as error:self.fail(f'正常な全ports予約を拒否: {error.code}')
        intent = json.loads(encoded)['offlineStart']
        expected = [dict(run=result['runId'],instance=self.s.token['instanceId'],service=k,**v)
                    for k,v in self.endpoints.items()]
        self.assertEqual(sorted(expected,key=lambda v:v['service']),self.ports())
        self.assertEqual(self.endpoints,intent['endpoints'])
        self.assertEqual('run-'+intent['nonce'],result['runId'])
        self.assertEqual((1,1,1,1),self.s.base.p.effects())
        self.assertNotIn(result['runId'],self.runtime.processes)
        create.assert_not_called()
        self.s.no_retry()

    def test_competitor_commits_first_and_partial_ports_and_start_roll_back(self):
        old = self.s.row()
        self.s.before_write(lambda:self.competitor(service=list(self.endpoints)[-1]))
        self.assertEqual(old,self.s.row())
        self.assertEqual(['competing-run'],[v['run'] for v in self.ports()])

    def test_competing_real_writer_waits_for_start_commit_and_cannot_steal_ports(self):
        reserve = offline_control.reserve
        entered = threading.Event()
        futures = []
        def competing():
            entered.set()
            with self.registry.transaction() as db:
                runtime_ports.reserve(db,'competitor','competing-run',{'other':next(iter(self.endpoints.values()))})
        def reserving(db,*args):
            reserve(db,*args)
            futures.append(self.s.base.pool.submit(competing))
            self.assertTrue(entered.wait(10))
        with patch.object(offline_control,'reserve',side_effect=reserving):
            result,binding = self.s.reserve()
        with self.assertRaises(RegistryError) as caught:futures[0].result(10)
        self.assertEqual('PORT_RESERVED',caught.exception.code)
        self.assertTrue(all(v['run']==result['runId'] for v in self.ports()))
        self.assertEqual(self.s.saved()[0]['response'],binding())

    def test_nonoverlapping_other_instance_ports_are_preserved(self):
        self.competitor(free=True)
        original = self.ports()
        result,binding = self.s.reserve()
        self.assertEqual(original,[v for v in self.ports() if v['run']=='competing-run'])
        self.assertEqual(len(self.endpoints),len([v for v in self.ports() if v['run']==result['runId']]))
        binding()

    def test_orphan_same_instance_other_run_is_not_replaced_or_adopted(self):
        self.s.before_write(lambda:self.competitor(instance=self.s.token['instanceId'],run='orphan-run',free=True))
        self.assertEqual(['orphan-run'],[v['run'] for v in self.ports()])

    def test_final_copy_missing_port_refuses_return_and_preserves_reservations(self):
        self.s.final_copy(lambda:self.commit(lambda db:db.execute("DELETE FROM endpoint_reservations WHERE service=?",(next(iter(self.endpoints)),))))
        self.assertEqual(len(self.endpoints)-1,len(self.ports()))
        self.assertEqual('PENDING',self.s.saved()[0]['status'])

    def test_final_copy_changed_port_value_refuses_return(self):
        self.s.final_copy(lambda:self.commit(lambda db:db.execute('UPDATE endpoint_reservations SET port=port+1')))
        self.assertEqual(len(self.endpoints),len(self.ports()))

    def test_final_copy_other_run_row_refuses_return(self):
        self.s.final_copy(lambda:self.commit(lambda db:db.execute("UPDATE endpoint_reservations SET run='changed-run'")))

    def test_final_copy_other_instance_row_refuses_return(self):
        self.s.final_copy(lambda:self.commit(lambda db:db.execute("UPDATE endpoint_reservations SET instance='other-instance'")))

    def test_final_copy_extra_foreign_instance_row_for_same_run_refuses_return(self):
        def change():
            run = self.s.row()['run_id']
            value = dict(next(iter(self.endpoints.values())),port=max(v['port'] for v in self.endpoints.values())+10)
            self.commit(lambda db:runtime_ports.reserve(db,'other-instance',run,{'extra':value}))
        self.s.final_copy(change)
        self.assertEqual(len(self.endpoints)+1,len(self.ports()))

    def test_callback_after_return_rechecks_original_ports(self):
        _,binding = self.s.reserve()
        self.commit(lambda db:db.execute('DELETE FROM endpoint_reservations'))
        with self.assertRaises(RegistryError):binding()
        self.assertTrue(self.s.offline.starts[self.s.key]['returned'])
        self.assertEqual('PENDING',self.s.saved()[0]['status'])
        self.s.no_retry()

    def test_deadline_during_port_reservation_rolls_back_whole_transaction(self):
        reserve = offline_control.reserve
        def reserving(*args):
            reserve(*args)
            self.s.base.f.clock = self.s.base.f.deadline
        with patch.object(offline_control,'reserve',side_effect=reserving):self.s.rejected()
        self.assertEqual((None,[]),self.s.saved())
        self.assertEqual([],self.ports())
        self.assertFalse(self.s.offline.starts[self.s.key]['reserved'])
        self.s.no_retry()

    def test_port_values_changed_during_reservation_are_not_refixed(self):
        reserve = offline_control.reserve
        def reserving(db,instance,run,endpoints):
            next(iter(endpoints.values()))['port'] += 1
            reserve(db,instance,run,endpoints)
        with patch.object(offline_control,'reserve',side_effect=reserving):self.s.rejected()
        self.assertEqual((None,[]),self.s.saved())
        self.assertEqual([],self.ports())
        self.s.no_retry()

    def test_final_copy_port_values_are_not_readopted_from_mutable_argument(self):
        reserve = offline_control.reserve
        captured = []
        def reserving(db,instance,run,endpoints):
            captured.append(endpoints)
            reserve(db,instance,run,endpoints)
        with patch.object(offline_control,'reserve',side_effect=reserving):
            self.s.final_copy(lambda:next(iter(captured[0].values())).__setitem__('port',50001))
        self.assertEqual(len(self.endpoints),len(self.ports()))

    def test_final_copy_fixed_owner_change_refuses_return(self):
        self.s.final_copy(lambda:self.commit(lambda db:db.execute("UPDATE instances SET owner='changed-owner'")))

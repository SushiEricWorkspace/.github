"""assign/admitの操作全体deadlineを実Registryと決定的clockで検査する。"""

import unittest
from unittest.mock import patch

from . import offline_control
from . import test_offline_repeat as repeated
from . import test_offline_process as processes
from .registry import RegistryError


class OfflineDeadlineTests(unittest.TestCase):
    def setUp(self):
        f=repeated.OfflineRepeatTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f=f

    def boundary(self,stage,where):
        f=self.f
        if stage=='admit':f.call('assign')
        baseline=f.counts()
        clock=[10 if where=='expired' else 0]
        field,local=('assignment','assignedResponseSha256') if stage=='assign' else ('admission','candidateResponseSha256')
        peer=f.c if stage=='assign' else f.worker()
        checks=[]
        receive=processes.Pipe.receive
        method='_prepared' if stage=='assign' else '_assigned'
        original=getattr(f.runtime._offline,method)
        verify_peer=f.worker() if stage=='assign' else f.c
        original_verify=verify_peer.verify

        def received(*args):
            result=receive(*args)
            if where=='response':clock[0]=10
            return result

        def verified():
            original_verify()
            raw=f.c.assignment_response if stage=='assign' else f.worker().candidate_response
            if where=='checked' and raw is not None:clock[0]=10
            if where=='ack' and 'responseSha256' in f.saved().get('assignment',{}):clock[0]=10

        def transaction(*args):
            result=original(*args)
            ready=f.c.assignment_ready if stage=='assign' else f.worker().candidate_ready
            if where=='registry' and ready:
                checks.append(1)
                if len(checks)==2:clock[0]=10
            return result

        with patch.object(offline_control.time,'monotonic',lambda:clock[0]),patch.object(processes.Pipe,'receive',received),patch.object(
                verify_peer,'verify',verified),patch.object(f.runtime._offline,method,transaction):
            with self.assertRaises(RegistryError) as error:f.call(stage,deadline=10)
        self.assertEqual('STOP_TIMEOUT',error.exception.code)
        if where=='registry':self.assertEqual(2,len(checks))
        self.assertEqual(baseline[-1],f.c.pipe.acks)
        if stage=='assign':self.assertFalse(f.c.assignment_acknowledged)
        elif where in ('expired','response','checked'):self.assertFalse(f.worker().candidate_ready)
        if where in ('expired','response','checked'):
            self.assertNotIn(local,peer.record)
        if where=='ack':
            # 正本結果は期限内に記録済みなので保全し、期限後のACKだけを拒否する。
            self.assertIn('responseSha256',f.saved()[field])
            self.assertNotIn('assignedAcknowledgedSha256',f.c.record)
        else:
            self.assertNotIn('responseSha256' if stage=='assign' else 'candidateResponseSha256',f.saved()[field])
        if where=='expired':
            self.assertEqual(baseline,f.counts())
        else:
            raw=f.c.assignment_response if stage=='assign' else f.worker().candidate_response
            self.assertIsInstance(raw,bytes)
            self.assertEqual(1,f.effects['assigns'])
            for key in ('begins','dbOpens','edits','dbCloses','finishes'):
                self.assertEqual(int(stage=='admit'),f.effects[key])
            self.assertEqual(f.f.request,f.requests[0])
        preserved=f.counts()
        f.no_retry(stage)
        self.assertEqual(preserved,f.counts())
        self.assertEqual(3,len(f.f.f.p.adapter.processes))


def deadline_case(stage,where):
    def test(self):self.boundary(stage,where)
    return test


for stage in ('assign','admit'):
    for where in ('expired','response','checked','registry'):
        setattr(OfflineDeadlineTests,'test_'+stage+'_'+where+'_deadline',deadline_case(stage,where))
setattr(OfflineDeadlineTests,'test_assign_ack_deadline',deadline_case('assign','ack'))

"""同じ認証済み接続のHELLO/GRANTを合成Mod・実Registryで検証する。"""
from copy import deepcopy
import ctypes
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import mod_control_operations as operations, mod_control_transport, mod_control_wire as wire, offline_control
from . import test_offline_start_accept as acceptance
from .test_mod_control_wire_v3 import VECTORS, frame
from .registry import RegistryError


class StartGrantTests(unittest.TestCase):
    def setUp(self):
        self.f = acceptance.StartAcceptTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.b, self.runtime, self.native = self.f.b, self.f.runtime, self.f.native
        self.channel, self.starter = self.f.channel, self.f.starter
        self.native.chunk = 65536
        self.channel.incoming = self.channel.sent = b''
        self.hello = None
        self.change_hello = lambda value:None
        self.io_hook = lambda kind,stage,op:None
        def authenticated(stage, value):
            if stage != 'authenticated':return
            context = self.f.candidate()['context']
            descriptor = json.loads(context['descriptorJson'])
            message = wire.decode_frame_v3(bytes.fromhex(VECTORS['sessions'][0]['pairs'][0]['request']['frameHex']))
            evidence = json.loads(message['payload']['profileEvidenceJson'])
            evidence.update(deepcopy(context['expectedEvidence']), owner=context['owner'],
                descriptorSha256=wire.sha256(context['descriptorJson'].encode()),
                **{k:descriptor[k] for k in ('runtimeManifestHash','runtimeRunId','startNonce')})
            raw = json.dumps(evidence,ensure_ascii=False,separators=(',',':'))
            message['descriptorSha256'] = evidence['descriptorSha256']
            message['payload'].update(owner=deepcopy(context['owner']),profileEvidenceJson=raw,
                                      profileEvidenceSha256=wire.sha256(raw.encode()))
            self.change_hello(message)
            self.hello = self.channel.incoming = frame(message)
        self.f.observation_hook = authenticated
        make = self.native.operation.side_effect
        def operation(channel,kind,value):
            op = make(channel,kind,value)
            if kind != 'accept':
                op.overlapped = SimpleNamespace(hEvent=None)
                op.buffer = None
                op.result = op.before_issue = None
            start,finish,release = op.start,op.finish,op.release
            def issue(*,before_issue=None):
                if kind != 'accept':
                    op.overlapped.hEvent = 82
                    op.buffer = bytearray(value if kind=='write' else value)
                self.io_hook(kind,'prepared',op)
                return start(before_issue=before_issue)
            def finished(*args):
                result = finish(*args)
                if kind == 'read':op.buffer[:len(result)] = result
                if kind != 'accept':op.result = result
                self.io_hook(kind,'finished',op)
                return result
            def released():
                result = release()
                if kind != 'accept':op.overlapped.hEvent,op.buffer = None,None
                self.io_hook(kind,'released',op)
                return result
            op.start,op.finish,op.release = issue,finished,released
            self.io_hook(kind,'created',op)
            return op
        self.native.operation.side_effect = operation

    def grant(self):
        records = acceptance.preparation.creation.descriptors.records
        return self.runtime._offline_grant_start(self.b.token, operation_id=records.reservations.START,
            read_operation_id=records.reservations.readers.OPERATION,
            open_operation_id=self.b.base.o.base.opened['operationId'],ready=self.b.base.o.ready,
            deadline=self.b.base.f.deadline)

    def retained(self):
        self.f.retained()
        connection=self.f.connections[0]
        endpoint=connection.endpoint
        connection.reap()
        endpoint.reap()
        self.assertIs(endpoint.connection,connection)
        self.f.peer.close.assert_not_called()
        self.channel.close.assert_not_called()
        self.assertFalse(self.f.f.f.held()['returned'])
        before = len(self.native.operations),bytes(self.channel.sent)
        with self.assertRaises(RegistryError):self.grant()
        self.assertEqual(before,(len(self.native.operations),self.channel.sent))

    def fault(self, kind, stage, change):
        fired=[]
        def hook(actual,at,op):
            if (actual,at)==(kind,stage) and not fired:
                fired.append(True)
                change(op)
        self.io_hook=hook
        with self.assertRaises((RegistryError,OSError)):self.grant()
        self.assertEqual([True],fired)
        self.retained()

    def test_same_connection_records_hello_and_sends_exact_grant_once(self):
        result,(session,process,candidate,check)=self.grant()
        self.assertEqual('OFFLINE_START_GRANTED',result['status'])
        self.assertIs(candidate['connection'],session['endpoint'].connection)
        self.assertIs(process,self.f.f.f.m)
        self.assertIs(candidate['hello'],candidate['request'])
        self.assertEqual(self.hello,candidate['hello'])
        self.assertEqual(candidate['grant'],self.channel.sent)
        wire.correlate_grant_v3(self.hello,self.channel.sent,candidate['context'])
        self.assertEqual(result['helloBodySha256'],wire.sha256(self.hello[4:]))
        self.assertEqual(result['grantBodySha256'],wire.sha256(self.channel.sent[4:]))
        self.assertEqual(['accept','read','read','write'],self.native.started)
        self.assertTrue(all(op.released for op in self.native.operations))
        self.assertIsNone(candidate['connection'].pending)
        with self.runtime._transaction(self.b.token['instanceId'],read_only=True) as connection:
            self.assertEqual(self.channel.sent,operations.readback(connection,self.b.token['instanceId'],self.hello))
        self.assertIsInstance(check(),str)
        self.f.peer.close.assert_not_called()
        self.channel.close.assert_not_called()
        self.assertEqual('PENDING',self.b.saved()[0]['status'])

    def test_epoch_after_read_preparation_issues_zero_read(self):
        self.fault('read','prepared',lambda op:self.b.commit('epoch'))
        self.assertEqual(['accept'],self.native.started)

    def test_deadline_after_write_preparation_issues_zero_write(self):
        self.fault('write','prepared',lambda op:setattr(self.b.base.f,'clock',self.b.base.f.deadline))
        self.assertNotIn('write',self.native.started)

    def test_epoch_after_read_finish_prevents_grant(self):
        self.fault('read','finished',lambda op:self.b.commit('epoch'))
        self.assertEqual(b'',self.channel.sent)

    def test_epoch_after_write_finish_refuses_normal_return(self):
        self.fault('write','finished',lambda op:self.b.commit('epoch'))
        self.assertTrue(self.channel.sent)

    def test_deadline_after_read_finish_prevents_grant(self):
        self.fault('read','finished',lambda op:setattr(self.b.base.f,'clock',self.b.base.f.deadline))
        self.assertEqual(b'',self.channel.sent)

    def test_epoch_during_write_release_refuses_normal_return(self):
        self.fault('write','released',lambda op:self.b.commit('epoch'))
        self.assertEqual(self.f.candidate()['grant'],self.channel.sent)

    def test_peer_change_during_read_preparation_issues_zero_read(self):
        self.fault('read','prepared',lambda op:setattr(self.f.peer,'handle',999))
        self.assertEqual(['accept'],self.native.started)

    def test_write_buffer_change_after_preparation_issues_zero_write(self):
        self.fault('write','prepared',lambda op:op.buffer.__setitem__(0,op.buffer[0]^1))
        self.assertNotIn('write',self.native.started)

    def test_received_buffer_change_after_finish_prevents_grant(self):
        self.fault('read','finished',lambda op:op.buffer.__setitem__(0,op.buffer[0]^1))
        self.assertEqual(b'',self.channel.sent)

    def test_release_failure_keeps_pending_io_without_retry(self):
        def hook(kind,stage,op):
            if kind=='read' and stage=='finished':op.release=Mock(side_effect=OSError('合成event close失敗'))
        self.io_hook=hook
        with self.assertRaises(OSError):self.grant()
        connection=self.f.candidate()['connection']
        self.assertIs(connection.pending,self.native.operations[-1])
        self.assertTrue(connection.close_unknown)
        connection.pending.release.assert_called_once()
        self.retained()
        connection.pending.release.assert_called_once()

    def test_public_renew_during_write_preparation_preserves_deadline(self):
        def hook(kind,stage,op):
            if (kind,stage)==('write','prepared'):
                self.b.base.pool.submit(self.runtime.registry.execute,'renew',dict(self.b.token,ttlSeconds=600),
                                       operation_id='grant-renew').result(10)
        self.io_hook=hook
        _,(_,_,_,check)=self.grant()
        check()
        self.assertEqual(self.b.base.f.deadline,self.f.f.f.held()['deadline'])

    def windows_preparation(self,kind,change):
        class Overlapped(ctypes.Structure):
            _fields_=[('hEvent',ctypes.c_void_p)]
        kernel=Mock()
        kernel.CreateEventW.side_effect=lambda *args:(change(),82)[1]
        backend=SimpleNamespace(Overlapped=Overlapped,kernel=kernel,close_handle=Mock(),
                                check=lambda value:self.assertTrue(value))
        make=self.native.operation.side_effect
        def operation(channel,actual,value):
            if actual!=kind:return make(channel,actual,value)
            op=mod_control_transport._WinOperation(backend,channel,actual,value)
            self.native.operations.append(op)
            return op
        self.native.operation.side_effect=operation
        with self.assertRaises((RegistryError,OSError)):self.grant()
        kernel.CreateEventW.assert_called_once()
        kernel.ReadFile.assert_not_called()
        kernel.WriteFile.assert_not_called()
        backend.close_handle.assert_called_once_with(82)
        self.retained()

    def test_actual_windows_read_event_preparation_epoch_issues_zero_read(self):
        self.windows_preparation('read',lambda:self.b.commit('epoch'))

    def test_actual_windows_write_buffer_preparation_deadline_issues_zero_write(self):
        self.windows_preparation('write',lambda:setattr(self.b.base.f,'clock',self.b.base.f.deadline))

    def mac_preparation(self,kind,change):
        stream=SimpleNamespace(fileno=lambda:77,recv=Mock(return_value=b'\0\0\0\0'),send=Mock(return_value=1))
        self.channel.stream=stream
        make=self.native.operation.side_effect
        def operation(channel,actual,value):
            if actual!=kind:return make(channel,actual,value)
            op=mod_control_transport._MacOperation(channel,actual,value)
            start=op.start
            def prepared(**kwargs):
                change()
                return start(**kwargs)
            op.start=prepared
            self.native.operations.append(op)
            return op
        self.native.operation.side_effect=operation
        with self.assertRaises((RegistryError,OSError)):self.grant()
        stream.recv.assert_not_called()
        stream.send.assert_not_called()
        self.retained()

    def test_actual_macos_read_poll_epoch_issues_zero_recv(self):
        self.mac_preparation('read',lambda:self.b.commit('epoch'))

    def test_actual_macos_write_poll_deadline_issues_zero_send(self):
        self.mac_preparation('write',lambda:setattr(self.b.base.f,'clock',self.b.base.f.deadline))

    def test_approval_change_inside_save_write_rolls_back_grant(self):
        complete=operations.complete
        def changed(*args):
            result=complete(*args)
            self.f.managed.sessions[self.b.token['instanceId']]['approval']['expectedEvidence']['profileSha256']='e'*64
            return result
        with patch.object(operations,'complete',side_effect=changed):
            with self.assertRaises(RegistryError):self.grant()
        with self.runtime._transaction(self.b.token['instanceId'],read_only=True) as connection:
            self.assertIsNone(operations.readback(connection,self.b.token['instanceId'],self.hello))
        self.assertEqual(b'',self.channel.sent)
        self.retained()

    def test_saved_grant_change_during_write_preparation_issues_zero_write(self):
        def change(op):
            def commit():
                with self.runtime.registry.transaction() as connection:
                    connection.execute("UPDATE runtime_operations SET response='{}' WHERE command='mod-control/HELLO'")
            self.b.base.pool.submit(commit).result(10)
        self.fault('write','prepared',change)
        self.assertNotIn('write',self.native.started)

    def test_final_copy_grant_packet_replacement_refuses_return(self):
        def change():
            held=self.f.f.f.held()
            held['granted']=tuple(list(held['granted']))
        self.final_copy(change)

    def test_partial_send_failure_keeps_raws_and_original_connection(self):
        self.native.chunk=256
        def hook(kind,stage,op):
            if kind=='write' and stage=='prepared' and self.channel.sent:raise OSError('合成送信の続行失敗')
        self.io_hook=hook
        with self.assertRaises(OSError):self.grant()
        self.assertTrue(self.channel.sent)
        self.assertLess(len(self.channel.sent),len(self.f.candidate()['grant']))
        self.retained()

    def test_bad_hello_owner_has_no_saved_or_sent_grant(self):
        self.change_hello=lambda message:message['payload']['owner'].update(startId='other')
        with self.assertRaises((RegistryError,ValueError)):self.grant()
        self.assertEqual(b'',self.channel.sent)
        self.retained()

    def final_copy(self,change):
        copy=offline_control.deepcopy
        def mutate(value):
            result=copy(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_START_GRANTED':change()
            return result
        with patch.object(offline_control,'deepcopy',side_effect=mutate):
            with self.assertRaises(RegistryError):self.grant()
        self.retained()

    def test_final_copy_original_hello_change_refuses_return(self):
        self.final_copy(lambda:self.f.candidate().update(hello=b'changed'))

    def test_final_copy_original_peer_change_refuses_return(self):
        self.final_copy(lambda:setattr(self.f.peer,'handle',999))

    def test_final_copy_io_origin_change_refuses_return(self):
        self.final_copy(lambda:setattr(self.native.operations[-1],'value',b'changed'))

    def test_final_copy_grant_raw_change_refuses_return(self):
        self.final_copy(lambda:self.f.candidate().update(grant=b'changed'))

    def test_final_copy_io_release_change_refuses_return(self):
        self.final_copy(lambda:setattr(self.native.operations[-1],'released',False))

    def test_final_copy_deadline_refuses_return(self):
        self.final_copy(lambda:setattr(self.b.base.f,'clock',self.b.base.f.deadline))

    def test_final_copy_native_result_change_refuses_return(self):
        self.final_copy(lambda:setattr(self.native.operations[-1],'result',999))

    def test_final_copy_native_callback_change_refuses_return(self):
        self.final_copy(lambda:setattr(self.native.operations[-1],'before_issue',lambda:None))

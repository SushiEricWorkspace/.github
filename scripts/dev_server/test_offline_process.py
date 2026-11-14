"""C/W起動のnative/stdio境界だけを合成する。実Data遷移はJava vectorsで検証する。"""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from . import offline_process as target
from . import runtime_process
from .registry import RegistryError

WORLD = '00000000-0000-4000-8000-000000000001'
RUN = '00000000-0000-4000-8000-000000000002'


def frame(worker=False):
    value = dict(operationId='00000000-0000-4000-8000-000000000003', requestHash='2' * 64,
                 leaseId=WORLD, epoch=1, generation=WORLD, drainSha256='a' * 64,
                 worldRoot=str(Path('fixture-world').absolute()), shopDirectory=str(Path('fixture-shop').absolute()),
                 worldUuid=WORLD, runUuid=RUN if worker else WORLD, expectedStateSha256=('b' if worker else 'a') * 64,
                 expectedRevision=1 if worker else 0)
    if worker:
        value['request'] = dict(requestId=value['operationId'], worldUuid=WORLD, action=dict(
            type='io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterAction.Edit',
            products=[dict(type='io.github.sushiericworkspace.sushiericdatamodel.database.host.HostProductChange.Save',
                           id='tab.apple', expectedHash=None, definition=dict(itemInternalId='minecraft:stone', purchasePrice=123),
                           stock=dict(type='io.github.sushiericworkspace.sushiericdatamodel.database.host.HostStockEdit.Register',
                                      quantity=7, revision=None))]))
    return value


class Adapter:
    def __init__(self):
        self.parent = dict(pid=os.getpid(), owner='fixture-owner', startId='parent-start', bootId='boot',
                           cwd='parent', executable='python', commandHash='parent')
        self.processes = []
        self.mismatch = None

    def identity(self, pid):
        if pid == os.getpid():
            return deepcopy(self.parent)
        return deepcopy(next(p.identity for p in self.processes if p.pid == pid))

    def OwnedProcess(self):
        process = Process(self)
        self.processes.append(process)
        return process


class Process:
    def __init__(self, adapter):
        self.adapter = adapter
        self.pid = self.group = self.identity = None
        self.stage = 'NOT_CREATED'
        self.starts = 0
        self.code = None
        self.extra = []

    def start(self, arguments, cwd, log, name, created, **stdio):
        if stdio.get('before_create') is not None:
            stdio.pop('before_create')()
        self.starts += 1
        self.pid = 100000 + self.adapter.processes.index(self)
        self.group = name
        self.arguments = deepcopy(arguments)
        self.stdio = stdio
        self.identity = dict(pid=self.pid, startId=str(self.pid), bootId='boot', owner='fixture-owner',
                             cwd=str(cwd.resolve()), executable=str(Path(arguments[0]).resolve()),
                             commandHash=runtime_process.command_hash(arguments))
        if self.adapter.mismatch:
            self.identity[self.adapter.mismatch] = 'different'
        self.stage = 'CREATED'
        created()
        self.stage = 'RUNNING'

    def poll(self):
        return self.code

    def members(self):
        return [self.pid, *self.extra]


class Native:
    transport = 'windows-pipe'
    def __init__(self):
        self.host = 'a' * 64
        self.boot = 'boot'
    def host_boot(self):
        return self.host, self.boot


class Pipe:
    current = None
    def __init__(self):
        self.child_input = 101 + len(self.current.pipes) * 2
        self.child_output = self.child_input + 1
        self.input, self.output = self.child_input + 1000, self.child_output + 1000
        self.sent = []
        self.effects = 0
        self.current.pipes.append(self)

    def close_child_ends(self):
        self.child_input = self.child_output = None

    def close_after_exit(self, verify=None):
        self.close_child_ends()
        for name in ('input', 'output'):
            if getattr(self, name) is not None:
                if verify is not None:
                    verify()
                setattr(self, name, None)

    def send(self, request, deadline, verify):
        verify()
        self.sent.append(deepcopy(request))
        if self.current.failure == 'partial':
            raise TimeoutError('fixture 送信途中')
        if request['command'] == 'open':
            self.effects += 1
        self.current.after_send()

    def receive(self, deadline, verify):
        verify()
        if self.current.failure == 'lost':
            raise EOFError('fixture 作用後応答喪失')
        request = self.sent[-1]
        response = {k: request[k] for k in ('operationId', 'requestHash', 'sessionId', 'nonce')}
        response.update(status='OPEN' if request['command'] == 'open' else 'PREPARED',
                        stateSha256='b' * 64, stateRevision=1, runUuid=RUN)
        if self.current.response_change:
            field, value = self.current.response_change
            response[field] = value
        return response


class OfflineProcessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        self.adapter = Adapter()
        self.native = Native()
        self.valid = True
        self.pipes = []
        self.failure = self.response_change = None
        self.after_send = lambda: None
        Pipe.current = self
        for name, value in [('ProxyPipe', Pipe), ('make_private', lambda *a, **k: None),
                            ('offline_controller_command', lambda root: [str(root / 'java'), 'fixed-C']),
                            ('offline_worker_command', lambda root: [str(root / 'java'), 'fixed-W'])]:
            self.enterContext(patch.object(target, name, value))
        self.enterContext(patch.dict(os.environ, {k: '' for k in ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'CLASSPATH')}))

    def verify(self):
        if not self.valid:
            raise RegistryError('STALE_WRITER', 'fixture 受付binding失効')

    def helper(self, controller=None):
        return target.OfflineProcess(self.bundle, self.root / ('diag-' + str(len(self.adapter.processes))),
            native=self.native, verify=self.verify, controller=controller, process_adapter=self.adapter)

    def start(self, helper, worker=False):
        return helper.start(frame(worker), time.monotonic() + 1)

    def pair(self):
        c = self.helper()
        self.start(c)
        w = self.helper(c)
        return c, w

    def no_retry(self, helper, worker=False):
        counts = [(p.starts, len(pipe.sent), pipe.effects) for p, pipe in zip(self.adapter.processes, self.pipes)]
        with self.assertRaises(RegistryError):
            self.start(helper, worker)
        self.assertEqual(counts, [(p.starts, len(pipe.sent), pipe.effects) for p, pipe in zip(self.adapter.processes, self.pipes)])
        self.assertFalse(helper.ready)

    def test_sibling_processes_native_binding_and_nonempty_request_are_preserved(self):
        c, w = self.pair()
        expected = frame(True)
        response = w.start(expected, time.monotonic() + 1)
        self.assertEqual('PREPARED', response['status'])
        self.assertEqual(expected['request'], w.pipe.sent[0]['request'])
        self.assertEqual(1, len(w.pipe.sent[0]['request']['action']['products']))
        self.assertEqual(c.data_identity, w.pipe.sent[0]['owner'])
        self.assertEqual(w.data_identity, w.pipe.sent[0]['worker'])
        self.assertEqual(c.parent_data, w.pipe.sent[0]['controller'])
        self.assertEqual((c.session, c.nonce), (w.session, w.nonce))
        self.assertNotEqual(c.process.group, w.process.group)
        self.assertNotEqual(c.process.stdio, w.process.stdio)
        self.assertEqual(['fixed-C', 'fixed-W'], [p.arguments[-1] for p in self.adapter.processes])
        self.assertEqual([1, 0], [p.effects for p in self.pipes])
        self.assertNotIn('owner', expected)
        response['status'] = 'caller-change'
        self.assertEqual('PREPARED', json.loads(w.response)['status'])
        records = [json.loads(p.read_bytes()) for p in sorted(w.diagnostics.glob('creation-*.json'))]
        self.assertEqual('CREATED', records[1]['creation']['stage'])
        self.assertEqual(w.data_identity, records[2]['controlIdentity'])
        self.assertEqual('RUNNING', records[3]['creation']['stage'])
        journal = ''.join(p.read_text() for p in w.diagnostics.glob('creation-*.json'))
        for secret in (w.session, w.nonce, 'tab.apple', 'purchasePrice'):
            self.assertNotIn(secret, journal)
            self.assertNotIn(secret, ' '.join(w.process.arguments))

    def test_gate_and_implicit_jvm_inputs_reject_before_process_effect(self):
        self.valid = False
        c = self.helper()
        with self.assertRaises(RegistryError): self.start(c)
        self.assertEqual(0, c.process.starts)
        self.valid = True
        for key in ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'CLASSPATH'):
            with self.subTest(key=key), patch.dict(os.environ, {key: 'implicit'}):
                c = self.helper()
                with self.assertRaises(RegistryError): self.start(c)
                self.assertEqual(0, c.process.starts)

    def test_caller_cannot_replace_private_identities_or_binding(self):
        for key in ('command', 'controller', 'owner', 'worker', 'sessionId', 'nonce'):
            with self.subTest(key=key):
                c = self.helper()
                with self.assertRaises(RegistryError): c.start(frame() | {key: 'forged'}, time.monotonic() + 1)
                self.assertEqual(0, c.process.starts)

    def test_created_identity_mismatches_reject_before_frame_effect(self):
        for field in ('owner', 'cwd', 'executable', 'commandHash'):
            with self.subTest(field=field):
                self.adapter.mismatch = field
                c = self.helper()
                with self.assertRaises(RegistryError): self.start(c)
                self.assertEqual([], c.pipe.sent)
                self.assertEqual(0, c.pipe.effects)
                self.no_retry(c)

    def test_creation_journal_failure_preserves_process_and_never_sends(self):
        c = self.helper()
        original = c._record
        def record():
            if c.process.stage == 'CREATED': raise OSError('fixture 生成後記録失敗')
            original()
        with patch.object(c, '_record', record):
            with self.assertRaises(OSError): self.start(c)
        self.assertEqual('CREATED', c.process.stage)
        self.assertEqual([], c.pipe.sent)
        self.no_retry(c)

    def test_partial_send_and_post_effect_response_loss_do_not_retry(self):
        for failure in ('partial', 'lost'):
            with self.subTest(failure=failure):
                self.failure = failure
                c = self.helper()
                with self.assertRaises((TimeoutError, EOFError)): self.start(c)
                self.assertEqual(1 if failure == 'lost' else 0, c.pipe.effects)
                self.assertEqual(1, len(c.pipe.sent))
                self.assertIsNotNone(c.request)
                self.no_retry(c)

    def test_each_response_binding_mismatch_rejects_after_one_effect(self):
        for field in ('operationId', 'requestHash', 'sessionId', 'nonce', 'status', 'extra', 'runUuid', 'stateSha256', 'stateRevision'):
            with self.subTest(field=field):
                self.response_change = field, 'wrong'
                c = self.helper()
                with self.assertRaises((RegistryError, ValueError)): self.start(c)
                self.assertEqual(1, c.pipe.effects)
                self.assertIsNotNone(c.response)
                self.no_retry(c)

    def test_prepared_state_mismatches_are_rejected_individually(self):
        for field, value in (('stateSha256', 'c' * 64), ('stateRevision', 2), ('runUuid', WORLD)):
            with self.subTest(field=field):
                self.response_change = None
                c, w = self.pair()
                self.response_change = field, value
                with self.assertRaises(RegistryError): self.start(w, True)
                self.assertEqual(0, w.pipe.effects)
                self.no_retry(w, True)

    def test_parent_and_c_death_or_native_identity_change_prevent_worker_send(self):
        for change in ('parent', 'parent-boot', 'c-start', 'c-exit'):
            with self.subTest(change=change):
                c, w = self.pair()
                if change == 'parent': self.adapter.parent['startId'] += 'old'
                elif change == 'parent-boot': self.native.boot += 'old'
                elif change == 'c-start': c.process.identity['startId'] += 'old'
                else: c.process.code = 0
                with self.assertRaises(RegistryError): self.start(w, True)
                self.assertEqual(0, w.process.starts)

    def test_shared_group_or_cross_membership_rejects_before_worker_effect(self):
        for change in ('group', 'w-in-c', 'c-in-w', 'missing-self'):
            with self.subTest(change=change):
                c, w = self.pair()
                start = w.process.start
                def grouped(*args, **kwargs):
                    start(*args, **kwargs)
                    if change == 'group': w.process.group = c.process.group
                    elif change == 'w-in-c': c.process.extra = [w.process.pid]
                    elif change == 'c-in-w': w.process.extra = [c.process.pid]
                    else: w.process.members = lambda: []
                with patch.object(w.process, 'start', grouped):
                    with self.assertRaises(RegistryError): self.start(w, True)
                self.assertEqual([], w.pipe.sent)
                self.assertEqual(0, w.pipe.effects)
                self.no_retry(w, True)

    def test_post_send_identity_failure_and_post_response_journal_failure_remain_unknown(self):
        c = self.helper()
        self.after_send = lambda: setattr(self.native, 'boot', 'changed')
        with self.assertRaises(RegistryError): self.start(c)
        self.assertEqual(1, c.pipe.effects)
        self.no_retry(c)
        self.after_send = lambda: None
        c = self.helper()
        original = c._record
        def record():
            if c.response is not None: raise OSError('fixture 応答後記録失敗')
            original()
        with patch.object(c, '_record', record):
            with self.assertRaises(OSError): self.start(c)
        self.assertEqual(1, c.pipe.effects)
        self.assertIsNotNone(c.response)
        self.no_retry(c)

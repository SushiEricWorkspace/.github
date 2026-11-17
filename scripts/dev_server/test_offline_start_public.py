"""独立Readyの公開受付から元Mの監督・停止までを合成通信で検証する。"""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import importlib.util
import builtins
import io
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import textwrap
import threading
import unittest
from unittest.mock import Mock, patch

from . import runtime_control, supervisor
from .local_ipc import encode, MAX_MESSAGE
from .registry import RegistryError
from . import test_supervisor as registry_tests
from . import test_offline_start_receiver as receivers
from . import test_offline_start_proof as proofs


class ObservedLock:
    def __init__(self, lock, arrived):
        self.lock, self.arrived = lock, arrived
        self.owner = threading.get_ident()
        self.armed = False

    def __enter__(self):
        if self.armed and threading.get_ident() == self.owner:
            self.armed = False
            self.arrived.set()
        return self.lock.__enter__()

    def __exit__(self, *error):
        return self.lock.__exit__(*error)


def cli(registry, payload, operation='public-start', options=()):
    script = Path(__file__).parents[1] / 'dev-server.py'
    spec = importlib.util.spec_from_file_location('offline_start_cli', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    arguments = [str(script), 'offline-start', '--root', str(registry.root), '--operation-id', operation,
                 '--payload-stdin', *options]
    output = io.StringIO()
    def request(root, message, *, timeout):
        assert root == registry.root
        return supervisor.dispatch(registry, message)
    with patch('sys.argv', arguments), patch('sys.stdin', SimpleNamespace(buffer=io.BytesIO(encode(payload)))), \
            patch.object(module, 'request', side_effect=request), redirect_stdout(output):
        code = module.main()
    response = json.loads(output.getvalue())
    assert code == (0 if response['ok'] else 1)
    return response


class PublicInputTests(unittest.TestCase):
    def setUp(self):
        self.f = registry_tests.RegistryTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.registry = self.f.registry
        token = dict(self.f.acquire(), expectedGeneration='00000000-0000-0000-0000-000000000001')
        self.payload = dict(token, timeoutSeconds=10, openOperationId='closed-source', readOperationId='new-read',
            ready=dict(worldUuid='00000000-0000-0000-0000-000000000002',
                runUuid='00000000-0000-0000-0000-000000000003', stateSha256='a'*64, stateRevision=6))

    def rejected(self, payload, *, dry_run=False):
        with patch.object(self.registry, 'transaction', wraps=self.registry.transaction) as sql, \
                patch.object(self.registry.runtime, '_offline_finish_start') as finish:
            with self.assertRaises(RegistryError) as caught:
                self.registry.execute('offline-start', payload, operation_id='public-start', dry_run=dry_run)
            self.assertIn(caught.exception.code, ('INVALID_INPUT', 'OPERATION_CONFLICT', 'START_TIMEOUT'))
            sql.assert_not_called()
            finish.assert_not_called()

    def test_ready_is_mandatory_and_not_inferred_from_token(self):
        value = deepcopy(self.payload)
        value.pop('ready')
        self.rejected(value)

    def test_unknown_public_field_is_rejected(self):
        self.rejected(dict(self.payload, approved=True))

    def test_dry_run_does_not_claim_official_validation_without_reader(self):
        self.rejected(self.payload, dry_run=True)

    def test_operation_roles_must_be_distinct(self):
        self.rejected(dict(self.payload, readOperationId='public-start'))

    def test_ready_unknown_field_is_rejected(self):
        value = deepcopy(self.payload)
        value['ready']['generation'] = self.payload['expectedGeneration']
        self.rejected(value)

    def test_invalid_ready_formats_are_rejected_before_io(self):
        for key, value in (('worldUuid','bad'), ('runUuid',4), ('stateSha256','A'*64),
                           ('stateRevision',True), ('stateRevision',6.0), ('stateRevision',-1),
                           ('stateRevision',2**63)):
            with self.subTest(key=key, value=value):
                payload = deepcopy(self.payload)
                payload['ready'][key] = value
                self.rejected(payload)

    def test_invalid_timeout_and_token_types_are_rejected_before_io(self):
        for key, value in (('timeoutSeconds',True), ('timeoutSeconds',1.0), ('timeoutSeconds',0),
                           ('epoch',True), ('leaseId',None), ('expectedGeneration',None)):
            with self.subTest(key=key):
                self.rejected(dict(self.payload, **{key:value}))

    def test_snapshot_is_validated_after_caller_clear(self):
        copy = runtime_control.deepcopy
        def changed(value):
            value['ready'].clear()
            return copy(value)
        with patch.object(runtime_control, 'deepcopy', side_effect=changed):
            self.rejected(self.payload)

    def test_snapshot_float_revision_is_rejected(self):
        copy = runtime_control.deepcopy
        def changed(value):
            value['ready']['stateRevision'] = 6.0
            return copy(value)
        with patch.object(runtime_control, 'deepcopy', side_effect=changed):
            self.rejected(self.payload)

    def test_input_copy_time_is_part_of_original_deadline(self):
        clock = [0.0]
        copy = runtime_control.deepcopy
        def changed(value):
            result = copy(value)
            clock[0] = 10.0
            return result
        with patch.object(runtime_control, 'time', SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(runtime_control, 'deepcopy', side_effect=changed):
            with self.assertRaises(RegistryError) as error:
                self.registry.runtime._offline_start_input(self.payload, 'public-start', False)
            self.assertEqual('START_TIMEOUT', error.exception.code)

    def test_input_snapshot_detaches_from_original_caller(self):
        expected = deepcopy(self.payload)
        packet = self.registry.runtime._offline_start_input(self.payload, 'public-start', False)
        self.payload['ready'].clear()
        self.assertEqual(expected, packet[0])

    def test_validator_import_time_is_part_of_original_deadline(self):
        clock = [0.0]
        original = builtins.__import__
        def imported(name, globals=None, locals=None, fromlist=(), level=0):
            result = original(name, globals, locals, fromlist, level)
            if name == 'offline_control' and '_check_ready' in fromlist:
                clock[0] = 10.0
            return result
        with patch.object(runtime_control, 'time', SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(builtins, '__import__', side_effect=imported):
            with self.assertRaises(RegistryError) as error:
                self.registry.runtime._offline_start_input(self.payload, 'public-start', False)
            self.assertEqual('START_TIMEOUT', error.exception.code)

    def test_float_revision_is_rejected_at_public_input(self):
        self.payload['ready']['stateRevision'] = 6.0
        with self.assertRaises(RegistryError) as error:
            self.registry.runtime._offline_start_input(self.payload, 'public-start', False)
        self.assertEqual('INVALID_INPUT', error.exception.code)

    def test_cli_missing_operation_id_is_rejected_before_request(self):
        script = Path(__file__).parents[1] / 'dev-server.py'
        spec = importlib.util.spec_from_file_location('operation_cli', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        arguments = [str(script), 'offline-start', '--root', str(self.registry.root), '--payload-stdin']
        output = io.StringIO()
        with patch('sys.argv', arguments), patch('sys.stdin', SimpleNamespace(buffer=io.BytesIO(encode(self.payload)))), \
                patch.object(module, 'request', return_value=dict(ok=True, result={})) as request, redirect_stdout(output):
            self.assertEqual(1, module.main())
        request.assert_not_called()

    def test_cli_preserves_explicit_ready_and_rejects_missing_terminal(self):
        response = cli(self.registry, self.payload)
        self.assertFalse(response['ok'])
        self.assertEqual('PROOF_REQUIRED', response['error']['code'])

    def test_cli_payload_file_read_is_bounded_on_actual_stream(self):
        script = Path(__file__).parents[1] / 'dev-server.py'
        spec = importlib.util.spec_from_file_location('bounded_cli', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = io.BytesIO(b' '*(MAX_MESSAGE+100))
        read = Mock(wraps=source.read)
        source.read = read
        output = io.StringIO()
        arguments = [str(script), 'offline-start', '--root', str(self.registry.root),
                     '--operation-id', 'public-start', '--payload-file', 'synthetic-input.json']
        with patch('sys.argv', arguments), patch.object(Path, 'open', return_value=source), \
                patch.object(module, 'request') as request, redirect_stdout(output):
            self.assertEqual(1, module.main())
        read.assert_called_once_with(MAX_MESSAGE+1)
        request.assert_not_called()

    def test_resource_entry_expiry_starts_no_gc_read(self):
        clock = [0.0]
        locked, arrived = threading.Event(), threading.Event()
        proxy = ObservedLock(self.registry.db_lock, arrived)
        transaction = self.registry.transaction
        prepare = self.registry.runtime._offline_start_input
        errors, sql, deadlines = [], [], []

        def hold():
            try:
                with transaction(read_only=True) as db:
                    db.execute('SELECT 1').fetchone()
                    locked.set()
                    self.assertTrue(arrived.wait(5))
                    clock[0] = deadlines[0]
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=hold)
        def snapshot(*args):
            result = prepare(*args)
            deadlines.append(result[1])
            worker.start()
            self.assertTrue(locked.wait(5))
            proxy.armed = True
            return result

        @contextmanager
        def tracked(*args, **kwargs):
            if threading.get_ident() == proxy.owner:
                sql.append(clock[0])
            with transaction(*args, **kwargs) as db:
                yield db

        with patch.object(runtime_control, 'time', SimpleNamespace(monotonic=lambda:clock[0])), \
                patch.object(self.registry, 'db_lock', proxy), \
                patch.object(self.registry, 'transaction', tracked), \
                patch.object(self.registry.runtime, '_offline_start_input', side_effect=snapshot), \
                patch.object(self.registry.runtime, '_offline_finish_start') as finish:
            try:
                with self.assertRaises(RegistryError) as caught:
                    self.registry.execute('offline-start', self.payload, operation_id='public-start')
                self.assertEqual('START_TIMEOUT', caught.exception.code)
            finally:
                arrived.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertEqual([10.0], deadlines)
            self.assertEqual([], sql)
            finish.assert_not_called()
        self.assertEqual(0, self.registry.resource_users)
        self.assertEqual({}, self.registry.runtime._offline_requests)


class PublicStartTests(unittest.TestCase):
    def setUp(self):
        self.f = receivers.StartReceiverTests()
        self.addCleanup(lambda: self.assertTrue(self.f.doCleanups()))
        self.f.setUp()
        self.addCleanup(self.f.finish_receiver)
        self.runtime, self.b = self.f.runtime, self.f.b
        self.core = self.f.f.f
        with patch.object(self.runtime, '_offline_record_start_proof') as probe:
            self.core.record()
        arguments = probe.call_args.kwargs
        self.operation = arguments['operation_id']
        remaining = arguments['deadline'] - self.b.base.f.clock
        self.assertEqual(int(remaining), remaining)
        self.payload = dict(self.b.token, ready=deepcopy(arguments['ready']),
            timeoutSeconds=int(remaining), openOperationId=arguments['open_operation_id'],
            readOperationId=arguments['read_operation_id'])
        self.direct = False
        self.f.f.record = self.record

    def call(self):
        if not self.direct:
            return cli(self.runtime.registry, self.payload, self.operation)
        try:
            result = self.runtime.registry.execute('offline-start', self.payload, operation_id=self.operation)
            return dict(ok=True, result=result)
        except RegistryError as error:
            return dict(ok=False, error=dict(code=error.code, message=str(error)))

    def record(self):
        response = self.call()
        if not response['ok']:
            raise RegistryError(response['error']['code'], response['error']['message'])
        result = response['result']
        instance = self.b.token['instanceId']
        return result, (self.runtime.managed.sessions[instance], self.runtime.processes[result['runId']],
                        self.runtime.managed.startup.sessions[instance], None)

    def test_cli_start_then_public_status_and_refresh(self):
        receivers.StartReceiverTests.test_public_status_and_refresh_consume_same_running_session(self.f)

    def test_cli_start_then_public_stop_completes_via_real_event(self):
        receivers.StartReceiverTests.test_public_stop_waits_for_existing_serve_done_and_completes(self.f)

    def test_cli_start_then_stopping_proof_supervision(self):
        receivers.StartReceiverTests.test_stopping_proof_enters_existing_running_supervision(self.f)

    def test_cli_start_then_stopping_refresh(self):
        receivers.StartReceiverTests.test_refresh_preserves_legitimate_stopping_proof(self.f)

    def test_direct_api_keeps_snapshot_after_original_caller_change(self):
        self.direct = True
        prepare = self.runtime._offline_start_input
        def changed(*args):
            packet = prepare(*args)
            self.payload['ready'].clear()
            return packet
        with patch.object(self.runtime, '_offline_start_input', side_effect=changed):
            self.f.start()

    def test_old_approval_alone_still_cannot_use_normal_start(self):
        with self.assertRaises(RegistryError) as caught:
            self.runtime.registry.execute('start', dict(self.b.token, timeoutSeconds=10), operation_id='legacy-start')
        self.assertEqual('CONTROL_BLOCKED', caught.exception.code)
        self.assertEqual(0, self.core.creator.generated)

    def rejected_after_finish(self, change, *, substitute=False):
        original = self.runtime._offline_finish_start
        def finished(*args, **kwargs):
            result, packet = original(*args, **kwargs)
            self.assertFalse(self.f.entered.is_set())
            change()
            return (dict(result) if substitute else result), packet
        with patch.object(self.runtime, '_offline_finish_start', side_effect=finished):
            response = self.call()
        self.assertFalse(response['ok'])
        held = self.runtime._offline_requests[(self.b.token['instanceId'], self.operation)]
        self.assertFalse(held['returned'])
        self.assertIsNotNone(held['input'][2])
        original_slot = self.core.held()
        self.assertIs(original_slot['accepted'][0], original_slot['startup'][0]['endpoint'].connection)
        self.core.creator.m.close.assert_not_called()
        original_slot['accepted'][0].peer.close.assert_not_called()
        original_slot['accepted'][0].channel.close.assert_not_called()
        self.assertIsNotNone(self.b.base.p.reader.request)
        effects = self.core.creator.generated, bytes(self.core.f.f.channel.sent)
        self.assertFalse(self.call()['ok'])
        self.assertEqual(effects, (self.core.creator.generated, bytes(self.core.f.f.channel.sent)))

    def test_outer_current_mutation_refuses_public_success(self):
        self.rejected_after_finish(lambda: self.core.f.original.current_path.write_bytes(b'{}'))

    def test_outer_proof_mutation_refuses_public_success(self):
        self.rejected_after_finish(lambda: Path(self.core.held()['creation_run']['proofPath']).write_bytes(b'{}'))

    def test_outer_deadline_refuses_public_success(self):
        self.rejected_after_finish(lambda: setattr(self.b.base.f, 'clock', self.b.base.f.deadline))

    def test_outer_input_mutation_refuses_public_success(self):
        def change():
            held = self.runtime._offline_requests[(self.b.token['instanceId'], self.operation)]
            held['input'][0]['ready'].clear()
        self.rejected_after_finish(change)

    def test_outer_packet_replacement_is_not_rebased(self):
        def change():
            self.runtime._offline_requests[(self.b.token['instanceId'], self.operation)]['packet'] = object()
        self.rejected_after_finish(change)

    def test_substituted_equal_result_object_is_not_adopted(self):
        self.rejected_after_finish(lambda: None, substitute=True)

    def test_receive_waits_until_public_completion(self):
        original = self.runtime._offline_finish_start
        def finished(*args, **kwargs):
            result = original(*args, **kwargs)
            self.assertFalse(self.f.entered.wait(0.25))
            return result
        with patch.object(self.runtime, '_offline_finish_start', side_effect=finished):
            self.f.start()

    def test_after_outer_callback_input_mutation_refuses_public_success(self):
        original = self.runtime._offline_finish_start
        def finished(*args, **kwargs):
            result, packet = original(*args, **kwargs)
            def verify(**inputs):
                value = packet[3](**inputs)
                held = self.runtime._offline_requests[(self.b.token['instanceId'], self.operation)]
                held['input'][0]['ready'].clear()
                return value
            return result, (*packet[:3], verify)
        with patch.object(self.runtime, '_offline_finish_start', side_effect=finished):
            response = self.call()
        self.assertFalse(response['ok'])
        held = self.runtime._offline_requests[(self.b.token['instanceId'], self.operation)]
        self.assertFalse(held['returned'])
        self.core.creator.m.close.assert_not_called()
        self.core.held()['accepted'][0].peer.close.assert_not_called()
        self.core.held()['accepted'][0].channel.close.assert_not_called()

    def commit(self, action):
        self.b.base.pool.submit(action).result(10)

    @contextmanager
    def after_gc(self, change):
        original = self.runtime.registry.transaction
        fired = []
        @contextmanager
        def transaction(*args, **kwargs):
            with original(*args, **kwargs) as db:
                yield db
            if kwargs.get('read_only') and not fired:
                fired.append(True)
                change()
        with patch.object(self.runtime.registry, 'transaction', transaction):
            yield
        self.assertEqual([True], fired)

    def test_gc_read_time_does_not_reset_public_deadline(self):
        with self.after_gc(lambda: setattr(self.b.base.f, 'clock', self.b.base.f.deadline)):
            response = self.call()
        self.assertFalse(response['ok'])
        self.assertEqual('START_TIMEOUT', response['error']['code'])
        self.assertEqual(0, self.core.creator.generated)

    def test_epoch_commit_after_gc_refuses_generation(self):
        def change():
            def write():
                with self.runtime.registry.transaction() as db:
                    db.execute('UPDATE instances SET epoch=epoch+1 WHERE id=?', (self.b.token['instanceId'],))
            self.commit(write)
        with self.after_gc(change):
            response = self.call()
        self.assertFalse(response['ok'])
        self.assertEqual('STALE_LEASE', response['error']['code'])
        self.assertEqual(0, self.core.creator.generated)

    def test_normal_public_renew_after_gc_keeps_same_deadline(self):
        deadlines = []
        original = self.runtime._offline_finish_start
        def finish(*args, **kwargs):
            deadlines.append(kwargs['deadline'])
            return original(*args, **kwargs)
        def change():
            self.commit(lambda: self.runtime.registry.execute('renew', dict(self.b.token, ttlSeconds=1000),
                                                              operation_id='renew-public-acceptance'))
        with self.after_gc(change), patch.object(self.runtime, '_offline_finish_start', side_effect=finish):
            self.f.start()
        self.assertEqual([self.b.base.f.deadline], deadlines)

    def test_epoch_commit_between_private_completion_and_public_gate(self):
        def change():
            def write():
                with self.runtime.registry.transaction() as db:
                    db.execute('UPDATE instances SET epoch=epoch+1 WHERE id=?', (self.b.token['instanceId'],))
            self.commit(write)
        self.rejected_after_finish(change)

    def test_same_public_operation_concurrent_call_cannot_generate_again(self):
        original = self.runtime._offline_finish_start
        duplicate = []
        def finish(*args, **kwargs):
            def second():
                try:
                    self.runtime.registry.execute('offline-start', self.payload, operation_id=self.operation)
                except RegistryError as error:
                    duplicate.append(error.code)
            self.commit(second)
            self.assertEqual(['OPERATION_CONFLICT'], duplicate)
            self.assertEqual(0, self.core.creator.generated)
            return original(*args, **kwargs)
        with patch.object(self.runtime, '_offline_finish_start', side_effect=finish):
            self.f.start()
        self.assertEqual(1, self.core.creator.generated)

    def test_resource_exit_expiry_preserves_running_without_public_return(self):
        registry = self.runtime.registry
        locked, arrived = threading.Event(), threading.Event()
        proxy = ObservedLock(registry.db_lock, arrived)
        transaction, resources = registry.transaction, registry.resource_use
        depth, fired, errors = [0], [], []
        def hold():
            try:
                with transaction(read_only=True) as db:
                    db.execute('SELECT 1').fetchone()
                    locked.set()
                    self.assertTrue(arrived.wait(5))
                    self.b.base.f.clock = self.b.base.f.deadline
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=hold)

        @contextmanager
        def adjusted(*args, **kwargs):
            main = threading.get_ident() == proxy.owner
            outer = main and depth[0] == 0
            if main:
                depth[0] += 1
            try:
                with resources(*args, **kwargs):
                    yield
                    if outer and not fired:
                        fired.append(True)
                        self.assertFalse(self.runtime._offline_requests[(self.b.token['instanceId'], self.operation)]['returned'])
                        self.assertFalse(self.f.entered.is_set())
                        worker.start()
                        self.assertTrue(locked.wait(5))
                        proxy.armed = True
            finally:
                if main:
                    depth[0] -= 1

        with patch.object(registry, 'db_lock', proxy), patch.object(registry, 'resource_use', adjusted):
            try:
                response = self.call()
            finally:
                arrived.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertEqual([True], fired)
        self.assertFalse(response['ok'])
        self.assertEqual('START_TIMEOUT', response['error']['code'])
        self.assertTrue(self.f.entered.wait(5))
        held = self.runtime._offline_requests[(self.b.token['instanceId'], self.operation)]
        self.assertFalse(held['returned'])
        self.assertEqual(self.b.base.f.deadline, held['deadline'])
        self.assertEqual(0, registry.resource_users)
        self.assertEqual(('RUNNING', 0), (self.b.row()['state'], self.b.row()['pending']))
        self.assertEqual('COMPLETED', self.b.saved()[0]['status'])
        self.assertFalse(self.b.saved()[1])
        slot = self.core.held()
        self.assertIs(slot['accepted'][0], slot['startup'][0]['endpoint'].connection)
        self.core.creator.m.close.assert_not_called()
        slot['accepted'][0].peer.close.assert_not_called()
        slot['accepted'][0].channel.close.assert_not_called()
        with registry.transaction(read_only=True) as db:
            ports = db.execute('SELECT count(*) FROM endpoint_reservations WHERE run=?',
                               (slot['creation_run']['runId'],)).fetchone()[0]
        self.assertEqual(len(slot['creation_run']['endpoints']), ports)
        session = self.runtime.managed.sessions[self.b.token['instanceId']]
        self.assertIs(self.core.creator.m, self.runtime.game_processes[session['run']][0])
        self.assertEqual(self.b.token, session['token'])
        self.assertTrue(self.f.thread.is_alive())
        self.assertFalse(session['done'].is_set())
        effects = self.core.creator.generated, bytes(self.core.f.f.channel.sent)
        self.assertFalse(self.call()['ok'])
        self.assertEqual(effects, (self.core.creator.generated, bytes(self.core.f.f.channel.sent)))

    def test_public_final_gate_catalog_and_no_io_after_last_bundle(self):
        source = textwrap.dedent(inspect.getsource(proofs.StartProofTests.observe_final_actions))
        source = source.replace("def profile(frame, event, value):\n        if state['phase'] is None:",
            "def profile(frame, event, value):\n        if event == 'call' and frame.f_code.co_name == 'verify_running' and not frame.f_locals.get('memory_only', False) and frame.f_back.f_code.co_name == '_offline_start': state['phase'] = 'external'\n        if state['phase'] is None:")
        source = source.replace("if name == 'verify_start_proof':", "if name == 'execute' and filename == 'registry.py':")
        source = source.replace("value.get('status') == 'OFFLINE_START_PROOF_RECORDED'", "value.get('status') == '未使用'")
        source = source.replace("\n        if event == 'return':\n", "\n        if event == 'return':\n            if name == 'is_alive' and filename == 'threading.py' and frame.f_locals.get('self') is self.held().get('receiver'): observed('receiver')\n")
        source = source.replace('            yield state', '            sys.setprofile(profile)\n            yield state')
        namespace = dict(proofs.__dict__)
        exec(compile(source, __file__, 'exec'), namespace)
        with namespace['observe_final_actions'](self.core) as state:
            self.f.start()
        self.assertTrue(state['finished'])
        expected = dict(self.core.final_actions, receiver=1)
        counts = {kind:sum(item[0] == kind for item in state['events']) for kind in expected}
        self.assertEqual(expected, counts)
        self.observations = state['events']

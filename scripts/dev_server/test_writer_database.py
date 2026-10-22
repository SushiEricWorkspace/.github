"""既存Registryを正本とするDB writer接続。Mod/DB/native childはfakeで、実機結合とは区別する。"""
import json
from pathlib import Path
from unittest.mock import patch
import unittest

from .build_inputs import canonical, digest
from .registry import RegistryError
from . import test_writer_control as fixtures


class ProxyPeer:
    def __init__(self, bundle, diagnostics, binding, *, verify, process_adapter):
        self.diagnostics = diagnostics
        self.verify = verify
        self.started = self.bound = self.exited = False
        self.request_bytes = self.response_bytes = None
        self.frames = []
        self.acks = []
        self.results = []
        self.bootstrap_status = 'OPEN'
        self.default_status = 'RESULT'
        self.partial = False
        self.exit_pending = False
        self.finish_calls = 0
        self.on_finish = lambda: None
        self.on_ack = lambda response: None
        self.history = False
        self.ended = False

    def start(self):
        self.started = True
        self.diagnostics.mkdir(parents=True)
        (self.diagnostics / 'creation-00.json').write_text('{"fixture":"fake native only"}')
        self.verify()

    def begin(self, frame, deadline):
        self.verify()
        self.frames.append(json.loads(canonical(frame)))
        self.request_bytes = canonical(frame)
        self.response_bytes = None
        if frame['command'] == 'execute' and self.partial:
            raise TimeoutError('fixture partial frame')
        status = self.bootstrap_status if frame['command'] == 'open' else self.default_status
        if frame['command'] in {'execute', 'resolve'} and self.results:
            status = self.results.pop(0)
        if frame['command'] == 'close':
            status = 'CLOSED' if self.bound else 'UNBOUND_CLOSED'
            self.ended = self.history
        response = {key: frame[key] for key in ('operationId', 'requestHash')} | {'status': status}
        if status == 'OPEN':
            response['response'] = {'type': 'database_context', 'success': True, 'binding': {
                'worldUuid': '11111111-1111-1111-1111-111111111111',
                'runId': '22222222-2222-2222-2222-222222222222'}}
        elif frame['command'] in {'execute', 'resolve'}:
            kind = frame.get('message', {}).get('type')
            if kind == 'database_history_start':
                self.history = True
            response['response'] = {'type': 'database_history' if self.history else 'money_history_save_result',
                                    'success': status == 'RESULT', 'detail': 'typed-response-secret'}
        if frame['command'] == 'close':
            response.update(historyEnded=True, pending=0, peerClosed=True)
        self.response_bytes = canonical(response)
        return self.receive(deadline)

    def receive(self, deadline):
        self.verify()
        if self.response_bytes is None:
            raise TimeoutError('fixture missing response')
        return json.loads(self.response_bytes)

    def acknowledge(self, response_hash):
        self.verify()
        assert digest(self.response_bytes) == response_hash
        response = json.loads(self.response_bytes)
        assert response['status'] != 'UNKNOWN'
        self.on_ack(response)
        self.acks.append(response['status'])
        if response['status'] == 'OPEN':
            self.bound = True
        self.request_bytes = self.response_bytes = None

    def finish_exit(self, deadline):
        self.verify()
        self.finish_calls += 1
        self.on_finish()
        if self.exit_pending:
            raise TimeoutError('fixture native child still present')
        assert json.loads(self.response_bytes)['status'] in {'CLOSED', 'UNBOUND_CLOSED'}
        self.exited = True
        return {'proxyExited': True, 'proxyChildrenExited': True}


class WriterDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WriterControlTests('test_status_file_io_and_actual_api_do_not_enumerate_unrelated_tree')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.registry = self.fixture.registry
        self.writer = self.registry.writer
        self.binding = self.fixture.binding
        self.instances = []
        self.configure = lambda proxy: None

        def create(*args, **kwargs):
            proxy = ProxyPeer(*args, **kwargs)
            self.configure(proxy)
            self.instances.append(proxy)
            return proxy
        self.addCleanup(patch.stopall)
        patch('dev_server.database_proxy.DatabaseProxy', side_effect=create).start()
        patch('dev_server.database_proxy_bundle.proxy_command', return_value=['fixed-bundle-jdk', 'fixed-proxy']).start()
        self.fixture.open()

    def database(self, operation='db-operation', *, message=None, binding=None, timeout=2):
        request = {'action': 'database', 'message': message or {'type': 'money_history_save'}, 'timeoutSeconds': timeout}
        return self.fixture.command('writer-io', dict(binding or self.binding, request=request), operation)

    def close(self, operation='db-close', *, binding=None):
        return self.fixture.command('writer-close', dict(binding or self.binding, drained=True, disconnected=True), operation)

    def session(self, session_id=None):
        with self.registry.transaction(read_only=True) as connection:
            return dict(connection.execute('SELECT * FROM writer_sessions WHERE id=?', (session_id or self.binding['sessionId'],)).fetchone())

    def operation(self, operation_id='db-operation'):
        with self.registry.transaction(read_only=True) as connection:
            row = connection.execute('SELECT * FROM writer_operations WHERE id=?', (operation_id,)).fetchone()
            return dict(row) if row else None

    def test_success_same_id_readback_privacy_and_close_after_real_exit_boundary(self):
        message = {'type': 'money_history_save', 'playerUuid': '77777777-7777-7777-7777-777777777777'}
        first = self.database(message=message)
        self.assertTrue(first['ok'], first)
        self.assertTrue(first['result']['completed'])
        proxy = self.instances[0]
        self.assertEqual(['open', 'execute'], [frame['command'] for frame in proxy.frames])
        again = self.database(message=message)
        self.assertTrue(again['result']['replayed'])
        self.assertTrue(again['result']['responseAvailable'])
        self.assertEqual(2, len(proxy.frames))
        for secret in ('77777777-7777-7777-7777-777777777777', 'typed-response-secret'):
            self.assertNotIn(secret, self.operation()['response'])
        proxy.on_finish = lambda: self.assertEqual('CLOSING', self.session()['status'])
        closed = self.close()
        self.assertTrue(closed['ok'], closed)
        self.assertTrue(closed['result']['closed'])
        self.assertTrue(proxy.exited)
        self.assertEqual('CLOSED', self.session()['status'])
        self.assertEqual(0, self.fixture.status()['writers'])

    def test_completed_dry_run_replay_does_not_claim_or_receive_retained_response(self):
        self.assertTrue(self.database()['ok'])
        proxy = self.instances[0]
        operation, session = self.operation(), self.session()
        payload = dict(self.binding, request={'action': 'database', 'message': {'type': 'money_history_save'}, 'timeoutSeconds': 2})
        with patch.object(self.writer, '_claim_existing') as claim, patch.object(self.writer, '_release_call') as release, \
                patch.object(proxy, 'receive') as receive, patch.object(self.writer, '_database_ack') as ack:
            response = fixtures.dispatch(self.registry, fixtures.new_request('writer-io', payload,
                                         operation_id='db-operation', dry_run=True))
        self.assertTrue(response['ok'], response)
        self.assertTrue(response['result']['replayed'])
        self.assertTrue(response['result']['dryRun'])
        self.assertFalse(response['result']['responseAvailable'])
        for call in (claim, release, receive, ack):
            call.assert_not_called()
        self.assertEqual(operation, self.operation())
        self.assertEqual(session, self.session())
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM reservations WHERE reference LIKE 'writer-call:%'").fetchone()[0])
        self.assertTrue(self.database()['result']['responseAvailable'])

    def test_bootstrap_keeps_validated_local_request_when_caller_changes_nested_message(self):
        message = {'type': 'money_history_save', 'playerUuid': '77777777-7777-7777-7777-777777777777'}
        original = dict(message)
        self.configure = lambda proxy: message.update(type='command', playerUuid='changed', command='not-allowed')
        first = self.database(message=message)
        self.assertTrue(first['ok'], first)
        self.assertEqual(original, self.instances[0].frames[1]['message'])
        replay = self.database(message=original)
        self.assertTrue(replay['result']['replayed'], replay)
        self.assertEqual(2, len(self.instances[0].frames))

    def test_open_record_failure_rejects_unsent_message_then_allows_close(self):
        original = self.writer._database_record
        def fail(*args, **kwargs):
            if kwargs['phase'] == 'bootstrap':
                raise OSError('fixture bootstrap record failure')
            return original(*args, **kwargs)
        with patch.object(self.writer, '_database_record', side_effect=fail):
            self.assertFalse(self.database()['ok'])
        proxy = self.instances[0]
        result = self.database()
        self.assertTrue(result['result']['completed'], result)
        self.assertTrue(result['result']['rejectedBeforeEffect'])
        self.assertEqual('DB_BOOTSTRAP_INTERRUPTED', result['result']['code'])
        self.assertEqual(['open'], [item['command'] for item in proxy.frames])
        self.assertTrue(self.close()['result']['closed'])
        self.assertEqual(['open', 'close'], [item['command'] for item in proxy.frames])

    def test_open_ack_before_clear_failure_rejects_unsent_message_then_next_operation_works(self):
        failed = False
        def configure(proxy):
            def ack(response):
                nonlocal failed
                if response['status'] == 'OPEN' and not failed:
                    failed = True
                    raise OSError('fixture before OPEN ack')
            proxy.on_ack = ack
        self.configure = configure
        self.assertFalse(self.database()['ok'])
        result = self.database()
        self.assertEqual('DB_BOOTSTRAP_INTERRUPTED', result['result']['code'], result)
        proxy = self.instances[0]
        self.assertEqual(['open'], [item['command'] for item in proxy.frames])
        self.assertTrue(self.database('next-operation')['result']['completed'])
        self.assertEqual(['open', 'execute'], [item['command'] for item in proxy.frames])
        self.assertTrue(self.close()['result']['closed'])

    def test_open_ack_after_clear_failure_uses_recorded_open_without_new_frame(self):
        def configure(proxy):
            original = proxy.acknowledge
            def ack(response_hash):
                status = json.loads(proxy.response_bytes)['status']
                original(response_hash)
                if status == 'OPEN':
                    raise OSError('fixture after OPEN ack')
            proxy.acknowledge = ack
        self.configure = configure
        self.assertFalse(self.database()['ok'])
        proxy = self.instances[0]
        self.assertTrue(proxy.bound)
        self.assertIsNone(proxy.request_bytes)
        result = self.database()
        self.assertEqual('DB_BOOTSTRAP_INTERRUPTED', result['result']['code'], result)
        self.assertEqual(['open'], [item['command'] for item in proxy.frames])
        self.assertTrue(self.close()['result']['closed'])

    def test_close_directly_after_open_ack_failure_finishes_unsent_io_before_closed(self):
        def configure(proxy):
            original = proxy.acknowledge
            def ack(response_hash):
                status = json.loads(proxy.response_bytes)['status']
                original(response_hash)
                if status == 'OPEN':
                    raise OSError('fixture after OPEN ack before execute')
            proxy.acknowledge = ack
            def finishing():
                self.assertEqual('COMPLETED', self.operation()['status'])
                self.assertEqual('DB_BOOTSTRAP_INTERRUPTED', json.loads(self.operation()['response'])['code'])
            proxy.on_finish = finishing
        self.configure = configure
        self.assertFalse(self.database()['ok'])
        self.assertEqual('PENDING', self.operation()['status'])
        result = self.close()
        self.assertTrue(result['result']['closed'], result)
        self.assertEqual(['open', 'close'], [item['command'] for item in self.instances[0].frames])
        self.assertEqual('CLOSED', self.session()['status'])

    def test_close_with_lost_execute_evidence_preserves_io_and_writer(self):
        self.configure = lambda proxy: setattr(proxy, 'default_status', 'PENDING')
        self.assertFalse(self.database()['result']['completed'])
        proxy = self.instances[0]
        proxy.request_bytes = proxy.response_bytes = None
        result = self.close()
        self.assertFalse(result['ok'], result)
        self.assertEqual('PENDING', self.operation()['status'])
        self.assertEqual('CLOSING', self.session()['status'])
        self.assertEqual(0, proxy.finish_calls)
        self.assertEqual(['open', 'execute'], [item['command'] for item in proxy.frames])

    def test_missing_open_evidence_does_not_reject_or_complete_pending_request(self):
        with patch.object(self.writer, '_database_record', side_effect=OSError('fixture missing root receipt')):
            self.assertFalse(self.database()['ok'])
        proxy = self.instances[0]
        proxy.request_bytes = proxy.response_bytes = None
        proxy.bound = False
        result = self.database()
        self.assertFalse(result['ok'], result)
        self.assertEqual('PENDING', self.operation()['status'])
        self.assertEqual(['open'], [item['command'] for item in proxy.frames])

    def test_execute_frame_loss_is_not_reclassified_as_unsent_bootstrap(self):
        self.configure = lambda proxy: setattr(proxy, 'default_status', 'PENDING')
        self.assertFalse(self.database()['result']['completed'])
        proxy = self.instances[0]
        proxy.request_bytes = proxy.response_bytes = None
        result = self.database()
        self.assertFalse(result['ok'], result)
        self.assertEqual('PENDING', self.operation()['status'])
        self.assertEqual(['open', 'execute'], [item['command'] for item in proxy.frames])

    def test_pending_waits_original_future_without_second_execute(self):
        self.configure = lambda proxy: proxy.results.extend(['PENDING', 'RESULT'])
        first = self.database()
        self.assertFalse(first['result']['completed'])
        proxy = self.instances[0]
        second = self.database()
        self.assertTrue(second['result']['completed'], second)
        self.assertEqual(['open', 'execute', 'resolve'], [item['command'] for item in proxy.frames])
        conflict = self.database(message={'type': 'money_history_save', 'playerUuid': 'other'})
        self.assertFalse(conflict['ok'])
        self.assertEqual('OPERATION_CONFLICT', conflict['error']['code'])
        self.assertEqual(3, len(proxy.frames))

    def test_known_unknown_never_resends_or_closes_writer(self):
        self.configure = lambda proxy: setattr(proxy, 'default_status', 'UNKNOWN')
        self.assertEqual('UNKNOWN', self.database()['result']['proxyStatus'])
        proxy = self.instances[0]
        self.assertEqual('UNKNOWN', self.database()['result']['proxyStatus'])
        self.assertFalse(self.database('new-db-id')['ok'])
        result = self.close()
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['result']['closed'])
        self.assertEqual('CLOSING', self.session()['status'])
        self.assertEqual(['open', 'execute'], [item['command'] for item in proxy.frames])
        self.assertEqual(0, proxy.finish_calls)
        self.assertEqual(1, self.fixture.status()['writers'])

    def test_partial_frame_is_not_completed_by_same_id_retry(self):
        self.configure = lambda proxy: setattr(proxy, 'partial', True)
        self.assertFalse(self.database()['ok'])
        proxy = self.instances[0]
        self.assertFalse(self.database()['ok'])
        self.assertEqual(['open', 'execute'], [item['command'] for item in proxy.frames])
        self.assertEqual('UNKNOWN', self.session()['status'])
        self.assertEqual('PENDING', self.operation()['status'])

    def test_record_failure_keeps_buffer_until_same_owner_records_it(self):
        original = self.writer._database_record
        failed = False
        def record(*args, **kwargs):
            nonlocal failed
            if kwargs['phase'] == 'execute' and not failed:
                failed = True
                raise OSError('fixture record failure before commit')
            return original(*args, **kwargs)
        with patch.object(self.writer, '_database_record', side_effect=record):
            self.assertFalse(self.database()['ok'])
        proxy = self.instances[0]
        self.assertEqual(['OPEN'], proxy.acks)
        self.assertIsNotNone(proxy.response_bytes)
        self.assertEqual('PENDING', self.operation()['status'])
        self.assertEqual('UNKNOWN', self.session()['status'])
        self.assertTrue(self.database()['result']['completed'])
        self.assertEqual(2, len(proxy.frames))
        self.assertEqual('COMPLETED', self.operation()['status'])
        self.assertEqual(['OPEN'], proxy.acks)

    def test_close_drains_pending_start_before_history_end_and_blocks_new_request(self):
        self.configure = lambda proxy: proxy.results.extend(['PENDING', 'RESULT'])
        self.assertFalse(self.database(message={'type': 'database_history_start', 'binding': {}})['result']['completed'])
        proxy = self.instances[0]
        def before_exit():
            self.assertEqual('CLOSING', self.session()['status'])
            self.assertEqual('COMPLETED', self.operation()['status'])
            payload = dict(self.binding, request={'action': 'database', 'message': {'type': 'money_history_save'}, 'timeoutSeconds': 2})
            with self.assertRaises(RegistryError):
                self.writer.execute('writer-io', payload, operation_id='blocked-new-db')
            self.assertIsNone(self.operation('blocked-new-db'))
        proxy.on_finish = before_exit
        result = self.close()
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['result']['closed'])
        self.assertTrue(proxy.ended)
        self.assertEqual(['open', 'execute', 'resolve', 'close'], [item['command'] for item in proxy.frames])

    def test_close_waits_child_exit_and_reuses_known_closed_frame(self):
        self.assertTrue(self.database()['ok'])
        proxy = self.instances[0]
        proxy.exit_pending = True
        self.assertFalse(self.close()['ok'])
        self.assertEqual('CLOSING', self.session()['status'])
        self.assertEqual('PENDING', self.operation('db-close')['status'])
        proxy.exit_pending = False
        result = self.close()
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['result']['closed'])
        self.assertEqual(1, sum(item['command'] == 'close' for item in proxy.frames))

    def test_closed_commit_failure_does_not_publish_closed_early(self):
        self.assertTrue(self.database()['ok'])
        proxy = self.instances[0]
        original = self.writer._database_record
        def fail_closed(*args, **kwargs):
            if kwargs.get('exit_proof'):
                raise OSError('fixture CLOSED commit failed')
            return original(*args, **kwargs)
        with patch.object(self.writer, '_database_record', side_effect=fail_closed):
            self.assertFalse(self.close()['ok'])
        self.assertTrue(proxy.exited)
        self.assertEqual('CLOSING', self.session()['status'])
        self.assertEqual(1, self.fixture.status()['writers'])
        self.assertTrue(self.close()['result']['closed'])
        self.assertEqual(1, sum(item['command'] == 'close' for item in proxy.frames))

    def test_four_unknown_proxy_slots_reject_fifth_before_accept_without_blocking_old_io(self):
        self.configure = lambda proxy: setattr(proxy, 'default_status', 'UNKNOWN')
        for index in range(1, 6):
            binding = dict(self.binding, sessionId=f'session-{index:02d}')
            if index > 1:
                opened = self.fixture.command('writer-open', dict(self.fixture.payload, sessionId=binding['sessionId']), f'open-{index}')
                self.assertTrue(opened['ok'], opened)
            result = self.database(f'db-{index}', binding=binding)
            if index <= 4:
                self.assertTrue(result['ok'], result)
                self.assertEqual('UNKNOWN', result['result']['proxyStatus'])
            else:
                self.assertFalse(result['ok'])
                self.assertEqual('DB_PROXY_LIMIT', result['error']['code'])
                self.assertIsNone(self.operation('db-5'))
                old = self.fixture.command('writer-io', dict(binding, request={'action': 'write', 'path': 'item_data/normal.yml', 'text': 'old v2 caller'}), 'old-write')
                self.assertTrue(old['ok'], old)
        self.assertEqual(4, len(self.instances))
        self.assertEqual(4, len(self.writer.database_proxies))

    def test_process_restart_lost_body_never_spawns_replacement(self):
        self.assertTrue(self.database()['ok'])
        self.writer.database_proxies.clear()
        replay = self.database()
        self.assertTrue(replay['result']['completed'])
        self.assertFalse(replay['result']['responseAvailable'])
        self.assertFalse(self.database('new-after-restart')['ok'])
        self.assertFalse(self.close()['ok'])
        self.assertEqual(1, len(self.instances))
        self.assertEqual('CLOSING', self.session()['status'])

    def test_context_mismatch_is_before_effect_and_unbound_resources_can_close(self):
        self.configure = lambda proxy: setattr(proxy, 'bootstrap_status', 'REJECTED')
        response = self.database()
        self.assertTrue(response['result']['completed'])
        self.assertTrue(response['result']['rejectedBeforeEffect'])
        self.assertFalse(response['result']['responseAvailable'])
        self.assertEqual(['open'], [item['command'] for item in self.instances[0].frames])
        self.assertTrue(self.close()['result']['closed'])
        self.assertEqual(1, len(self.instances))

    def test_bootstrap_unknown_is_not_replaced_or_forwarded(self):
        self.configure = lambda proxy: setattr(proxy, 'bootstrap_status', 'UNKNOWN')
        first = self.database()
        self.assertFalse(first['result']['completed'])
        self.assertFalse(first['result']['responseAvailable'])
        self.assertEqual('UNKNOWN', self.database()['result']['proxyStatus'])
        self.assertEqual(['open'], [item['command'] for item in self.instances[0].frames])

    def test_new_database_input_rejected_before_proxy_or_operation_acceptance(self):
        for index, (message, timeout) in enumerate([({'type': 'ping'}, 2), ({'type': []}, 2),
                ({'type': 'money_history_save', 'rawSql': 'not accepted'}, 2),
                ({'type': 'money_history_save', 'playerUuid': 'あ' * 21000}, 2),
                ({'type': 'money_history_save'}, 31), ({'type': 'money_history_save'}, True)]):
            with self.subTest(index=index):
                result = self.database(f'invalid-db-{index}', message=message, timeout=timeout)
                self.assertFalse(result['ok'])
                self.assertIsNone(self.operation(f'invalid-db-{index}'))
        self.assertEqual([], self.instances)
        self.assertEqual(0, self.session()['pending'])

    def test_missing_new_adapter_leaves_old_api_and_file_paths_unchanged(self):
        with patch('dev_server.database_proxy_bundle.proxy_command', side_effect=FileNotFoundError('fixture old v2')):
            rejected = self.database()
        self.assertFalse(rejected['ok'])
        self.assertEqual('ADAPTER_UNAVAILABLE', rejected['error']['code'])
        self.assertIsNone(self.operation())
        self.assertEqual([], self.instances)
        normal = self.fixture.io('write', path='item_data/unchanged.yml', text='normal old path')
        self.assertTrue(normal['ok'], normal)
        with patch('dev_server.writer_api.exchange', return_value={'type': 'pong', 'nonce': 'old-api'}):
            old = self.fixture.command('writer-io', dict(self.binding, request={'action': 'api', 'message': {'type': 'ping'}, 'timeoutSeconds': 2}), 'old-api')
        self.assertTrue(old['ok'], old)
        self.assertEqual('pong', old['result']['apiResponse']['type'])
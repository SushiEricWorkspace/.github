"""停止済み管理runの正本予約・候補・元W終了証拠とC本人確定を接続する。"""

from copy import deepcopy
import json
import time

from .build_inputs import canonical, digest, regular_bytes
from .mod_control_runtime import _closed_writers
from .local_ipc import plain_path
from .snapshot_data import portable_properties, properties
from .mod_control_startup import _admission, _change
from .mod_control_wire import _canonical_uuid, _hash
from .offline_process import OfflineProcess
from .registry import fields, identifier, require
from .runtime_bundle import verify_bundle, require_data_generation
from .runtime_control import TOKEN


class _OfflineOpen:
    def __init__(self, runtime):
        self.runtime = runtime
        self.registry = runtime.registry
        self.controllers = {}
        self.workers = {}
        self.history = {}

    def _state(self, connection, token, *, reserved, release_operation_id=None):
        row = dict(self.registry._instance(connection, token['instanceId']))
        proof = json.loads(row['runtime_proof']) if row['runtime_proof'] else {}
        terminal_release = (release_operation_id is not None
                            and proof.get('gameHandleReleaseOperationId') == release_operation_id)
        self.registry._token(row, token, allow_expired=terminal_release)
        require(row['state'] == 'LEASED_STOPPED' and row['generation_bound'] and row['generation'] is not None
                and row['pending'] == int(reserved) and not row['writers'] and not row['children'],
                'WRITER_PENDING', '停止済み世代とwriter/child/未完了操作の照合が必要です')
        gate = _admission(connection, row)
        require(gate['gate'] == 'BLOCKED' and gate['descriptor_hash'] is not None,
                'CONTROL_BLOCKED', '元認証runの受付遮断が必要です')
        run = json.loads(connection.execute('SELECT record FROM runs WHERE id=?', (row['run_id'],)).fetchone()[0])
        require(all(run[k] == row[v] for k, v in (('instanceId','id'),('generationId','generation'),
                    ('artifactId','artifact'),('runId','run_id'))) and run.get('jvmAndChildrenExited') is True
                and run.get('runtimeSaveCompleted') is True and type(run.get('exitCode')) is int and run['exitCode'] == 0,
                'PROOF_REQUIRED', '元runの保存・全child正常終了証拠が必要です')
        require(all(proof.get(k) == row[v] for k,v in (('instanceId','id'),('generation','generation'),('runId','run_id')))
                and proof.get('nonce') == run['nonce'] and proof.get('runtimeStopped') is True
                and proof.get('modSaveCompleted') is True and proof.get('jvmAndChildrenExited') is True
                and type(proof.get('exitCode')) is int and proof['exitCode'] == 0 and proof.get('proofHash') == run.get('proofHash')
                and type(run.get('identity')) is dict,
                'PROOF_REQUIRED', '元runと正常停止の耐久証拠が一致しません')
        _hash(proof['proofHash'])
        stop = connection.execute('SELECT * FROM runtime_operations WHERE id=?', (proof.get('stopOperationId'),)).fetchone()
        require(stop is not None and stop['instance'] == row['id'] and stop['command'] == 'stop'
                and stop['status'] == 'COMPLETED' and json.loads(stop['response']) == proof,
                'PROOF_REQUIRED', '元stop操作の確定証拠がありません')
        require(not connection.execute("SELECT 1 FROM writer_sessions WHERE instance=? AND (status!='CLOSED' OR pending!=0)",
                                       (row['id'],)).fetchone(), 'WRITER_PENDING', '未終了writerを保全します')
        require(not connection.execute("SELECT 1 FROM writer_operations o JOIN writer_sessions w ON w.id=o.session WHERE w.instance=? AND o.status!='COMPLETED'",
                                       (row['id'],)).fetchone(), 'WRITER_PENDING', '未完了writer操作を保全します')
        writers = _closed_writers(connection, row['id'], row['run_id'])
        return row, gate, run, proof, writers

    def _reserved(self, connection, token, operation_id, fingerprint, expected):
        row, gate, run, proof, writers = self._state(connection, token, reserved=True)
        operation = connection.execute('SELECT * FROM runtime_operations WHERE id=?', (operation_id,)).fetchone()
        require(operation is not None and operation['instance'] == row['id'] and operation['command'] == 'offline-open'
                and operation['fingerprint'] == fingerprint and operation['status'] == 'PENDING',
                'STALE_OPERATION', '元offline予約が一致しません')
        saved = json.loads(operation['response'])
        require(saved.get('binding') == expected and gate['revision'] == expected['revision']
                and gate['descriptor_hash'] == expected['descriptorSha256']
                and digest(canonical(run)) == expected['runRecordSha256']
                and digest(canonical(proof)) == expected['stopProofSha256']
                and writers == expected['writers'], 'STALE_OPERATION', '閉門・drain・停止証拠が変更されました')
        refs = connection.execute('SELECT * FROM reservations WHERE instance=?', (row['id'],)).fetchall()
        require(len(refs) == 1 and refs[0]['operation'] == operation_id and refs[0]['reference'] == 'artifact:' + row['artifact']
                and refs[0]['lease'] == row['lease'] and refs[0]['epoch'] == row['epoch'],
                'STALE_OPERATION', '元artifact予約が一致しません')
        require(not connection.execute("SELECT 1 FROM runtime_operations WHERE instance=? AND status!='COMPLETED' AND id!=?",
                                       (row['id'], operation_id)).fetchone(), 'WRITER_PENDING', '別の未完了runtime操作があります')
        return run

    def _opened(self, connection, token, controller, close_operation_id=None):
        require(controller.ready, 'WRITER_PENDING', 'OPEN未照合のCを使えません')
        opened = json.loads(controller.request)
        response = json.loads(controller.response)
        operation = connection.execute('SELECT response FROM runtime_operations WHERE id=?',
                                       (opened['operationId'],)).fetchone()
        require(operation is not None, 'STALE_OPERATION', '元取得予約がありません')
        saved = json.loads(operation['response'])
        require(digest(canonical(saved['binding'])) == opened['drainSha256']
                and saved.get('startupResponseSha256') == digest(controller.response),
                'STALE_OPERATION', '元Cの起動応答と正本記録が一致しません')
        self._reserved(connection, token, opened['operationId'], opened['requestHash'], saved['binding'])
        require('ownerClose' not in saved or close_operation_id is not None
                and saved['ownerClose'].get('operationId') == close_operation_id,
                'WRITER_PENDING', '元Cのclose意図を保全します')
        return opened, response, saved

    def _prepared(self, connection, token, controller, worker, operation_id, close_operation_id=None):
        opened, response, saved = self._opened(connection, token, controller, close_operation_id)
        history = self.history.get(token['instanceId'], [])
        require(saved.get('history', []) == [record for _, record in history]
                and set(controller.operation_history) == {record['worker']['operationId'] for _, record in history},
                'STALE_OPERATION', '元Cの操作別履歴と正本が一致しません')
        for previous, record in history:
            previous_id = record['worker']['operationId']
            held = controller.operation_history[previous_id]
            completion = held['completion_response']
            require(previous.controller is controller and held['assignment_acknowledged']
                    and held['completion_ready'] and held['completion_acknowledged']
                    and record['assignment'] == {'operationId':previous_id, 'responseSha256':digest(held['assignment_response'])}
                    and record['completion'] == {'operationId':previous_id, 'workerExitSha256':digest(previous.exit_response),
                                                 'responseSha256':digest(completion)}
                    and held['record'].get('assignedResponseSha256') == held['record'].get('assignedAcknowledgedSha256') == digest(held['assignment_response'])
                    and held['record'].get('completionResponseSha256') == held['record'].get('completionAcknowledgedSha256') == digest(completion),
                    'STALE_OPERATION', '元CのAssigned/complete原文と正本記録/ACKを保全します')
            response = json.loads(completion)
            require(response['operationId'] == previous_id and response['status'] == 'COMPLETED',
                    'STALE_OPERATION', '履歴末尾のIdleが元操作と一致しません')
        require(worker.controller is controller and controller.startup_acknowledged and worker.startup_acknowledged,
                'WRITER_PENDING', '元Cと正本記録済みWが必要です')
        frame = json.loads(worker.request)
        require(saved.get('worker') == {'operationId':operation_id,'requestSha256':frame['requestHash'],
                                       'startupResponseSha256':digest(worker.response)}
                and frame['operationId'] == operation_id
                and all(frame[k] == opened[k] for k in ('leaseId','epoch','generation','drainSha256',
                                                       'worldRoot','shopDirectory','worldUuid'))
                and frame['expectedStateSha256'] == response['stateSha256']
                and frame['expectedRevision'] == response['stateRevision'] and frame['runUuid'] == response['runUuid'],
                'STALE_OPERATION', '元PREPAREDとCのIdle/管理bindingが一致しません')
        return opened, saved

    def _assigned(self, connection, token, controller, worker, operation_id, close_operation_id=None):
        opened, saved = self._prepared(connection, token, controller, worker, operation_id, close_operation_id)
        require(controller.assignment_ready and controller.assignment_acknowledged
                and saved.get('assignment') == {'operationId':operation_id,
                                               'responseSha256':digest(controller.assignment_response)},
                'STALE_OPERATION', '元Assignedの応答と正本記録が一致しません')
        return opened, saved

    def _candidate(self, connection, token, controller, worker, operation_id, close_operation_id=None):
        opened, saved = self._assigned(connection, token, controller, worker, operation_id, close_operation_id)
        require(worker.candidate_ready and saved.get('admission') == {
                    'operationId':operation_id, 'candidateResponseSha256':digest(worker.candidate_response)}
                and worker.record.get('candidateResponseSha256') == digest(worker.candidate_response),
                'STALE_OPERATION', '元候補packetのローカル記録と正本が一致しません')
        return opened, saved

    def _exited(self, connection, token, controller, worker, operation_id, close_operation_id=None):
        opened, saved = self._candidate(connection, token, controller, worker, operation_id, close_operation_id)
        require(worker.exit_started and worker.exit_response is not None,
                'WRITER_PENDING', '元Wの正常終了証拠が必要です')
        exited = json.loads(worker.exit_response)
        require(saved.get('workerExit') == {'operationId':operation_id,'responseSha256':digest(worker.exit_response)}
                and worker.record.get('workerExit') == exited
                and exited.keys() == {'status','operationId','candidateResponseSha256','worker','ownerGroup','exitCode','allChildrenExited'}
                and exited['status'] == 'WORKER_EXITED' and exited['operationId'] == operation_id
                and exited['candidateResponseSha256'] == digest(worker.candidate_response)
                and type(exited['exitCode']) is int and exited['exitCode'] == 0 and exited['allChildrenExited'] is True
                and exited['worker'] == worker.data_identity == json.loads(controller.assignment_request)['worker']
                and worker.record['controlIdentity'] == worker.data_identity and worker.record['identity'] == worker.identity
                and worker.process.pid == worker.identity['pid'] == worker.data_identity['pid'] == worker.record['creation']['pid']
                and exited['ownerGroup'] == worker.process.group == worker.record['creation']['ownerGroup']
                and worker.process.group != controller.process.group,
                'STALE_OPERATION', '元W/全child終了証拠と同候補・生成binding・正本が一致しません')
        return opened, saved

    def _idle(self, connection, token, controller, operation_id, deadline):
        opened, response, saved = self._opened(connection, token, controller, close_operation_id=operation_id)
        worker = self.workers.get(token['instanceId'])
        history = self.history.get(token['instanceId'], [])
        require(saved.get('history', []) == [record for _, record in history],
                'STALE_OPERATION', '元Cの操作履歴が変更されました')
        if worker is None:
            require(not history and not controller.operation_history and 'worker' not in saved,
                    'WRITER_PENDING', '受理済みWの終端が必要です')
        else:
            previous_id = json.loads(worker.request)['operationId']
            self._exited(connection, token, controller, worker, previous_id, close_operation_id=operation_id)
            value = digest(controller.completion_response) if controller.completion_response is not None else None
            require(controller.completion_ready and controller.completion_acknowledged
                    and saved.get('completion') == {'operationId':previous_id,
                        'workerExitSha256':digest(worker.exit_response), 'responseSha256':value}
                    and controller.record.get('completionResponseSha256') == value
                    and controller.record.get('completionAcknowledgedSha256') == value,
                    'WRITER_PENDING', '最新Wのcomplete記録/ACKが必要です')
            response = json.loads(controller.completion_response)
        require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C closeの最新Idle照合後に期限を超過しました')
        return opened, response, saved

    def close_owner(self, token, *, operation_id, deadline):
        """元Cの最新IdleからData close応答を一度記録し、要求と予約を保持する。"""
        fields(token, TOKEN)
        identifier(operation_id, 'operationId')
        token = deepcopy(token)
        controller = self.controllers.get(token['instanceId'])
        require(controller is not None, 'WRITER_PENDING', '保持した元Cが必要です')

        def idle(connection):
            return self._idle(connection, token, controller, operation_id, deadline)

        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            with self.runtime._transaction(token['instanceId']) as connection:
                opened, latest, saved = idle(connection)
                require('ownerClose' not in saved, 'WRITER_PENDING', '元Cのclose要求を再送しません')
                used = {opened['operationId'], *controller.operation_history}
                if token['instanceId'] in self.workers:
                    used.add(json.loads(self.workers[token['instanceId']].request)['operationId'])
                require(operation_id not in used, 'OPERATION_CONFLICT', '元C/Wの操作IDをcloseへ再使用しません')
                for table in ('operations','runtime_operations','reference_operations','writer_operations','snapshot_operations'):
                    require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (operation_id,)).fetchone(),
                            'OPERATION_CONFLICT', '既存の操作IDをcloseへ再使用しません')
                closing = {'operationId':operation_id, 'idleResponseSha256':digest(canonical(latest))}
                closing['requestHash'] = digest(canonical({'command':'offline-close-owner', 'token':token,
                    'openOperationId':opened['operationId'], 'idleResponseSha256':closing['idleResponseSha256'],
                    'operationId':operation_id}))
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C close意図の更新前に期限を超過しました')
                saved['ownerClose'] = closing
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (json.dumps(saved), opened['operationId']))

            def verify():
                with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                    _, current_idle, current = idle(connection)
                    require(current.get('ownerClose') == closing and canonical(current_idle) == canonical(latest),
                            'STALE_OPERATION', '元C close意図/最新Idleが変更されました')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C closeの正本照合後に期限を超過しました')

            verify()
            if not controller.startup_acknowledged:
                controller.acknowledge_startup(saved['startupResponseSha256'], deadline=deadline)
            response = controller.close_owner(latest, operation_id=operation_id,
                request_hash=closing['requestHash'], deadline=deadline, verify=verify)
            verify()
            require(controller.close_ready and canonical(response) == controller.close_response
                    and controller.record.get('ownerCloseResponseSha256') == digest(controller.close_response),
                    'STALE_OPERATION', '元C close応答の保持記録が一致しません')
            with self.runtime._transaction(token['instanceId']) as connection:
                _, current_idle, current = idle(connection)
                require(current.get('ownerClose') == closing and canonical(current_idle) == canonical(latest),
                        'STALE_OPERATION', 'C close結果の最終正本照合が一致しません')
                recorded = dict(closing, responseSha256=digest(controller.close_response))
                current['ownerClose'] = recorded
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C close結果の更新前に期限を超過しました')
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (json.dumps(current), opened['operationId']))
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C close結果の記録後に期限を超過しました')
            return response

    def observe_owner_exit(self, token, *, operation_id, deadline):
        """元closeの対応応答とC/全child正常終了を正本へ記録する。

        同close操作IDだけを受け付け、一度開始した観測は再試行しない。失敗時も原文・
        元予約・handle/pipeを保持する。成功もwriter CLOSEDや予約解放を意味しない。
        """
        fields(token, TOKEN)
        identifier(operation_id, 'operationId')
        token = deepcopy(token)
        controller = self.controllers.get(token['instanceId'])
        require(controller is not None, 'WRITER_PENDING', '保持した元Cが必要です')

        require(controller.close_binding is not None, 'WRITER_PENDING', '元closeの固定期限が必要です')
        deadline = min(deadline, controller.close_binding['deadline'])

        def closed(connection):
            require(self.controllers.get(token['instanceId']) is controller,
                    'STALE_OPERATION', '元Cの保持先が変更されました')
            opened, latest, saved = self._idle(connection, token, controller, operation_id, deadline)
            closing = saved.get('ownerClose', {})
            require(controller.close_ready and controller.close_request is not None and controller.close_response is not None
                    and closing.get('operationId') == operation_id
                    and closing.get('idleResponseSha256') == digest(canonical(latest))
                    and closing.get('responseSha256') == digest(controller.close_response)
                    and controller.record.get('ownerCloseRequestSha256') == digest(controller.close_request)
                    and controller.record.get('ownerCloseResponseSha256') == digest(controller.close_response),
                    'STALE_OPERATION', '元closeの記録済み対応応答が必要です')
            expected_hash = digest(canonical({'command':'offline-close-owner', 'token':token,
                'openOperationId':opened['operationId'], 'idleResponseSha256':closing['idleResponseSha256'],
                'operationId':operation_id}))
            frame = dict(command='close', operationId=operation_id, requestHash=expected_hash,
                         sessionId=controller.session, nonce=controller.nonce, openOperationId=opened['operationId'],
                         expectedStateSha256=latest['stateSha256'], expectedRevision=latest['stateRevision'])
            response = json.loads(controller.close_response)
            require(closing.get('requestHash') == expected_hash and canonical(frame) == controller.close_request
                    and response.keys() == {'status','operationId','requestHash','sessionId','nonce',
                                            'stateSha256','stateRevision','runUuid'}
                    and response['status'] == 'OWNER_CLOSED'
                    and all(response[k] == frame[k] for k in ('operationId','requestHash','sessionId','nonce'))
                    and response['runUuid'] == latest['runUuid']
                    and type(response['stateRevision']) is int and response['stateRevision'] == latest['stateRevision'] + 1,
                    'STALE_OPERATION', '元close要求/応答と最新Idleのbindingが一致しません')
            _hash(response['stateSha256'])
            return opened, saved, closing

        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            with self.runtime._transaction(token['instanceId']) as connection:
                opened, saved, closing = closed(connection)
                require('ownerExit' not in saved, 'WRITER_PENDING', '元Cの終了観測を再実行しません')
                observation = dict(operationId=operation_id, closeResponseSha256=closing['responseSha256'])
                saved['ownerExit'] = observation
                encoded = json.dumps(saved)
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C終了観測意図の更新前に期限を超過しました')
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?', (encoded, opened['operationId']))

            def verify():
                with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                    _, current, current_close = closed(connection)
                    require(current.get('ownerExit') == observation and current_close == closing,
                            'STALE_OPERATION', 'C終了観測意図/close応答が変更されました')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C終了観測の正本照合後に期限を超過しました')

            verify()
            response = controller.observe_owner_exit(deadline=deadline, verify=verify)
            verify()
            require(canonical(response) == controller.owner_exit_response
                    and controller.record.get('ownerExit') == response,
                    'STALE_OPERATION', '元Cの終了観測原文と保持記録が一致しません')
            with self.runtime._transaction(token['instanceId']) as connection:
                _, current, current_close = closed(connection)
                require(current.get('ownerExit') == observation and current_close == closing,
                        'STALE_OPERATION', 'C終了結果の最終正本照合が一致しません')
                current['ownerExit'] = dict(observation, responseSha256=digest(controller.owner_exit_response))
                encoded = json.dumps(current)
                controller._verify_close_binding(deadline)
                require(canonical(response) == controller.owner_exit_response
                        and controller.record.get('ownerExit') == response,
                        'STALE_OPERATION', '最終更新前に元C終了原文が変更されました')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C終了結果の更新前に期限を超過しました')
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?', (encoded, opened['operationId']))
            controller.verify_owner()
            controller._verify_close_binding(deadline)
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C終了結果の記録後に期限を超過しました')
            return response

    def complete(self, token, *, operation_id, deadline):
        """記録済み元W終了証拠からC本人completeを接続し、応答hashを正本へ記録する。

        例外後も元意図・C/W・予約を保全し、再送しない。戻り値はData確定応答であり、
        writer CLOSEDや次W生成の許可ではない。要求/応答本文はRegistryへ保存しない。
        """
        fields(token, TOKEN)
        _canonical_uuid(operation_id)
        token = deepcopy(token)
        controller, worker = self.controllers.get(token['instanceId']), self.workers.get(token['instanceId'])
        require(controller is not None and worker is not None, 'WRITER_PENDING', '保持した元C/Wが必要です')
        completion = {'operationId':operation_id}
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            with self.runtime._transaction(token['instanceId']) as connection:
                opened, saved = self._exited(connection, token, controller, worker, operation_id)
                require('completion' not in saved, 'WRITER_PENDING', '元completion意図と結果を保全します')
                completion['workerExitSha256'] = digest(worker.exit_response)
                saved['completion'] = completion
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (json.dumps(saved),opened['operationId']))

            def verify():
                with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                    _, current = self._exited(connection, token, controller, worker, operation_id)
                    require(current.get('completion') == completion, 'STALE_OPERATION', '元completion意図が変更されました')

            response = controller.complete(worker, deadline, verify)
            verify()
            controller.verify()
            with self.runtime._transaction(token['instanceId']) as connection:
                _, current = self._exited(connection, token, controller, worker, operation_id)
                require(current.get('completion') == completion, 'STALE_OPERATION', '元completion意図が変更されました')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C候補確定の正本照合中に期限を超過しました')
                recorded = dict(completion, responseSha256=digest(canonical(response)))
                current['completion'] = recorded
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (json.dumps(current),opened['operationId']))
            completion = recorded
            controller.acknowledge_completion(completion['responseSha256'], deadline, verify)
            return response

    def observe_worker_exit(self, token, *, operation_id, deadline):
        """記録済み候補のW/全child正常終了を正本へ記録する。C complete/結果終端は行わない。"""
        fields(token, TOKEN)
        _canonical_uuid(operation_id)
        token = deepcopy(token)
        controller, worker = self.controllers.get(token['instanceId']), self.workers.get(token['instanceId'])
        require(controller is not None and worker is not None, 'WRITER_PENDING', '保持した元C/Wが必要です')
        observation = {'operationId':operation_id}
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            with self.runtime._transaction(token['instanceId']) as connection:
                opened, saved = self._candidate(connection, token, controller, worker, operation_id)
                require('workerExit' not in saved, 'WRITER_PENDING', '元Wの終了観測意図と結果を保全します')
                saved['workerExit'] = observation
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (json.dumps(saved),opened['operationId']))

            def verify():
                with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                    _, current = self._candidate(connection, token, controller, worker, operation_id)
                    require(current.get('workerExit') == observation, 'STALE_OPERATION', '元Wの終了観測意図が変更されました')

            response = worker.observe_exit(deadline, verify)
            verify()
            controller.verify()
            with self.runtime._transaction(token['instanceId']) as connection:
                _, current = self._candidate(connection, token, controller, worker, operation_id)
                require(current.get('workerExit') == observation, 'STALE_OPERATION', '元Wの終了観測意図が変更されました')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W終了観測の正本照合中に期限を超過しました')
                current['workerExit'] = dict(observation, responseSha256=digest(canonical(response)))
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (json.dumps(current),opened['operationId']))
            return response

    def admit(self, token, *, operation_id, deadline):
        """保持Assignedを元Wへadmitし候補packetを記録する。実終了/receipt確定は行わない。"""
        fields(token, TOKEN)
        _canonical_uuid(operation_id)
        token = deepcopy(token)
        controller, worker = self.controllers.get(token['instanceId']), self.workers.get(token['instanceId'])
        require(controller is not None and worker is not None, 'WRITER_PENDING', '保持した元C/Wが必要です')
        admission = {'operationId':operation_id}
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            with self.runtime._transaction(token['instanceId']) as connection:
                opened, saved = self._assigned(connection, token, controller, worker, operation_id)
                require('admission' not in saved, 'WRITER_PENDING', '元admission意図と候補を保全します')
                saved['admission'] = admission
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (json.dumps(saved),opened['operationId']))

            def verify():
                with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                    _, current = self._assigned(connection, token, controller, worker, operation_id)
                    require(current.get('admission') == admission, 'STALE_OPERATION', '元admission意図が変更されました')

            response = worker.admit(deadline, verify)
            verify()
            controller.verify()
            with self.runtime._transaction(token['instanceId']) as connection:
                _, current = self._assigned(connection, token, controller, worker, operation_id)
                require(current.get('admission') == admission, 'STALE_OPERATION', '元admission意図が変更されました')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W候補の正本照合中に期限を超過しました')
                current['admission'] = dict(admission, candidateResponseSha256=digest(canonical(response)))
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (json.dumps(current),opened['operationId']))
            return response

    def assign(self, token, *, operation_id, deadline):
        """保持WのPREPARED原文を正本照合しC本人へ登録する。本文差替・再登録は行わない。"""
        fields(token, TOKEN)
        _canonical_uuid(operation_id)
        token = deepcopy(token)
        controller, worker = self.controllers.get(token['instanceId']), self.workers.get(token['instanceId'])
        require(controller is not None and worker is not None, 'WRITER_PENDING', '保持した元C/Wが必要です')
        assignment = {'operationId':operation_id}
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            with self.runtime._transaction(token['instanceId']) as connection:
                opened, saved = self._prepared(connection, token, controller, worker, operation_id)
                require('assignment' not in saved, 'WRITER_PENDING', '元assignment意図と結果を保全します')
                saved['assignment'] = assignment
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (json.dumps(saved),opened['operationId']))

            def verify():
                with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                    _, current = self._prepared(connection, token, controller, worker, operation_id)
                    require(current.get('assignment') == assignment, 'STALE_OPERATION', '元assignment意図が変更されました')

            response = controller.assign(worker, deadline, verify)
            verify()
            with self.runtime._transaction(token['instanceId']) as connection:
                _, current = self._prepared(connection, token, controller, worker, operation_id)
                require(current.get('assignment') == assignment, 'STALE_OPERATION', '元assignment意図が変更されました')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C Assigned登録の正本照合中に期限を超過しました')
                recorded = dict(assignment, responseSha256=digest(canonical(response)))
                current['assignment'] = recorded
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (json.dumps(current),opened['operationId']))
            assignment = recorded
            controller.acknowledge_assignment(assignment['responseSha256'], worker, verify, deadline=deadline)
            return response

    def prepare(self, token, *, operation_id, request, deadline):
        """記録済みOPEN又は前操作のIdleから独立Wを一度生成する。DBは開かない。

        requestはData HostAdapterCodecの型付きEdit本文。元Cのworldと同じ要求IDを渡し、
        Dataの正規化/hash算法は使わずIPC fingerprintだけを記録する。失敗後は再生成しない。
        """
        fields(token, TOKEN)
        _canonical_uuid(operation_id)
        token, request = deepcopy(token), deepcopy(request)
        controller = self.controllers.get(token['instanceId'])
        require(controller is not None and type(request) is dict,
                'WRITER_PENDING', '元Cと型付き要求が必要です')
        fingerprint = digest(canonical({'command':'offline-prepare','request':request}))
        worker_record = {'operationId':operation_id,'requestSha256':fingerprint}
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            with self.runtime._transaction(token['instanceId']) as connection:
                opened, response, saved = self._opened(connection, token, controller)
                require(request.get('requestId') == operation_id and request.get('worldUuid') == opened['worldUuid'],
                        'INVALID_INPUT', '元worldの操作IDと要求が一致しません')
                previous = self.workers.get(token['instanceId'])
                history = [record for _, record in self.history.get(token['instanceId'], [])]
                require(saved.get('history', []) == history, 'STALE_OPERATION', '元操作履歴が一致しません')
                if previous is None:
                    require('worker' not in saved, 'WRITER_PENDING', '元Wの生成意図を保全します')
                else:
                    require(previous.ready, 'WRITER_PENDING', '未完了Wの生成資源を保全します')
                    require(time.monotonic() < deadline, 'STOP_TIMEOUT', '次W準備の期限を超過しました')
                    previous_id = json.loads(previous.request)['operationId']
                    self._exited(connection, token, controller, previous, previous_id)
                    response_sha256 = digest(controller.completion_response) if controller.completion_response is not None else None
                    require(controller.completion_ready and controller.completion_acknowledged
                            and saved.get('completion') == {'operationId':previous_id,
                                'workerExitSha256':digest(previous.exit_response), 'responseSha256':response_sha256}
                            and controller.record.get('completionResponseSha256') == response_sha256
                            and controller.record.get('completionAcknowledgedSha256') == response_sha256,
                            'WRITER_PENDING', '元Cの照合済み確定応答と正本記録/ACKが必要です')
                    response = json.loads(controller.completion_response)
                    require(response['operationId'] == previous_id and response['status'] == 'COMPLETED',
                            'STALE_OPERATION', '元操作のIdle応答が一致しません')
                require(all(record['worker']['operationId'] != operation_id for record in history)
                        and (previous is None or previous_id != operation_id),
                        'OPERATION_CONFLICT', '過去のoffline operationIdを再使用しません')
                for table in ('operations','runtime_operations','reference_operations','writer_operations','snapshot_operations'):
                    require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (operation_id,)).fetchone(),
                            'OPERATION_CONFLICT', '既存operationIdを再使用しません')
                if previous is not None:
                    archived = {key:saved.pop(key) for key in ('worker','assignment','admission','workerExit','completion')}
                    history = deepcopy(history) + [archived]
                    saved['history'] = history
                saved['worker'] = worker_record
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (json.dumps(saved),opened['operationId']))

            if previous is not None:
                self.history.setdefault(token['instanceId'], []).append((previous, deepcopy(history[-1])))
                controller.archive_completed(previous)

            def verify():
                with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                    _, _, current = self._opened(connection, token, controller)
                    require(current.get('worker') == worker_record and current.get('history', []) == history
                            and history == [record for _, record in self.history.get(token['instanceId'], [])],
                            'STALE_OPERATION', '元Wの予約又は操作履歴が変更されました')

            verify()
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W生成前に期限を超過しました')
            if previous is None:
                controller.acknowledge_startup(saved['startupResponseSha256'], deadline=deadline)
            worker = OfflineProcess(controller.bundle, controller.diagnostics / ('worker-' + operation_id),
                                    native=controller.native, verify=verify, controller=controller,
                                    process_adapter=self.runtime.process_adapter)
            self.workers[token['instanceId']] = worker
            frame = {key:opened[key] for key in ('leaseId','epoch','generation','drainSha256',
                                                'worldRoot','shopDirectory','worldUuid')}
            frame.update(operationId=operation_id,requestHash=fingerprint,request=request,
                         expectedStateSha256=response['stateSha256'],expectedRevision=response['stateRevision'],
                         runUuid=response['runUuid'])
            prepared = worker.start(frame, deadline)
            verify()
            with self.runtime._transaction(token['instanceId']) as connection:
                _, _, current = self._opened(connection, token, controller)
                require(current.get('worker') == worker_record and current.get('history', []) == history
                        and history == [record for _, record in self.history.get(token['instanceId'], [])],
                        'STALE_OPERATION', '元Wの予約又は操作履歴が変更されました')
                completed_record = dict(worker_record, startupResponseSha256=digest(canonical(prepared)))
                current['worker'] = completed_record
                recorded_response = json.dumps(current)
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W起動結果の更新前に期限を超過しました')
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (recorded_response,opened['operationId']))
            worker_record = completed_record
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W起動結果の記録後に期限を超過しました')
            worker.acknowledge_startup(worker_record['startupResponseSha256'], deadline=deadline)
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W起動ACK後に期限を超過しました')
            return prepared

    def _game_observation(self, connection, row, gate, proof, deadline):
        """確定stopと保持した観測原本を照合する。Readyの承認とOS再観測は行わない。"""
        if 'gameExit' not in proof:
            return
        slot, originals, binding = self.runtime._game_exit(row['run_id'], proof['stopOperationId'])
        stop = connection.execute('SELECT fingerprint FROM runtime_operations WHERE id=?',
                                  (proof['stopOperationId'],)).fetchone()
        require(binding == {'instanceId':row['id'], 'runId':row['run_id'],
                            'stopOperationId':proof['stopOperationId'], 'stopFingerprint':stop['fingerprint'],
                            'controlOperationId':proof['controlOperationId']}
                and slot['stop'] == (proof['stopOperationId'], stop['fingerprint'], proof['controlOperationId']),
                'STALE_OPERATION', 'Ready供給元の終端資源が元stopと一致しません')
        saved = proof['managedSave']
        require(digest(originals['descriptor']) == saved['descriptorSha256'] == gate['descriptor_hash']
                and digest(originals['save_request'][4:]) == saved['requestBodySha256']
                and digest(originals['completion_response'][4:]) == saved['responseBodySha256'],
                'STALE_OPERATION', 'Ready供給元の認証済原文が変更されました')
        observer = slot['observer']
        observed = json.loads(slot['result'])
        require(proof['gameExit'] == {'candidate':slot['candidate'], 'observationSha256':digest(slot['result']),
                    'requestSha256':digest(observer.request), 'responseSha256':digest(observer.game_exit_response),
                    'observerRecordSha256':digest(slot['observer_snapshot'])}
                and observer.game_exit_result == slot['result']
                and canonical(observer.record) == slot['observer_snapshot']
                and observed == observer.record['gameExit'],
                'STALE_OPERATION', 'Ready供給元の確定参照と観測原本が一致しません')
        response = json.loads(observer.game_exit_response)
        require(observed['status'] == 'GAME_EXIT_OBSERVED' and observed['operationId'] == proof['stopOperationId']
                and type(observed['exitCode']) is int and observed['exitCode'] == 0
                and observed['allChildrenExited'] is True
                and observed['requestSha256'] == proof['gameExit']['requestSha256']
                and observed['responseSha256'] == proof['gameExit']['responseSha256']
                and response['candidateStateSha256'] == slot['candidate']['stateSha256']
                and canonical(observed['ready']) == canonical({key:response[key] for key in ('worldUuid','runUuid','stateSha256','stateRevision')}),
                'PROOF_MISMATCH', '独立明示Readyが同stopの正常退出応答と一致しません')
        if deadline is not None:
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'Ready供給の最終照合後に期限を超過しました')
        return slot

    def _game_ready(self, connection, row, gate, proof, ready, deadline):
        """確定stopの観測原本と独立明示Readyを照合する。OS観測とData作用は呼出元が担う。"""
        slot = self._game_observation(connection, row, gate, proof, deadline)
        if slot is not None:
            require(canonical(ready) == canonical(json.loads(slot['result'])['ready']),
                    'PROOF_MISMATCH', '独立明示Readyが同stopの正常退出応答と一致しません')
            if deadline is not None:
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'Ready供給の最終照合後に期限を超過しました')

    def release_game_handles(self, token, *, operation_id, stop_operation_id, deadline):
        """確定stopの元M/観測Cを一度だけ解放する。部分失敗も元objectとPENDING予約を保全する。"""
        fields(token, TOKEN)
        identifier(operation_id, 'operationId')
        identifier(stop_operation_id, 'stopOperationId')
        token = deepcopy(token)
        fingerprint = digest(canonical({'command':'game-handle-release','token':token,'stopOperationId':stop_operation_id}))
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                row, gate, run, proof, writers = self._state(connection, token, reserved=False, release_operation_id=operation_id)
                require(proof.get('stopOperationId') == stop_operation_id and 'gameExit' in proof,
                        'PROOF_REQUIRED', '同stopのgame退出確定証拠が必要です')
                slot = self._game_observation(connection, row, gate, proof, deadline)
            process, snapshot = self.runtime._game_process(run)
            observer = slot['observer_process']
            pipe = slot['observer'].pipe
            require(row['id'] not in self.controllers and 'handle_release' not in slot,
                    'PROCESS_MISMATCH', '元M/Cの保持本人または解放枠が一致しません')
            expected = {'runId':row['run_id'],'stopOperationId':stop_operation_id,'revision':gate['revision'],
                        'stopProofSha256':digest(canonical(proof)), 'runRecordSha256':digest(canonical(run)),
                        'descriptorSha256':gate['descriptor_hash'],'writers':writers,
                        'gameSnapshotSha256':digest(slot['snapshot']), 'observerRecordSha256':digest(slot['observer_snapshot'])}

            def checked(connection, reserved):
                current, current_gate, current_run, current_proof, current_writers = self._state(connection, token, reserved=reserved, release_operation_id=operation_id)
                self.runtime._binding(current, row)
                require(current_gate == gate and current_run == run and current_proof == proof and current_writers == writers
                        and self._game_observation(connection, current, current_gate, current_proof, deadline) is slot,
                        'STALE_OPERATION', 'handle解放中に元停止証拠が変更されました')
                held, born = self.runtime._game_process(current_run)
                observed = json.loads(slot['result'])
                controller = slot['observer']
                request = json.loads(controller.request)
                require(held is process is slot['process'] and canonical(born) == slot['snapshot'] == canonical(snapshot)
                        and snapshot['owner'] == observed['gameOwner'] and process.group == observed['gameGroup']
                        and controller.process is observer is slot['observer_process'] and observer is not process
                        and controller.pipe is pipe
                        and controller.identity == controller.record['identity']
                        and controller.data_identity == controller.record['controlIdentity'] == observed['observer'] == request['owner']
                        and observer.pid == controller.identity['pid'] == controller.data_identity['pid'] == controller.record['creation']['pid']
                        and observer.group == controller.record['creation']['ownerGroup'] == observed['observerGroup']
                        and observer.group != process.group,
                        'PROCESS_MISMATCH', 'handle解放中に元M/Cの保持資源が変更されました')
                if not reserved:
                    require('handle_release' not in slot and row['id'] not in self.controllers,
                            'WRITER_PENDING', '元M/Cの単発解放は開始済みです')
                if reserved:
                    require(slot.get('handle_release') is progress, 'STALE_OPERATION', '元handle解放の進捗保持先が変更されました')
                    operation = connection.execute('SELECT * FROM runtime_operations WHERE id=?', (operation_id,)).fetchone()
                    refs = connection.execute('SELECT * FROM reservations WHERE instance=?', (row['id'],)).fetchall()
                    require(operation is not None and operation['instance'] == row['id']
                            and operation['command'] == 'game-handle-release' and operation['fingerprint'] == fingerprint
                            and operation['status'] == 'PENDING' and json.loads(operation['response']) == {'binding':expected}
                            and len(refs) == 1 and refs[0]['operation'] == operation_id
                            and refs[0]['reference'] == 'artifact:' + row['artifact']
                            and refs[0]['lease'] == row['lease'] and refs[0]['epoch'] == row['epoch'],
                            'STALE_OPERATION', '元handle解放予約が変更されました')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'handle解放の最終照合後に期限を超過しました')

            with self.runtime._transaction(row['id']) as connection:
                checked(connection, False)
                for table in ('operations','runtime_operations','reference_operations','writer_operations','snapshot_operations'):
                    require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (operation_id,)).fetchone(),
                            'OPERATION_CONFLICT', 'handle解放を再実行しません')
                require(not connection.execute('SELECT 1 FROM reservations WHERE instance=?', (row['id'],)).fetchone()
                        and not connection.execute("SELECT 1 FROM runtime_operations WHERE instance=? AND status!='COMPLETED'", (row['id'],)).fetchone(),
                        'WRITER_PENDING', '未完了操作の資源を保全します')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'handle解放予約前に期限を超過しました')
                connection.execute("INSERT INTO runtime_operations VALUES (?,?,'game-handle-release',?,'PENDING',?)",
                                   (operation_id,fingerprint,row['id'],json.dumps({'binding':expected})))
                connection.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',
                                   (operation_id,'artifact:'+row['artifact'],row['id'],row['lease'],row['epoch']))
                connection.execute('UPDATE instances SET pending=1 WHERE id=?', (row['id'],))
            progress = {'operationId':operation_id,'fingerprint':fingerprint,'m':'NOT_STARTED','c':'NOT_STARTED'}
            slot['handle_release'] = progress
            def verify_pipe():
                with self.runtime._transaction(row['id'], read_only=True) as connection:
                    checked(connection, True)
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', '親側pipe解放直前に期限を超過しました')

            for role, owned in (('m',process),('c',observer)):
                with self.runtime._transaction(row['id'], read_only=True) as connection:
                    checked(connection, True)
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', '元handle解放直前に期限を超過しました')
                progress[role] = 'UNKNOWN'
                owned.close()
                if role == 'c':
                    pipe.close_after_exit(verify=verify_pipe)
                progress[role] = 'RELEASED'
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', '元handle解放後に期限を超過しました')
            result = {'instanceId':row['id'],'runId':row['run_id'],'stopOperationId':stop_operation_id,
                      'handlesReleased':True,'binding':expected}
            with self.runtime._transaction(row['id']) as connection:
                checked(connection, True)
                saved = json.dumps(result)
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'handle解放結果更新前に期限を超過しました')
                connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=?", (saved,operation_id))
                connection.execute('UPDATE instances SET pending=0 WHERE id=?', (row['id'],))
                connection.execute('DELETE FROM reservations WHERE operation=?', (operation_id,))
            progress['completed'] = True
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'handle解放結果記録後に期限を超過しました')
            return result


    def open(self, token, *, operation_id, revision, ready, deadline):
        """同leaseの認証run正常停止後だけCを取得する。例外後も予約と元Cを保全する。

        readyはData ReadyのworldUuid/runUuid/stateSha256/stateRevisionの期待値で、Cが実stateを照合する。
        deadlineはmonotonic期限。OPENを返しても予約は解放せず、writer終了を表さない。
        """
        fields(token, TOKEN)
        identifier(operation_id, 'operationId')
        fields(ready, {'worldUuid','runUuid','stateSha256','stateRevision'})
        for key in ('worldUuid','runUuid'): _canonical_uuid(ready[key])
        _hash(ready['stateSha256'])
        require(type(ready['stateRevision']) is int and ready['stateRevision'] >= 0,
                'INVALID_INPUT', 'Data revisionが不正です')
        token, ready = deepcopy(token), deepcopy(ready)
        require(self.runtime.managed is not None and token['instanceId'] not in self.controllers,
                'WRITER_PENDING', '認証nativeと未使用のoffline枠が必要です')
        fingerprint = digest(canonical({'command':'offline-open','token':token,'revision':revision,'ready':ready}))
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                row, gate, run, proof, writers = self._state(connection, token, reserved=False)
                self._game_ready(connection, row, gate, proof, ready, deadline)
                require(type(revision) is int and gate['revision'] == revision,
                        'STALE_OPERATION', '閉門revisionが変更されました')
            generation = self.runtime._active_generation(row)
            bundle = generation / 'runtime'
            manifest = verify_bundle(bundle)
            require(manifest['artifactId'] == row['artifact'], 'ARTIFACT_MISMATCH', '固定artifactが正本と一致しません')
            require_data_generation(manifest, row['generation'])
            settings = properties(portable_properties(regular_bytes(generation / 'server.properties')))
            world = generation / settings['level-name']
            shop = generation / 'config/SushiEricServerMod/shop_data'
            plain_path(world)
            plain_path(shop)
            require(self.runtime.process_adapter.identity(run['identity']['pid']) is None,
                    'PROCESS_MISMATCH', '旧MのPIDが存在します。再利用も自動許可しません')
            expected = {'runId':row['run_id'],'revision':revision+1,'stopProofSha256':digest(canonical(proof)),
                        'writers':writers,'ready':ready,'descriptorSha256':gate['descriptor_hash'],
                        'runRecordSha256':digest(canonical(run))}
            with self.runtime._transaction(row['id']) as connection:
                current, current_gate, current_run, current_proof, current_writers = self._state(connection, token, reserved=False)
                self.runtime._binding(current, row)
                require(current_gate == gate and current_proof == proof and current_run == run and current_writers == writers,
                        'STALE_OPERATION', '予約前に停止/drain証拠が変更されました')
                self._game_ready(connection, current, current_gate, current_proof, ready, deadline)
                for table in ('operations','runtime_operations','reference_operations','writer_operations','snapshot_operations'):
                    require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (operation_id,)).fetchone(),
                            'OPERATION_CONFLICT', '同じoperationIdを再取得・再作用しません')
                require(not connection.execute('SELECT 1 FROM reservations WHERE instance=?', (row['id'],)).fetchone()
                        and not connection.execute("SELECT 1 FROM runtime_operations WHERE instance=? AND status!='COMPLETED'",
                                                   (row['id'],)).fetchone(), 'WRITER_PENDING', '旧予約と未完了操作を保全します')
                if 'gameExit' in proof:
                    require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'Ready取得予約前に期限を超過しました')
                _change(connection, current, revision, 'BLOCKED')
                connection.execute("INSERT INTO runtime_operations VALUES (?,?,'offline-open',?,'PENDING',?)",
                                   (operation_id,fingerprint,row['id'],json.dumps({'binding':expected})))
                connection.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',
                                   (operation_id,'artifact:'+row['artifact'],row['id'],row['lease'],row['epoch']))
                connection.execute('UPDATE instances SET pending=1,clean_proof=NULL WHERE id=?', (row['id'],))
                connection.execute("INSERT INTO audit(at,operation,command) VALUES (?,?,'offline-open')",
                                   (self.registry.clock(),operation_id))

            def verify_binding():
                with self.runtime._transaction(row['id'], read_only=True) as connection:
                    previous = self._reserved(connection,token,operation_id,fingerprint,expected)
                    self._game_ready(connection, row, gate, proof, ready, None)
                require(self.runtime.process_adapter.identity(previous['identity']['pid']) is None,
                        'PROCESS_MISMATCH', '旧MのPIDが存在します。結果不明を保全します')

            def verify():
                verify_binding()
                if 'gameExit' in proof:
                    require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'Ready取得のPID照会後に期限を超過しました')

            diagnostics = self.runtime._paths(row['id']) / 'diagnostics' / ('offline-' + operation_id)
            controller = OfflineProcess(bundle,diagnostics,native=self.runtime.managed.transport.native,
                                        verify=verify_binding,process_adapter=self.runtime.process_adapter)
            self.controllers[row['id']] = controller
            frame = {'operationId':operation_id,'requestHash':fingerprint,'leaseId':row['lease'],'epoch':row['epoch'],
                     'generation':row['generation'],'drainSha256':digest(canonical(expected)),
                     'worldRoot':str(world),'shopDirectory':str(shop),'worldUuid':ready['worldUuid'],'runUuid':ready['runUuid'],
                     'expectedStateSha256':ready['stateSha256'],'expectedRevision':ready['stateRevision']}
            response = controller.start(frame,deadline)
            verify()
            with self.runtime._transaction(row['id']) as connection:
                self._reserved(connection,token,operation_id,fingerprint,expected)
                self._game_ready(connection, row, gate, proof, ready, deadline)
                saved = json.dumps({'binding':expected,'startupResponseSha256':digest(canonical(response))})
                if 'gameExit' in proof:
                    require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'Ready取得結果の更新前に期限を超過しました')
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?', (saved,operation_id))
            if 'gameExit' in proof:
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'Ready取得結果の記録後に期限を超過しました')
            return response

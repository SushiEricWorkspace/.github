"""停止済み管理runの正本予約・候補・元W終了証拠とC本人確定を接続する。"""

from copy import deepcopy
import json
import time
import uuid

from .build_inputs import canonical, digest, regular_bytes
from .mod_control_runtime import _closed_writers
from .local_ipc import make_private, plain_path
from .snapshot_data import portable_properties, properties
from .mod_control_startup import _admission, _change
from .mod_control_wire import _canonical_uuid, _hash, _json, descriptor_path
from .offline_process import OfflineProcess
from .registry import fields, identifier, require
from .runtime_bundle import verify_bundle, require_data_generation
from .runtime_control import TOKEN
from . import mod_control_operations as operations, runtime_process
from .runtime_ports import instance_endpoints, require_listeners, reserve


def _check_ready(ready):
    fields(ready, {'worldUuid', 'runUuid', 'stateSha256', 'stateRevision'})
    for name in ('worldUuid', 'runUuid'):
        _canonical_uuid(ready[name])
    _hash(ready['stateSha256'])
    require(type(ready['stateRevision']) is int and 0 <= ready['stateRevision'] <= 9223372036854775807,
            'INVALID_INPUT', 'Ready revisionが範囲外です')


class _OfflineOpen:
    def __init__(self, runtime):
        self.runtime = runtime
        self.registry = runtime.registry
        self.controllers = {}
        self.workers = {}
        self.history = {}
        self.retired = {}
        self.readers = {}
        self.starts = {}
        self.start_records = {}
        self.close_recoveries = {}
        self.close_preparations = {}
        self.release_failures = {}

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

    def _closed_owner(self, connection, token, controller, operation_id, deadline):
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
            return self._closed_owner(connection, token, controller, operation_id, deadline)

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

    def _owner_exited(self, connection, token, controller, operation_id, deadline):
        opened, saved, closing = self._closed_owner(connection, token, controller, operation_id, deadline)
        require(controller.owner_exit_started and controller.owner_exit_response is not None,
                'PROOF_REQUIRED', '元Cの確定した終了原文が必要です')
        response = json.loads(controller.owner_exit_response)
        require(saved.get('ownerExit') == dict(operationId=operation_id, closeResponseSha256=closing['responseSha256'],
                    responseSha256=digest(controller.owner_exit_response))
                and controller.record.get('ownerExit') == response
                and response.keys() == {'status','operationId','closeRequestSha256','closeResponseSha256',
                                        'owner','ownerGroup','exitCode','allChildrenExited'}
                and response['status'] == 'OWNER_EXIT_OBSERVED' and response['operationId'] == operation_id
                and response['closeRequestSha256'] == digest(controller.close_request)
                and response['closeResponseSha256'] == digest(controller.close_response)
                and response['owner'] == controller.data_identity
                and response['ownerGroup'] == controller.process.group
                and type(response['exitCode']) is int and response['exitCode'] == 0
                and response['allChildrenExited'] is True,
                'PROOF_MISMATCH', '元C終了原文・生成bindingと正本hashが一致しません')
        controller._verify_close_binding(deadline)
        return opened, saved

    def release_owner_handles(self, token, *, operation_id, deadline):
        """確定した元C/全child終了から同handle/pipeを一度だけ正常解放する。

        close時固定期限と呼出期限のminを使う。各資源の部分失敗はUNKNOWN/RELEASEDと
        原文・元object・予約を保持して伝播し、再close/再観測しない。正常結果もCLOSEDや
        予約終端を意味しない。元W等の資源解放は後続で行う。
        """
        fields(token, TOKEN)
        identifier(operation_id, 'operationId')
        token = deepcopy(token)
        controller = self.controllers.get(token['instanceId'])
        require(controller is not None and controller.close_binding is not None,
                'WRITER_PENDING', '元Cとclose時固定bindingが必要です')
        deadline = min(deadline, controller.close_binding['deadline'])
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            with self.runtime._transaction(token['instanceId']) as connection:
                opened, saved = self._owner_exited(connection, token, controller, operation_id, deadline)
                require('ownerRelease' not in saved and controller.owner_release is None,
                        'WRITER_PENDING', '元Cの解放を再実行しません')
                release_record = dict(operationId=operation_id, ownerExitSha256=digest(controller.owner_exit_response))
                saved['ownerRelease'] = release_record
                encoded = json.dumps(saved)
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C解放意図の更新前に期限を超過しました')
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?', (encoded, opened['operationId']))
            progress = dict(handle='NOT_STARTED', pipe='NOT_STARTED')
            controller.owner_release = progress
            process, pipe = controller.process, controller.pipe

            def checked(connection):
                _, current = self._owner_exited(connection, token, controller, operation_id, deadline)
                require(current.get('ownerRelease') == release_record and controller.owner_release is progress
                        and digest(controller.owner_exit_response) == release_record['ownerExitSha256'],
                        'STALE_OPERATION', '元C解放意図・終了原文・進捗保持先が変更されました')
                return current

            def verify():
                controller.verify_owner()
                with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                    checked(connection)
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C資源解放直前に期限を超過しました')

            verify()
            progress['handle'] = 'UNKNOWN'
            process.close()
            progress['handle'] = 'RELEASED'
            verify()
            progress['pipe'] = 'UNKNOWN'
            pipe.close_after_exit(verify=verify)
            require(pipe.input is None and pipe.output is None,
                    'WRITER_PENDING', '元Cの親側pipe解放が完了していません')
            progress['pipe'] = 'RELEASED'
            verify()
            result = dict(status='OWNER_HANDLES_RELEASED', operationId=operation_id,
                          openOperationId=opened['operationId'], ownerExitSha256=release_record['ownerExitSha256'],
                          handlesReleased=True)
            controller.owner_release_response = canonical(result)
            verify()
            controller.record['ownerRelease'] = deepcopy(result)
            controller._record()
            verify()

            def finished():
                require(progress == {'handle':'RELEASED','pipe':'RELEASED'}
                        and canonical(result) == controller.owner_release_response
                        and controller.record.get('ownerRelease') == result,
                        'PROOF_MISMATCH', '元C解放の原文・記録・進捗が一致しません')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C解放結果の照合後に期限を超過しました')

            with self.runtime._transaction(token['instanceId']) as connection:
                current = checked(connection)
                finished()
                recorded = dict(release_record, responseSha256=digest(controller.owner_release_response))
                current['ownerRelease'] = recorded
                encoded = json.dumps(current)
                checked(connection)
                finished()
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C解放結果の更新前に期限を超過しました')
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?', (encoded, opened['operationId']))
            release_record = recorded
            returned = deepcopy(result)
            verify()
            finished()
            return returned

    def _owner_released(self, connection, token, controller, operation_id, deadline):
        opened, saved = self._owner_exited(connection, token, controller, operation_id, deadline)
        require(controller.owner_release_response is not None,
                'PROOF_REQUIRED', '元Cの正常解放原文が必要です')
        response = json.loads(controller.owner_release_response)
        require(controller.owner_release == {'handle':'RELEASED','pipe':'RELEASED'}
                and controller.pipe.input is None and controller.pipe.output is None
                and response == dict(status='OWNER_HANDLES_RELEASED', operationId=operation_id,
                    openOperationId=opened['operationId'], ownerExitSha256=digest(controller.owner_exit_response),
                    handlesReleased=True) and response['handlesReleased'] is True
                and controller.record.get('ownerRelease') == response
                and saved.get('ownerRelease') == dict(operationId=operation_id,
                    ownerExitSha256=digest(controller.owner_exit_response),
                    responseSha256=digest(controller.owner_release_response)),
                'PROOF_MISMATCH', '元C解放の原文・正本hash・RELEASED進捗が一致しません')
        return opened, saved

    def _worker_release_bindings(self, token, controller, opened, saved):
        workers = list(self.history.get(token['instanceId'], []))
        latest = self.workers.get(token['instanceId'])
        if latest is not None:
            workers.append((latest, saved))
        held, bindings = [], []
        for worker, record in workers:
            frame = json.loads(worker.request)
            operation_id = frame['operationId']
            archived = controller.operation_history.get(operation_id)
            assigned = json.loads(archived['assignment_request'] if archived else controller.assignment_request)
            exited = json.loads(worker.exit_response)
            prepared = json.loads(worker.response)
            require(worker.controller is controller and worker.startup_acknowledged and worker.exit_started
                    and worker.candidate_ready and worker.creation_resources is not None
                    and worker.creation_resources[0] is worker.process and worker.creation_resources[1] is worker.pipe
                    and worker.parent == controller.close_binding['parent']
                    and worker.parent_data == controller.close_binding['parent_data']
                    and worker.record['identity'] == worker.identity
                    and worker.record['controlIdentity'] == worker.data_identity
                    and worker.record['commandHash'] == worker.identity['commandHash']
                    and worker.identity['owner'] == worker.parent['owner']
                    and worker.identity['startId'] == worker.data_identity['startId']
                    and worker.process.pid == worker.identity['pid'] == worker.data_identity['pid'] == worker.record['creation']['pid']
                    and worker.process.pid != controller.process.pid
                    and worker.process.group == worker.record['creation']['ownerGroup'] != controller.process.group,
                    'PROCESS_MISMATCH', '元Wの生成資源・P/C・本人bindingが一致しません')
            require(record['worker'] == dict(operationId=operation_id, requestSha256=frame['requestHash'],
                        startupResponseSha256=digest(worker.response))
                    and record['admission'] == dict(operationId=operation_id,
                        candidateResponseSha256=digest(worker.candidate_response))
                    and worker.record.get('candidateResponseSha256') == digest(worker.candidate_response)
                    and record['workerExit'] == dict(operationId=operation_id, responseSha256=digest(worker.exit_response))
                    and worker.record.get('workerExit') == exited
                    and exited == dict(status='WORKER_EXITED', operationId=operation_id,
                        candidateResponseSha256=digest(worker.candidate_response), worker=worker.data_identity,
                        ownerGroup=worker.process.group, exitCode=0, allChildrenExited=True)
                    and type(exited['exitCode']) is int and exited['allChildrenExited'] is True
                    and all(frame[k] == opened[k] for k in ('leaseId','epoch','generation','drainSha256',
                                                           'worldRoot','shopDirectory','worldUuid'))
                    and frame['controller'] == worker.parent_data and frame['owner'] == controller.data_identity
                    and frame['command'] == 'prepare' and frame['worker'] == worker.data_identity
                    and frame['sessionId'] == controller.session and frame['nonce'] == controller.nonce
                    and frame['requestHash'] == digest(canonical({'command':'offline-prepare','request':frame['request']}))
                    and prepared == dict(status='PREPARED', **{k:frame[k] for k in
                        ('operationId','requestHash','sessionId','nonce','runUuid')},
                        stateSha256=frame['expectedStateSha256'], stateRevision=frame['expectedRevision'])
                    and worker.record.get('startupResponseSha256') == worker.record.get('startupAcknowledgedSha256') == digest(worker.response)
                    and all(assigned[k] == frame[k] for k in ('operationId','requestHash','sessionId','nonce',
                        'expectedStateSha256','expectedRevision','runUuid','request'))
                    and assigned['command'] == 'assign' and assigned['openOperationId'] == opened['operationId']
                    and assigned['worker'] == worker.data_identity,
                    'PROOF_MISMATCH', '元W終了原文・候補・割当原文と正本hashが一致しません')
            require(all(worker is not previous and worker.process is not previous.process
                        and worker.pipe is not previous.pipe for previous in held)
                    and worker.process is not controller.process and worker.pipe is not controller.pipe
                    and all(binding['operationId'] != operation_id for binding in bindings),
                    'PROCESS_MISMATCH', '全Wの操作と保持資源は独立である必要があります')
            held.append(worker)
            bindings.append(dict(operationId=operation_id, requestSha256=digest(worker.request),
                assignmentSha256=digest(canonical(assigned)), workerExitSha256=digest(worker.exit_response),
                creationSha256=digest(canonical(worker.record['creation']))))
        return held, bindings

    def release_worker_handles(self, token, *, operation_id, deadline):
        """元Cの正常解放後、全Wの確定終了証拠から生成時handle/pipeを一度解放する。

        履歴順に解放し、close時と呼出時の短い期限を各資源・最終記録/返却に適用する。
        部分失敗はUNKNOWN/RELEASEDと原文・元object・予約を保全して伝播する。
        成功もRegistry CLOSED・元予約終端やstart許可を意味しない。
        """
        fields(token, TOKEN)
        identifier(operation_id, 'operationId')
        token = deepcopy(token)
        controller = self.controllers.get(token['instanceId'])
        require(controller is not None and controller.close_binding is not None,
                'WRITER_PENDING', '元Cとclose時固定bindingが必要です')
        deadline = min(deadline, controller.close_binding['deadline'])
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            with self.runtime._transaction(token['instanceId']) as connection:
                opened, saved = self._owner_released(connection, token, controller, operation_id, deadline)
                require('workerRelease' not in saved and controller.worker_release is None,
                        'WRITER_PENDING', '全Wの単発解放意図を保全します')
                workers, bindings = self._worker_release_bindings(token, controller, opened, saved)
                release_record = dict(operationId=operation_id,
                    ownerReleaseSha256=digest(controller.owner_release_response), workers=bindings)
                saved['workerRelease'] = release_record
                encoded = json.dumps(saved)
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W解放意図の更新前に期限を超過しました')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W解放の固定row読取前に期限を超過しました')
                row = dict(self.registry._instance(connection, token['instanceId']))
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W解放の固定row読取後に期限を超過しました')
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?', (encoded, opened['operationId']))
            resources = [(worker.process, worker.pipe) for worker in workers]
            progress = {binding['operationId']:dict(handle='NOT_STARTED', pipe='NOT_STARTED') for binding in bindings}
            controller.worker_release = progress

            # 最初のWのhandle解放不明だけを、作用前の同じ基準で観測する。
            failed = False
            if workers:
                instance = token['instanceId']
                key = instance, opened['operationId']
                history = self.history.get(instance)
                history_refs = tuple(history or [])
                latest = self.workers.get(instance)
                close_binding = controller.close_binding
                close_values = deepcopy({k:v for k,v in close_binding.items() if k not in {'process','pipe'}})
                actors = (controller, *workers)
                references = tuple((actor.process,actor.pipe,actor.creation_resources,tuple(actor.creation_resources),
                    actor.native,actor.adapter,actor.verify_owner,actor.controller,
                    getattr(actor.process,'process',None),getattr(actor.pipe,'incoming',None)) for actor in actors)
                def values():
                    return tuple((actor.bundle,actor.diagnostics,actor.record_number,
                        *(getattr(actor.process,n,None) for n in ('handle','job','thread')),
                        *(getattr(actor.pipe,n,None) for n in
                          ('child_input','child_output','outgoing','offset','incoming','expected'))) for actor in actors)
                fixed_values = deepcopy(values())
                snapshot = self._release_snapshot(controller, workers, history or [])
                snapshot[0][0][0]['worker_release'][bindings[0]['operationId']]['handle'] = 'UNKNOWN'
                intent = encoded
                expected = deepcopy(saved['binding'])
                original_request = canonical(dict(token=token,openOperationId=opened['operationId'],releaseOperationId=operation_id))
                facts = canonical(dict(instanceId=instance,openOperationId=opened['operationId'],
                    releaseOperationId=operation_id,ownerReleaseSha256=digest(controller.owner_release_response),
                    owner={'handle':'RELEASED','pipe':'RELEASED'},workers=[dict(binding,
                        handle='UNKNOWN' if index==0 else 'NOT_STARTED',pipe='NOT_STARTED')
                        for index,binding in enumerate(bindings)]))

                def memory(request):
                    require(request==original_request,'STALE_OPERATION','元W解放要求の固定bindingが一致しません')
                    require(failed and self.runtime._offline is self and self.release_failures.get(key) is packet
                        and self.controllers.get(instance) is controller and self.workers.get(instance) is latest
                        and self.history.get(instance) is history and controller.worker_release is progress
                        and len(history or [])==len(history_refs)
                        and all(a is b and x is y for (a,x),(b,y) in zip(history or [],history_refs))
                        and controller.close_binding is close_binding
                        and close_binding['process'] is controller.process and close_binding['pipe'] is controller.pipe
                        and {k:v for k,v in close_binding.items() if k not in {'process','pipe'}} == close_values
                        and all(actor.process is refs[0] and actor.pipe is refs[1]
                            and actor.creation_resources is refs[2]
                            and len(actor.creation_resources)==len(refs[3])
                            and all(a is b for a,b in zip(actor.creation_resources,refs[3]))
                            and all(a is b for a,b in zip((actor.native,actor.adapter,actor.verify_owner,actor.controller,
                                getattr(actor.process,'process',None),getattr(actor.pipe,'incoming',None)),refs[4:]))
                            for actor,refs in zip(actors,references))
                        and values()==fixed_values
                        and self._release_snapshot(controller,workers,history or [])==snapshot,
                        'PROOF_MISMATCH', '元W解放失敗の作用前bindingと保持証拠が一致しません')

                def saved_failure(connection, current_token, request, limit):
                    memory(request)
                    require(time.monotonic()<limit,'STOP_TIMEOUT','元W予約の照合前に期限を超過しました')
                    self._reserved(connection,current_token,opened['operationId'],opened['requestHash'],expected)
                    require(time.monotonic()<limit,'STOP_TIMEOUT','元W固定rowの照合前に期限を超過しました')
                    current=dict(self.registry._instance(connection,instance))
                    require(time.monotonic()<limit,'STOP_TIMEOUT','元W意図原文の読取前に期限を超過しました')
                    original=connection.execute('SELECT response FROM runtime_operations WHERE id=?',
                        (opened['operationId'],)).fetchone()[0]
                    require({k:v for k,v in current.items() if k!='expires'}=={k:v for k,v in row.items() if k!='expires'}
                        and original==intent,
                        'STALE_OPERATION', '元W解放意図・固定rowが変更されました')
                    memory(request)
                packet = facts, controller, memory, saved_failure
                self.release_failures[key] = packet

            def checked(connection):
                _, current = self._owner_released(connection, token, controller, operation_id, deadline)
                current_workers, current_bindings = self._worker_release_bindings(token, controller, opened, current)
                require(current.get('workerRelease') == release_record and controller.worker_release is progress
                        and current_bindings == bindings and len(current_workers) == len(workers)
                        and all(a is b for a,b in zip(current_workers, workers))
                        and all(worker.process is process and worker.pipe is pipe
                                for worker,(process,pipe) in zip(workers,resources))
                        and digest(controller.owner_release_response) == release_record['ownerReleaseSha256'],
                        'STALE_OPERATION', '全Wの解放意図・原文・保持資源・進捗が変更されました')
                return current

            def verify():
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W解放の本人照合前に期限を超過しました')
                controller.verify_owner()
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W解放の正本読取前に期限を超過しました')
                with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                    checked(connection)
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W資源解放前後に期限を超過しました')

            for binding,(process,pipe) in zip(bindings,resources):
                state = progress[binding['operationId']]
                verify()
                state['handle'] = 'UNKNOWN'
                try:
                    process.close()
                except BaseException:
                    failed = process is resources[0][0]
                    raise
                state['handle'] = 'RELEASED'
                verify()
                state['pipe'] = 'UNKNOWN'
                pipe.close_after_exit(verify=verify)
                require(pipe.input is None and pipe.output is None,
                        'WRITER_PENDING', '元Wの親側pipe解放が完了していません')
                state['pipe'] = 'RELEASED'
                verify()
            result = dict(status='WORKER_HANDLES_RELEASED', operationId=operation_id,
                openOperationId=opened['operationId'], ownerReleaseSha256=release_record['ownerReleaseSha256'],
                workers=deepcopy(bindings), handlesReleased=True)
            controller.worker_release_response = canonical(result)
            verify()
            controller.record['workerRelease'] = deepcopy(result)
            controller._record()
            verify()

            def finished():
                require(all(state == {'handle':'RELEASED','pipe':'RELEASED'} for state in progress.values())
                        and set(progress) == {binding['operationId'] for binding in bindings}
                        and canonical(result) == controller.worker_release_response
                        and controller.record.get('workerRelease') == result,
                        'PROOF_MISMATCH', '全W解放の原文・記録・進捗が一致しません')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W解放結果照合後に期限を超過しました')

            with self.runtime._transaction(token['instanceId']) as connection:
                current = checked(connection)
                finished()
                recorded = dict(release_record, responseSha256=digest(controller.worker_release_response))
                current['workerRelease'] = recorded
                encoded = json.dumps(current)
                checked(connection)
                finished()
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W解放結果の更新前に期限を超過しました')
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?', (encoded, opened['operationId']))
            release_record = recorded
            returned = deepcopy(result)
            verify()
            finished()
            return returned

    def observe_worker_release_failure(self, token, *, operation_id, open_operation_id, release_operation_id, deadline):
        """同じPの最初Wのhandle不明を照合記録する。解放・元終端・再採用は行わない。

        新しい照合IDのCOMPLETEDは観測結果だけで、元openはPENDINGと予約を維持する。
        他の失敗段階とP再起動は拒否する。deadlineは今回の観測全体の絶対期限。
        """
        token=deepcopy(token)
        fields(token,TOKEN)
        for value,name in ((operation_id,'operationId'),(open_operation_id,'openOperationId'),
                           (release_operation_id,'releaseOperationId')):
            identifier(value,name)
        require(len({operation_id,open_operation_id,release_operation_id})==3,
                'OPERATION_CONFLICT','元open/解放と別の照合IDが必要です')
        packet=self.release_failures.get((token['instanceId'],open_operation_id))
        require(packet is not None,'RECOVERY_REQUIRED','同じPの作用前W解放証拠が必要です')
        raw,controller,memory,saved=packet
        facts=json.loads(raw)
        request=canonical(dict(token=token,openOperationId=open_operation_id,releaseOperationId=release_operation_id))
        fingerprint=digest(request)
        result=dict(status='OFFLINE_RELEASE_UNKNOWN',operationId=operation_id,source=facts,terminatesOriginal=False)
        response=canonical(result)
        encoded=json.dumps(result)
        returned=None

        def checked():
            memory(request)
            require(canonical(dict(token=token,openOperationId=open_operation_id,releaseOperationId=release_operation_id))==request,
                    'PROOF_MISMATCH','観測要求の固定原文が変更されました')
            require(canonical(result)==response and (returned is None or canonical(returned)==response),
                    'PROOF_MISMATCH','元観測結果と返却値が一致しません')
            require(time.monotonic()<deadline,'STOP_TIMEOUT','W解放不明の観測期限を超過しました')

        def stored(connection):
            checked()
            saved(connection,token,request,deadline)
            checked()
            for table in ('operations','writer_operations','reference_operations','snapshot_operations'):
                checked()
                require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?',(operation_id,)).fetchone(),
                        'OPERATION_CONFLICT','観測IDが他の操作で使用されています')
            checked()
            old=connection.execute('SELECT * FROM runtime_operations WHERE id=?',(operation_id,)).fetchone()
            require(old is None or (old['command']=='offline-observe-worker-release' and old['instance']==token['instanceId']
                and old['fingerprint']==fingerprint and old['status']=='COMPLETED' and old['response']==encoded),
                'OPERATION_CONFLICT','元観測の入力・結果が一致しません')
            checked()
            return old

        checked()
        with self.registry.resource_use(),self.runtime._lock_wait(deadline):
            checked()
            controller._verify_close_binding(deadline)
            checked()
            with self.runtime._transaction(token['instanceId']) as connection:
                old=stored(connection)
                if old is None:
                    checked()
                    connection.execute('INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)',
                        (operation_id,fingerprint,'offline-observe-worker-release',token['instanceId'],'COMPLETED',encoded))
                require(stored(connection) is not None,'PROOF_REQUIRED','W解放観測の記録がありません')
            returned=deepcopy(result)
            checked()
            with self.runtime._transaction(token['instanceId'],read_only=True) as connection:
                require(stored(connection) is not None,'PROOF_REQUIRED','W解放観測の正本がありません')
            checked()
        checked()
        return returned

    def _workers_released(self, connection, token, controller, operation_id, deadline):
        opened, saved = self._owner_released(connection, token, controller, operation_id, deadline)
        workers, bindings = self._worker_release_bindings(token, controller, opened, saved)
        require(controller.worker_release_response is not None,
                'PROOF_REQUIRED', '全Wの正常解放原文が必要です')
        response = json.loads(controller.worker_release_response)
        owner_sha256 = digest(controller.owner_release_response)
        require(controller.worker_release == {binding['operationId']:{'handle':'RELEASED','pipe':'RELEASED'}
                    for binding in bindings}
                and all(worker.pipe.input is None and worker.pipe.output is None for worker in workers)
                and response == dict(status='WORKER_HANDLES_RELEASED', operationId=operation_id,
                    openOperationId=opened['operationId'], ownerReleaseSha256=owner_sha256,
                    workers=bindings, handlesReleased=True) and response['handlesReleased'] is True
                and controller.record.get('workerRelease') == response
                and saved.get('workerRelease') == dict(operationId=operation_id,
                    ownerReleaseSha256=owner_sha256, workers=bindings,
                    responseSha256=digest(controller.worker_release_response)),
                'PROOF_MISMATCH', '全W解放の原文・正本hash・RELEASED進捗・親pipeが一致しません')
        require(time.monotonic() < deadline, 'STOP_TIMEOUT', '全W解放の照合後に期限を超過しました')
        return opened, saved, workers

    def record_writer_closed(self, token, *, operation_id, deadline):
        """同closeの全解放証拠からoffline writer CLOSEDを一度だけ正本へ記録する。

        close時と呼出時の短い期限を局所記録・最終transaction・返却まで適用する。
        失敗時も元原文・資源object・意図を保全する。元offline-open PENDINGと予約は
        終端せず、既存Manager writer登録・start受付を変更しない。
        """
        fields(token, TOKEN)
        identifier(operation_id, 'operationId')
        token = deepcopy(token)
        controller = self.controllers.get(token['instanceId'])
        require(controller is not None and controller.close_binding is not None,
                'WRITER_PENDING', '元Cとclose時固定bindingが必要です')
        deadline = min(deadline, controller.close_binding['deadline'])
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            with self.runtime._transaction(token['instanceId']) as connection:
                opened, saved, workers = self._workers_released(connection, token, controller, operation_id, deadline)
                require('writerClosed' not in saved and controller.writer_closed_response is None,
                        'WRITER_PENDING', 'offline writer CLOSEDの単発記録を保全します')
                closing = dict(operationId=operation_id,
                    ownerReleaseSha256=digest(controller.owner_release_response),
                    workerReleaseSha256=digest(controller.worker_release_response))
                saved['writerClosed'] = closing
                encoded = json.dumps(saved)
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'CLOSED意図の更新前に期限を超過しました')
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?', (encoded, opened['operationId']))

            def checked(connection):
                _, current, held = self._workers_released(connection, token, controller, operation_id, deadline)
                require(current.get('writerClosed') == closing and len(held) == len(workers)
                        and all(a is b for a,b in zip(held,workers))
                        and digest(controller.owner_release_response) == closing['ownerReleaseSha256']
                        and digest(controller.worker_release_response) == closing['workerReleaseSha256'],
                        'STALE_OPERATION', 'CLOSED意図と元C/全W解放原文・保持先が変更されました')
                return current

            def verify():
                controller.verify_owner()
                with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                    checked(connection)
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'CLOSED正本の照合後に期限を超過しました')

            result = dict(status='OFFLINE_WRITER_CLOSED', operationId=operation_id,
                openOperationId=opened['operationId'], writerStatus='CLOSED',
                ownerReleaseSha256=closing['ownerReleaseSha256'], workerReleaseSha256=closing['workerReleaseSha256'])
            controller.writer_closed_response = canonical(result)
            verify()
            controller.record['writerClosed'] = deepcopy(result)
            controller._record()
            verify()

            def finished():
                require(canonical(result) == controller.writer_closed_response
                        and controller.record.get('writerClosed') == result,
                        'PROOF_MISMATCH', 'CLOSED結果の保持原文と局所記録が一致しません')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'CLOSED結果の照合後に期限を超過しました')

            with self.runtime._transaction(token['instanceId']) as connection:
                current = checked(connection)
                finished()
                recorded = dict(closing, state='CLOSED', responseSha256=digest(controller.writer_closed_response))
                current['writerClosed'] = recorded
                encoded = json.dumps(current)
                checked(connection)
                finished()
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'CLOSED結果の更新前に期限を超過しました')
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?', (encoded, opened['operationId']))
            closing = recorded
            returned = deepcopy(result)
            verify()
            finished()
            return returned

    def _writer_closed(self, connection, token, controller, operation_id, deadline):
        opened, saved, workers = self._workers_released(connection, token, controller, operation_id, deadline)
        require(controller.writer_closed_response is not None, 'PROOF_REQUIRED', 'writer CLOSED原文が必要です')
        result = dict(status='OFFLINE_WRITER_CLOSED', operationId=operation_id,
            openOperationId=opened['operationId'], writerStatus='CLOSED',
            ownerReleaseSha256=digest(controller.owner_release_response),
            workerReleaseSha256=digest(controller.worker_release_response))
        require(json.loads(controller.writer_closed_response) == result and controller.record.get('writerClosed') == result
                and saved.get('writerClosed') == dict(operationId=operation_id,
                    ownerReleaseSha256=result['ownerReleaseSha256'], workerReleaseSha256=result['workerReleaseSha256'],
                    state='CLOSED', responseSha256=digest(controller.writer_closed_response)),
                'PROOF_MISMATCH', 'writer CLOSEDの原文・局所記録・正本hashが一致しません')
        require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'writer CLOSED照合後に期限を超過しました')
        return opened, saved, workers

    @staticmethod
    def _release_snapshot(controller, workers, history):
        names = ('parent','parent_data','identity','data_identity','session','nonce','started','ready',
            'startup_acknowledged','request','response','assignment_started','assignment_ready','assignment_acknowledged',
            'assignment_request','assignment_response','admission_started','candidate_ready','admission_request',
            'candidate_response','exit_started','exit_response','completion_started','completion_ready',
            'completion_acknowledged','completion_request','completion_response','close_started','close_ready',
            'close_request','close_response','owner_exit_started','owner_exit_response','owner_release',
            'owner_release_response','worker_release','worker_release_response','writer_closed_response','operation_history')
        return deepcopy(([(dict((name,getattr(actor,name)) for name in names),
            {k:v for k,v in actor.record.items() if actor is not controller or k != 'termination'},
            actor.process.pid, actor.process.group, actor.pipe.input, actor.pipe.output)
            for actor in (controller,*workers)], [record for _,record in history]))

    def _terminal_memory(self, slot):
        controller, workers = slot['controller'], slot['workers']
        require(self.retired.get(slot['key']) is slot and controller.close_binding is slot['closeBinding']
                and all(worker.controller is controller for worker in workers)
                and all(actor.process is process and actor.pipe is pipe and actor.creation_resources is resources
                    for actor,process,pipe,resources in slot['resources'])
                and self._release_snapshot(controller, workers, slot['history']) == slot['snapshot'],
                'PROOF_MISMATCH', '退避した元C/全Wの資源object・解放原文・進捗が変更されました')
        require(controller.termination_response == canonical(slot['result'])
                and controller.record.get('termination') == slot['result'],
                'PROOF_MISMATCH', '終端原文と局所記録が一致しません')

    def _terminal_resources(self, slot, deadline):
        self._terminal_memory(slot)
        slot['controller']._verify_close_binding(deadline)
    def _terminal(self, connection, token, slot, deadline):
        self._terminal_resources(slot, deadline)
        self._terminal_state(connection, token, slot, deadline)

    def _terminal_state(self, connection, token, slot, deadline):
        row, gate, run, proof, writers = self._state(connection, token, reserved=False)
        self.runtime._binding(row, slot['row'])
        expected = slot['saved']['binding']
        require(gate['revision'] == expected['revision'] and gate['descriptor_hash'] == expected['descriptorSha256']
                and digest(canonical(run)) == expected['runRecordSha256']
                and digest(canonical(proof)) == expected['stopProofSha256'] and writers == expected['writers'],
                'STALE_OPERATION', '終端後の元run・閉門・停止証拠が変更されました')
        opened = slot['opened']
        operation = connection.execute('SELECT * FROM runtime_operations WHERE id=?', (opened['operationId'],)).fetchone()
        require(operation is not None and operation['instance'] == row['id'] and operation['command'] == 'offline-open'
                and operation['fingerprint'] == opened['requestHash'] and operation['status'] == 'COMPLETED'
                and operation['response'] == slot['encoded']
                and not connection.execute('SELECT 1 FROM reservations WHERE instance=?', (row['id'],)).fetchone()
                and not connection.execute("SELECT 1 FROM runtime_operations WHERE instance=? AND status!='COMPLETED'",
                                           (row['id'],)).fetchone(),
                'STALE_OPERATION', '元offline-open終端正本と予約解放が一致しません')
        require(time.monotonic() < deadline, 'STOP_TIMEOUT', '終端正本の照合後に期限を超過しました')

    def finish_close(self, token, *, operation_id, deadline):
        """CLOSED正本から元offline-openと予約を一度だけ終端し、元object・原文を退避保持する。

        close時と呼出時の短い期限を返却まで適用する。終端後はPENDING前提の元callbackを
        呼ばず専用照合を行う。失敗時の意図・既知結果・退避先は保持し、自動再試行しない。
        """
        fields(token, TOKEN)
        identifier(operation_id, 'operationId')
        token = deepcopy(token)
        instance = token['instanceId']
        controller = self.controllers.get(instance)
        require(controller is not None and controller.close_binding is not None,
                'WRITER_PENDING', '元Cとclose時固定bindingが必要です')
        deadline = min(deadline, controller.close_binding['deadline'])
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            with self.runtime._transaction(instance) as connection:
                opened, saved, workers = self._writer_closed(connection, token, controller, operation_id, deadline)
                key = instance, opened['operationId']
                require(key not in self.retired and 'termination' not in saved and controller.termination_response is None,
                        'WRITER_PENDING', '元終端意図と退避証拠を保全します')
                row = dict(self.registry._instance(connection, instance))
                history = self.history.get(instance, [])
                latest = self.workers.get(instance)
                snapshot = self._release_snapshot(controller, workers, history)
                release_snapshot = deepcopy(snapshot)
                slot = dict(key=key, controller=controller, workers=tuple(workers), history=history,
                    latest=latest, opened=opened, saved=deepcopy(saved), row=row,
                    closeBinding=controller.close_binding,
                    resources=[(actor,actor.process,actor.pipe,actor.creation_resources) for actor in (controller,*workers)],
                    snapshot=snapshot)
                ending = dict(operationId=operation_id, generationId=row['generation'], runId=row['run_id'],
                    writerClosedSha256=digest(controller.writer_closed_response))
                saved['termination'] = ending
                encoded = json.dumps(saved)
                intent = encoded
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', '終端意図の更新前に期限を超過しました')
                self.retired[key] = slot
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?', (encoded, opened['operationId']))

            def checked(connection):
                require(self.retired.get(key) is slot and self.controllers.get(instance) is controller
                        and self.workers.get(instance) is latest and slot['latest'] is latest,
                        'STALE_OPERATION', '元終端の保持枠が変更されました')
                _, current, held = self._writer_closed(connection, token, controller, operation_id, deadline)
                require(current.get('termination') == ending
                        and self.history.get(instance, []) == history and len(held) == len(workers)
                        and all(a is b for a,b in zip(held,workers))
                        and controller.close_binding is slot['closeBinding']
                        and all(actor.process is process and actor.pipe is pipe and actor.creation_resources is resources
                            for actor,process,pipe,resources in slot['resources'])
                        and slot['snapshot'] is snapshot and snapshot == release_snapshot
                        and self._release_snapshot(controller, workers, history) == release_snapshot,
                        'STALE_OPERATION', '終端意図と元C/全Wの保持証拠が変更されました')
                return current

            def verify():
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', '終端意図の本人照合前に期限を超過しました')
                controller.verify_owner()
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', '終端意図の正本読取前に期限を超過しました')
                with self.runtime._transaction(instance, read_only=True) as connection:
                    checked(connection)
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', '終端意図の照合後に期限を超過しました')

            result = dict(status='OFFLINE_SESSION_COMPLETED', operationId=operation_id,
                openOperationId=opened['operationId'], instanceId=instance,
                generationId=row['generation'], runId=row['run_id'], writerClosedSha256=ending['writerClosedSha256'])
            slot['result'] = result
            controller.termination_response = canonical(result)
            verify()
            controller.record['termination'] = deepcopy(result)
            current = json.loads(intent)
            current['termination'] = dict(ending, state='COMPLETED', responseSha256=digest(controller.termination_response))
            encoded = json.dumps(current)
            slot['encoded'] = encoded
            values = ('key', 'row', 'opened', 'saved', 'encoded', 'result', 'snapshot')
            fixed = deepcopy(tuple(slot[name] for name in values))
            originals = tuple(slot[name] for name in values)
            held = tuple(slot[name] for name in ('controller', 'workers', 'history', 'closeBinding', 'resources'))
            actors = tuple(slot['resources'])
            objects = tuple((actor.native, actor.adapter, actor.verify_owner, actor.controller,
                             getattr(process, 'process', None), getattr(pipe, 'incoming', None))
                            for actor,process,pipe,_ in actors)
            def resource_values():
                return tuple((actor.bundle, actor.diagnostics,
                    *(getattr(process, name, None) for name in ('handle','job','thread')),
                    *(getattr(pipe, name, None) for name in
                      ('input','output','child_input','child_output','outgoing','offset','incoming','expected')))
                    for actor,process,pipe,_ in actors)
            resource_snapshot = deepcopy(resource_values())
            record_numbers = tuple(actor.record_number + (1 if actor is controller else 0)
                                   for actor,_,_,_ in actors)
            close_values = deepcopy({name:value for name,value in controller.close_binding.items()
                                     if name not in {'process', 'pipe'}})
            raw = canonical(result)

            record_raw = canonical(controller.record)
            recorded = False

            def finished():
                require(controller.termination_response == canonical(result) and controller.record.get('termination') == result,
                        'PROOF_MISMATCH', '終端結果の原文と局所記録が一致しません')
                require(time.monotonic() < deadline, 'STOP_TIMEOUT', '終端結果の照合後に期限を超過しました')

            def recover_memory():
                require(recorded and self.close_preparations.get(key) is preparation
                        and self.close_recoveries.get(key) is packet
                        and slot['latest'] is latest
                        and slot['snapshot'] is snapshot and snapshot == release_snapshot
                        and all(slot[name] is value for name,value in zip(
                            ('controller', 'workers', 'history', 'closeBinding', 'resources'), held))
                        and len(slot['resources']) == len(actors)
                        and all(len(actual) == len(original) and all(a is b for a,b in zip(actual, original))
                                for actual,original in zip(slot['resources'], actors))
                        and all(slot[name] is value for name,value in zip(values, originals))
                        and tuple(slot[name] for name in values) == fixed
                        and all(all(a is b for a,b in zip(
                            (actor.native, actor.adapter, actor.verify_owner, actor.controller,
                             getattr(process, 'process', None), getattr(pipe, 'incoming', None)), original))
                            for (actor,process,pipe,_),original in zip(actors, objects))
                        and resource_values() == resource_snapshot
                        and tuple(actor.record_number for actor,_,_,_ in actors) == record_numbers
                        and {name:value for name,value in controller.close_binding.items()
                             if name not in {'process', 'pipe'}} == close_values
                        and controller.close_binding['process'] is controller.process
                        and controller.close_binding['pipe'] is controller.pipe,
                        'PROOF_MISMATCH', '元close確定時の保持bindingが変更されました')
                self._terminal_memory(slot)
                return intent

            packet = raw, slot, recover_memory
            def publish(current_token):
                require(recorded and self.close_preparations.get(key) is preparation,
                        'RECOVERY_REQUIRED', '同じPの実write正常完了と元準備packetが必要です')
                require(current_token == token,
                        'STALE_OPERATION', '元closeのlease/epoch/generationが一致しません')
                current = self.close_recoveries.get(key)
                require(current is None or current is packet,
                        'PROOF_MISMATCH', '元完成packetの保持先が変更されました')
                self.close_recoveries[key] = packet
                recover_memory()

            def written(data):
                nonlocal recorded
                require(not recorded and data == record_raw,
                        'PROOF_MISMATCH', '実write原文が作用前に固定した記録と一致しません')
                recorded = True
                publish(token)

            preparation = packet, publish
            self.close_preparations[key] = preparation
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', '終端局所記録の開始前に期限を超過しました')
            controller._record(observe=written)

            recover_memory()
            finished()
            verify()
            with self.runtime._transaction(instance) as connection:
                current = checked(connection)
                finished()
                current['termination'] = dict(ending, state='COMPLETED', responseSha256=digest(controller.termination_response))
                require(json.dumps(current) == encoded, 'STALE_OPERATION', '元意図から固定した終端原文が変更されました')
                recover_memory()
                finished()
                checked(connection)
                recover_memory()
                finished()
                connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=?",
                                   (encoded, opened['operationId']))
                connection.execute('DELETE FROM reservations WHERE operation=?', (opened['operationId'],))
                connection.execute('UPDATE instances SET pending=0 WHERE id=?', (instance,))
                self.workers.pop(instance, None)
                self.history.pop(instance, None)
                self.controllers.pop(instance)
                self._terminal(connection, token, slot, deadline)
            returned = deepcopy(result)
            with self.runtime._transaction(instance, read_only=True) as connection:
                self._terminal(connection, token, slot, deadline)
            recover_memory()
            finished()
            return returned

    def recover_recorded_close(self, token, *, operation_id, open_operation_id, close_operation_id,
                               termination_sha256, deadline):
        """同じPの実write完了後の引渡し失敗を、元準備packetと第56 CASで照合する。

        ファイル存在や記録番号から完了を推定せず、元記録・closeを再実行しない。
        deadlineは今回の照合要求だけに使用し、元資源と期限を変更しない。
        """
        token = deepcopy(token)
        fields(token, TOKEN)
        for value, name in ((operation_id, 'operationId'), (open_operation_id, 'openOperationId'),
                            (close_operation_id, 'closeOperationId')):
            identifier(value, name)
        _hash(termination_sha256)
        require(len({operation_id, open_operation_id, close_operation_id}) == 3,
                'OPERATION_CONFLICT', '終端照合には元open/closeとは別のIDが必要です')
        preparation = self.close_preparations.get((token['instanceId'], open_operation_id))
        require(preparation is not None, 'RECOVERY_REQUIRED', '同じPの元close準備packetが必要です')
        packet, publish = preparation
        require(digest(packet[0]) == termination_sha256 and json.loads(packet[0])['operationId'] == close_operation_id,
                'PROOF_MISMATCH', '元close IDと終端原文hashが一致しません')
        require(time.monotonic() < deadline, 'STOP_TIMEOUT', '実write証拠の照合前に期限を超過しました')
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', '元packet引渡し前に期限を超過しました')
            publish(token)
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', '元packet引渡し後に期限を超過しました')
        require(time.monotonic() < deadline, 'STOP_TIMEOUT', '資源調停退出後に期限を超過しました')
        return self.recover_pending_close(token, operation_id=operation_id, open_operation_id=open_operation_id,
            close_operation_id=close_operation_id, termination_sha256=termination_sha256, deadline=deadline)

    def recover_close(self, token, *, operation_id, open_operation_id, close_operation_id, termination_sha256, deadline):
        """同じPの確定済みcloseを新しい照合要求でreadbackする。解放・再送・再採用は行わない。

        元終端前に固定したpacketがなく、正本が未確定、又は保持資源が不明なら拒否する。
        deadlineは今回の照合だけの期限であり、元closeの期限や進捗は変更しない。
        """
        token = deepcopy(token)
        fields(token, TOKEN)
        for value, name in ((operation_id, 'operationId'), (open_operation_id, 'openOperationId'),
                            (close_operation_id, 'closeOperationId')):
            identifier(value, name)
        _hash(termination_sha256)
        require(len({operation_id, open_operation_id, close_operation_id}) == 3,
                'OPERATION_CONFLICT', '復旧照合には元open/closeとは別のIDが必要です')
        key = token['instanceId'], open_operation_id
        packet = self.close_recoveries.get(key)
        require(packet is not None, 'RECOVERY_REQUIRED', '同じPが保持する元close完成packetが必要です')
        raw, slot, memory = packet
        require(digest(raw) == termination_sha256 and json.loads(raw)['operationId'] == close_operation_id,
                'PROOF_MISMATCH', '指定された元closeと終端原文hashが一致しません')
        request = canonical(dict(token=token, openOperationId=open_operation_id,
            closeOperationId=close_operation_id, terminationSha256=termination_sha256))
        fingerprint = digest(request)
        result = dict(status='OFFLINE_CLOSE_RECOVERED', operationId=operation_id,
            openOperationId=open_operation_id, closeOperationId=close_operation_id,
            instanceId=token['instanceId'], terminationSha256=termination_sha256, original=json.loads(raw))
        encoded = json.dumps(result)
        response = canonical(result)
        returned = None

        def checked():
            memory()
            require(canonical(result) == response and (returned is None or canonical(returned) == response),
                    'PROOF_MISMATCH', '復旧結果の固定原文が変更されました')
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'close復旧照合の期限を超過しました')

        def saved(connection):
            checked()
            self._terminal_state(connection, token, slot, deadline)
            for table in ('operations', 'writer_operations', 'reference_operations', 'snapshot_operations'):
                require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (operation_id,)).fetchone(),
                        'OPERATION_CONFLICT', '復旧IDが他の操作で使用されています')
            old = connection.execute('SELECT * FROM runtime_operations WHERE id=?', (operation_id,)).fetchone()
            require(old is None or (old['command'] == 'offline-recover-close' and old['instance'] == token['instanceId']
                    and old['fingerprint'] == fingerprint and old['status'] == 'COMPLETED' and old['response'] == encoded),
                    'OPERATION_CONFLICT', '元復旧要求の固定入力・結果が一致しません')
            checked()
            return old

        checked()
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            checked()
            self._terminal_resources(slot, deadline)
            checked()
            with self.runtime._transaction(token['instanceId']) as connection:
                if saved(connection) is None:
                    connection.execute('INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)',
                        (operation_id, fingerprint, 'offline-recover-close', token['instanceId'], 'COMPLETED', encoded))
                saved(connection)
            checked()
            returned = deepcopy(result)
            checked()
        checked()
        return returned

    def recover_pending_close(self, token, *, operation_id, open_operation_id, close_operation_id,
                              termination_sha256, deadline):
        """同じPの元終端意図と解放済みpacketから未commitの終端を確定する。

        新しい照合要求のdeadlineを使い、元closeの期限・資源・原文を維持する。
        元packetがない場合や解放証拠が変わった場合は保全して拒否する。
        """
        token = deepcopy(token)
        fields(token, TOKEN)
        for value, name in ((operation_id, 'operationId'), (open_operation_id, 'openOperationId'),
                            (close_operation_id, 'closeOperationId')):
            identifier(value, name)
        _hash(termination_sha256)
        require(len({operation_id, open_operation_id, close_operation_id}) == 3,
                'OPERATION_CONFLICT', '終端照合には元open/closeとは別のIDが必要です')
        instance = token['instanceId']
        packet = self.close_recoveries.get((instance, open_operation_id))
        require(packet is not None, 'RECOVERY_REQUIRED', '同じPが保持する元終端packetが必要です')
        raw, slot, memory = packet
        intent = memory()
        require(isinstance(intent, str) and digest(raw) == termination_sha256
                and json.loads(raw)['operationId'] == close_operation_id,
                'PROOF_MISMATCH', '元終端意図・close ID・原文hashが一致しません')
        fingerprint = digest(canonical(dict(token=token, openOperationId=open_operation_id,
            closeOperationId=close_operation_id, terminationSha256=termination_sha256)))
        result = dict(status='OFFLINE_CLOSE_TERMINATED', operationId=operation_id,
            openOperationId=open_operation_id, closeOperationId=close_operation_id,
            instanceId=instance, terminationSha256=termination_sha256, original=json.loads(raw))
        encoded = json.dumps(result)
        response = canonical(result)
        returned = None

        def checked():
            require(memory() == intent and canonical(result) == response
                    and (returned is None or canonical(returned) == response),
                    'PROOF_MISMATCH', '元終端packetと今回の固定結果が一致しません')
            require(all(instance not in mapping or mapping[instance] is original
                        for mapping, original in ((self.controllers, slot['controller']),
                            (self.workers, slot['latest']), (self.history, slot['history']))),
                    'STALE_OPERATION', '元instanceの保持枠が別objectに置き換わりました')
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', '未確定closeの終端照合期限を超過しました')

        def finished():
            checked()
            require(all(instance not in mapping for mapping in (self.controllers, self.workers, self.history)),
                    'STALE_OPERATION', '終端後の元保持枠が再利用されました')

        checked()
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            checked()
            self._terminal_resources(slot, deadline)
            checked()
            with self.runtime._transaction(instance) as connection:
                checked()
                for table in ('operations', 'writer_operations', 'reference_operations', 'snapshot_operations'):
                    require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (operation_id,)).fetchone(),
                            'OPERATION_CONFLICT', '終端照合IDが別の操作で使用されています')
                old = connection.execute('SELECT * FROM runtime_operations WHERE id=?', (operation_id,)).fetchone()
                checked()
                if old is None:
                    self._reserved(connection, token, open_operation_id, slot['opened']['requestHash'], slot['saved']['binding'])
                    checked()
                    self.runtime._binding(self.registry._instance(connection, instance), slot['row'])
                    checked()
                    original = connection.execute('SELECT response FROM runtime_operations WHERE id=?',
                                                  (open_operation_id,)).fetchone()[0]
                    require(original == intent, 'STALE_OPERATION', '元終端意図の保存原文が変更されました')
                    checked()
                    changed = connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? "
                        "WHERE id=? AND status='PENDING' AND response=?", (slot['encoded'], open_operation_id, intent))
                    require(changed.rowcount == 1, 'STALE_OPERATION', '元終端意図の更新が競合しました')
                    checked()
                    connection.execute('DELETE FROM reservations WHERE instance=? AND operation=?', (instance, open_operation_id))
                    checked()
                    connection.execute('UPDATE instances SET pending=0 WHERE id=?', (instance,))
                    checked()
                    connection.execute('INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)',
                        (operation_id, fingerprint, 'offline-recover-pending-close', instance, 'COMPLETED', encoded))
                    checked()
                    self.workers.pop(instance, None)
                    self.history.pop(instance, None)
                    self.controllers.pop(instance, None)
                else:
                    require(old['command'] == 'offline-recover-pending-close' and old['instance'] == instance
                            and old['fingerprint'] == fingerprint and old['status'] == 'COMPLETED' and old['response'] == encoded,
                            'OPERATION_CONFLICT', '終端照合要求の固定入力・結果が一致しません')
                checked()
                self._terminal_state(connection, token, slot, deadline)
                finished()
            finished()
            returned = deepcopy(result)
            finished()
        finished()
        return returned

    def check_closed_ready(self, token, *, open_operation_id, ready, deadline):
        """独立明示Readyを保持した元offline終端証拠と照合する。

        新しい呼出期限を使い、元C/W・解放原文・CLOSED/予約終端正本を再照合する。
        戻り値はclose時Readyとの一致証拠であり、現在のData Readyやstartの許可ではない。
        Data原本の確認と起動予約・ACK・最終返却までの継続照合は起動側で必要。
        """
        token, ready = deepcopy((token, ready))
        fields(token, TOKEN)
        identifier(open_operation_id, 'openOperationId')
        _check_ready(ready)
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            slot = self.retired.get((token['instanceId'], open_operation_id))
            require(slot is not None and 'encoded' in slot,
                    'PROOF_REQUIRED', '保持した完成済みoffline終端証拠が必要です')
            with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                self._terminal(connection, token, slot, deadline)
                response = json.loads(slot['controller'].close_response)
                response['worldUuid'] = slot['opened']['worldUuid']
                require(ready == {name: response[name] for name in ready},
                        'PROOF_MISMATCH', '独立明示Readyと元close応答が一致しません')
                result = dict(status='OFFLINE_CLOSED_READY_VERIFIED', instanceId=token['instanceId'],
                    generationId=slot['row']['generation'], openOperationId=open_operation_id,
                    terminationSha256=digest(slot['controller'].termination_response),
                    writerClosedSha256=digest(slot['controller'].writer_closed_response), ready=ready)
            returned = deepcopy(result)
            with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                self._terminal(connection, token, slot, deadline)
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'Ready終端証拠の返却前に期限を超過しました')
            return returned

    def read_ready_original(self, token, *, open_operation_id, ready, deadline):
        """独立Readyと元終端に一致する現在原文を、固定generationから読み返す。

        world-id/current/不変stateと元C/Wの保持証拠を返却前にも照合する。chain/receiptの
        意味検証と起動予約は呼出元の後続責務であり、この結果だけでstartを受理しない。
        """
        return self._ready_original(token, open_operation_id=open_operation_id, ready=ready, deadline=deadline)[0]

    def _ready_original(self, token, *, open_operation_id, ready, deadline):
        token, ready = deepcopy((token, ready))
        fields(token, TOKEN)
        identifier(open_operation_id, 'openOperationId')
        key = token['instanceId'], open_operation_id

        def read(path, maximum=16 * 1024 * 1024):
            require(time.monotonic() < deadline, 'START_TIMEOUT', 'Ready原文読取前に期限を超過しました')
            plain_path(path)
            info = path.lstat()
            require(info.st_nlink == 1 and info.st_size <= maximum,
                    'PROOF_MISMATCH', 'Ready原文は上限内の単独通常fileが必要です')
            raw = regular_bytes(path, maximum=maximum)
            require(path.lstat().st_nlink == 1, 'PROOF_MISMATCH', 'Ready原文のlink数が変更されました')
            require(time.monotonic() < deadline, 'START_TIMEOUT', 'Ready原文読取後に期限を超過しました')
            return raw

        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            slot = self.retired.get(key)
            closed = self.check_closed_ready(token, open_operation_id=open_operation_id, ready=ready, deadline=deadline)
            require(slot is not None and self.retired.get(key) is slot,
                    'STALE_OPERATION', '元offline終端の退避先が変更されました')
            generation = self.runtime._active_generation(slot['row'])
            settings_path = generation / 'server.properties'
            settings_raw = read(settings_path)
            settings = properties(portable_properties(settings_raw))
            world = generation / settings['level-name']
            shop = generation / 'config/SushiEricServerMod/shop_data'
            require(slot['opened']['worldRoot'] == str(world) and slot['opened']['shopDirectory'] == str(shop),
                    'PROOF_MISMATCH', '元Cのworld/shopと固定generationが一致しません')
            data = world / 'sushieric'
            world_path = data / 'world-id'
            world_raw = read(world_path, 128)
            require(world_raw.decode('utf-8').strip() == ready['worldUuid'],
                    'PROOF_MISMATCH', '固定world-idと独立Readyが一致しません')
            current_path = data / 'run-state/current.json'
            current_raw = read(current_path)
            require(digest(current_raw) == ready['stateSha256'],
                    'PROOF_MISMATCH', '現在Ready原文と独立hashが一致しません')
            state_path = data / 'run-state/states' / (ready['stateSha256'] + '.json')
            state_raw = read(state_path)
            require(state_raw == current_raw, 'PROOF_MISMATCH', 'currentと不変state原文が一致しません')
            state = _json(current_raw)
            fields(state, {'format', 'worldUuid', 'generation', 'runUuid', 'stateRevision', 'paths',
                           'originSha256', 'previousStateSha256', 'payload'})
            require(type(state['format']) is int and state['format'] == 1
                    and type(state['stateRevision']) is int and state['stateRevision'] == ready['stateRevision']
                    and state['worldUuid'] == ready['worldUuid'] and state['runUuid'] == ready['runUuid']
                    and state['generation'] == closed['generationId'],
                    'PROOF_MISMATCH', '現在Readyの形式・world/run/世代/revisionが一致しません')
            require(state['paths'] == dict(worldRoot=str(world), database=str(data / 'server.sqlite'), shopDirectory=str(shop)),
                    'PROOF_MISMATCH', '現在Readyの保存先bindingが一致しません')
            fields(state['payload'], {'type', 'sealSha256', 'admissionSha256'})
            require(state['payload']['type'] == 'io.github.sushiericworkspace.sushiericdatamodel.database.run.WorldRunPhase.Ready',
                    'PROOF_MISMATCH', '現在原文はReadyが必要です')
            for value in (state['originSha256'], state['payload']['sealSha256'], state['payload']['admissionSha256']):
                _hash(value)
            if state['previousStateSha256'] is not None:
                _hash(state['previousStateSha256'])
            returned = deepcopy(dict(closed, status='OFFLINE_READY_ORIGINAL_MATCHED', stateJson=current_raw.decode('utf-8')))
            originals = ((settings_path, settings_raw, 16 * 1024 * 1024),
                         (world_path, world_raw, 128), (state_path, state_raw, 16 * 1024 * 1024))

            def files(current=None):
                for path, raw, maximum in originals:
                    require(read(path, maximum) == raw, 'PROOF_MISMATCH', 'Ready原文の保持中にfileが変更されました')
                if current is None:
                    require(read(current_path) == current_raw, 'PROOF_MISMATCH', 'Ready原文の保持中にfileが変更されました')
                else:
                    current(read, current_path, current_raw, state, originals)
                self.runtime._active_generation(slot['row'])
            files()
            with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                self._terminal(connection, token, slot, deadline)
            require(time.monotonic() < deadline, 'START_TIMEOUT', 'Ready原文の返却前に期限を超過しました')
            return returned, files

    def verify_ready(self, token, *, operation_id, open_operation_id, ready, deadline):
        """元終端と現在Readyを単発helperの公式読取へ結ぶ。startは許可しない。

        同じ期限で元C/W・現在原文・Registry bindingを作用前後に照合する。
        元readerと原文をoperation別に保持し、同ID再実行と不明な読取の再生成を拒否する。
        """
        return self._verify_ready(token, operation_id=operation_id, open_operation_id=open_operation_id,
                                  ready=ready, deadline=deadline)[0]

    def _verify_ready(self, token, *, operation_id, open_operation_id, ready, deadline):
        token, ready = deepcopy((token, ready))
        fields(token, TOKEN)
        _canonical_uuid(operation_id)
        identifier(open_operation_id, 'openOperationId')
        instance = token['instanceId']
        key = instance, operation_id
        fingerprint = digest(canonical(dict(command='offline-ready', token=token,
            openOperationId=open_operation_id, ready=ready)))
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            original, files = self._ready_original(token, open_operation_id=open_operation_id, ready=ready, deadline=deadline)
            original_raw = canonical(original)
            terminal = self.retired[(instance, open_operation_id)]
            with self.runtime._transaction(instance, read_only=True) as connection:
                self._terminal(connection, token, terminal, deadline)
                row = dict(self.registry._instance(connection, instance))
            bundle = self.runtime._active_generation(row) / 'runtime'
            managed, adapter = self.runtime.managed, self.runtime.process_adapter
            require(managed is not None and terminal['controller'].bundle == bundle,
                    'ARTIFACT_MISMATCH', '元Cと固定generationのbundleが必要です')
            manifest = verify_bundle(bundle)
            require(manifest['artifactId'] == row['artifact'],
                    'ARTIFACT_MISMATCH', '読取bundleと元artifact正本が一致しません')
            require_data_generation(manifest, original['generationId'])
            transport = managed.transport
            native = transport.native
            registry, root = self.registry, self.registry.root
            diagnostics = self.runtime._paths(instance) / 'diagnostics' / ('ready-' + operation_id)
            frame = dict(operationId=operation_id, requestHash=fingerprint, generation=original['generationId'],
                worldRoot=terminal['opened']['worldRoot'], shopDirectory=terminal['opened']['shopDirectory'],
                worldUuid=ready['worldUuid'], runUuid=ready['runUuid'],
                expectedStateSha256=ready['stateSha256'], expectedRevision=ready['stateRevision'])
            frame_raw = canonical(frame)
            resource_objects = tuple(tuple(item) for item in terminal['resources'])
            terminal_objects = tuple(terminal[name] for name in ('controller','workers','history','resources','closeBinding'))
            actor_objects = tuple(tuple(getattr(actor,name,None) for name in ('native','adapter','verify_owner','controller'))
                                  + (getattr(process,'process',None),)
                                  for actor,process,_,_ in resource_objects)
            close_objects = tuple(terminal['closeBinding'].get(name) for name in ('process','pipe'))
            history_objects = tuple(terminal_objects[2])

            def terminal_values():
                return deepcopy((tuple(terminal[name] for name in ('key','opened','saved','row','encoded','snapshot','result')),
                    {k:v for k,v in terminal['closeBinding'].items() if k not in ('process','pipe')},
                    self._release_snapshot(terminal_objects[0], terminal_objects[1], terminal_objects[2]),
                    [(getattr(actor,'bundle',None),getattr(actor,'diagnostics',None),process.pid,process.group,
                      tuple(getattr(process,name,None) for name in ('handle','job','thread')),
                      tuple(getattr(pipe,name,None) for name in ('input','output','child_input','child_output')))
                     for actor,process,pipe,_ in resource_objects]))

            terminal_snapshot = terminal_values()
            row_raw = canonical(row)

            def objects():
                self._terminal_memory(terminal)
                require(self.readers.get(key) is held and held['reader'] is reader
                        and held['original'] == original_raw and held['input'] == frame_raw
                        and held['deadline'] == deadline and held['terminal'] is terminal
                        and reader.process is process and reader.bundle == bundle and reader.diagnostics == diagnostics
                        and reader.verify_owner is verify and reader.native is native and reader.adapter is adapter
                        and (reader.ready_check_artifact is None or reader.ready_check_artifact['artifactId'] == row['artifact'])
                        and self.runtime.managed is managed and self.runtime.process_adapter is adapter
                        and managed.transport is transport and transport.native is native
                        and self.registry is registry and self.runtime.registry is registry and registry.root == root
                        and self.runtime._offline is self,
                        'PROOF_MISMATCH', '元reader・原文・生成bindingの保持先が変更されました')
                require(self.retired.get(terminal['key']) is terminal
                        and all(terminal[name] is obj for name,obj in zip(
                            ('controller','workers','history','resources','closeBinding'), terminal_objects))
                        and len(terminal['resources']) == len(resource_objects)
                        and all(len(current) == len(expected) and all(a is b for a,b in zip(current,expected))
                                for current,expected in zip(terminal['resources'],resource_objects))
                        and all(actor.process is process and actor.pipe is pipe and actor.creation_resources is resources
                                for actor,process,pipe,resources in resource_objects)
                        and all(worker.controller is terminal_objects[0] for worker in terminal_objects[1])
                        and all(all(getattr(actor,name,None) is obj for name,obj in zip(
                                    ('native','adapter','verify_owner','controller'), expected[:4]))
                                and getattr(process,'process',None) is expected[4]
                                for (actor,process,_,_),expected in zip(resource_objects,actor_objects))
                        and all(terminal['closeBinding'].get(name) is obj for name,obj in zip(('process','pipe'),close_objects))
                        and len(terminal_objects[2]) == len(history_objects)
                        and all(len(current) == len(expected) and all(a is b for a,b in zip(current,expected))
                                for current,expected in zip(terminal_objects[2],history_objects))
                        and terminal_values() == terminal_snapshot and canonical(row) == row_raw,
                        'PROOF_MISMATCH', '確定した元終端bindingが変更されました')

            def checked(connection):
                self._terminal(connection, token, terminal, deadline)
                self.runtime._binding(dict(self.registry._instance(connection, instance)), row)
                require(self.retired.get((instance, open_operation_id)) is terminal,
                        'STALE_OPERATION', '元終端の退避先が変更されました')
                for table in ('operations','runtime_operations','reference_operations','writer_operations','snapshot_operations'):
                    require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (operation_id,)).fetchone(),
                            'OPERATION_CONFLICT', '別操作のIDを読取に再使用しません')

            def verify():
                current = self.read_ready_original(token, open_operation_id=open_operation_id, ready=ready, deadline=deadline)
                require(canonical(current) == original_raw,
                        'PROOF_MISMATCH', '読取前のReady原文と元終端証拠が変更されました')
                with self.runtime._transaction(instance, read_only=True) as connection:
                    checked(connection)
                    objects()
                require(time.monotonic() < deadline, 'START_TIMEOUT', 'Ready公式読取の照合後に期限を超過しました')

            with self.runtime._transaction(instance) as connection:
                checked(connection)
                require(not any(k[1] == operation_id for k in self.readers),
                        'OPERATION_CONFLICT', '同じ読取を再実行しません')
                require(not any(value['terminal'] is terminal and not value.get('completed') for value in self.readers.values()),
                        'WRITER_PENDING', '不明又は未完了の元readerを保全します')
                require(time.monotonic() < deadline, 'START_TIMEOUT', 'Ready読取枠の取得前に期限を超過しました')
                reader = OfflineProcess(bundle, diagnostics, native=native, verify=verify, process_adapter=adapter)
                process = reader.process
                held = dict(reader=reader, terminal=terminal, original=original_raw, input=frame_raw, deadline=deadline)
                self.readers[key] = held
            verify()
            observed, binding = reader.verify_ready(frame, deadline)
            held.update(request=reader.request, response=reader.ready_check_response, binding=binding)

            def finished():
                require(held.get('binding') is binding and reader.ready_check_binding is binding,
                        'PROOF_MISMATCH', '元readerの完成binding保持先が変更されました')
                binding[1]()
                require(observed['artifactId'] == row['artifact']
                        and reader.ready_check_input == frame_raw and reader.ready_check_result == canonical(observed)
                        and reader.record.get('readyRead') == observed
                        and reader.ready_check_release == {'handle':'RELEASED','pipe':'RELEASED'}
                        and held['request'] == reader.request and digest(held['request']) == observed['requestSha256']
                        and held['response'] == reader.ready_check_response and digest(held['response']) == observed['responseSha256'],
                        'PROOF_MISMATCH', '元readerの公式読取・正常解放記録が一致しません')

            verify()
            finished()
            result = dict(original, status='OFFLINE_READY_VERIFIED', operationId=operation_id,
                          requestSha256=fingerprint, reader=observed)
            encoded = canonical(result)
            held['result'] = encoded
            returned = deepcopy(result)
            complete = False

            def proof_memory():
                objects()
                finished()
                require(held.get('completed', False) is complete and held.get('result') == encoded
                        and canonical(returned) == encoded,
                        'PROOF_MISMATCH', '確定した読取証拠と完成状態が変更されました')
                require(time.monotonic() < deadline, 'START_TIMEOUT', '確定読取証拠の引渡し期限を超過しました')

            def evidence(current=None):
                self._terminal_resources(terminal, deadline)
                files(current)
                proof_memory()
                return row_raw

            evidence()
            verify()
            finished()
            proof_memory()
            require(time.monotonic() < deadline, 'START_TIMEOUT', 'Ready公式読取の返却前に期限を超過しました')
            held['completed'] = complete = True
            return returned, (evidence, proof_memory)

    def reserve_start(self, token, *, operation_id, read_operation_id, open_operation_id, ready, deadline, current_check=None):
        """確定した公式Ready証拠から新startと全portsを原子的に予約する。M生成・GRANT・RUNNINGは後続。

        予約後は旧停止状態を再要求しない。commit/返却不明でも元意図と読取資源を保持し、
        同ID/別IDで再生成や予約の置換を行わない。公開startの迂回入口にはしない。
        """
        token, ready = deepcopy((token, ready))
        fields(token, TOKEN)
        identifier(operation_id, 'operationId')
        _canonical_uuid(read_operation_id)
        identifier(open_operation_id, 'openOperationId')
        require(len({operation_id,read_operation_id,open_operation_id}) == 3,
                'OPERATION_CONFLICT', 'start・公式読取・元openは別IDが必要です')
        instance = token['instanceId']
        key = instance, operation_id
        request = canonical(dict(command='offline-start', token=token, ready=ready,
            readOperationId=read_operation_id, openOperationId=open_operation_id))
        fingerprint = digest(request)
        with self.registry.resource_use(), self.runtime._lock_wait(deadline):
            require(time.monotonic() < deadline, 'START_TIMEOUT', '新startの受付期限を超過しました')
            self.check_closed_ready(token, open_operation_id=open_operation_id, ready=ready, deadline=deadline)
            with self.runtime._transaction(instance, read_only=True) as connection:
                for table in ('operations','runtime_operations','reference_operations','writer_operations','snapshot_operations'):
                    require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (operation_id,)).fetchone(),
                            'OPERATION_CONFLICT', 'start IDが他の操作で使用されています')
            with self.runtime._control_lock:
                require(key not in self.starts and not any(k[0] == instance for k in self.starts),
                        'OPERATION_CONFLICT', '元start意図を保全し再予約しません')
                nonce = uuid.uuid4().hex
                run_id = 'run-' + nonce
                held = dict(input=request, deadline=deadline, run=run_id, nonce=nonce, reserved=False, returned=False)
                self.starts[key] = held
            packet = self._verify_ready(token, operation_id=read_operation_id,
                open_operation_id=open_operation_id, ready=ready, deadline=deadline)
            proof, (evidence, proof_memory) = packet
            held['proof'] = packet
            row = json.loads(evidence())
            terminal = self.retired[(instance, open_operation_id)]
            generation = self.runtime._active_generation(row)
            managed = self.runtime.managed
            approval, profile = managed.approved(row, generation)
            approval_raw = canonical(approval)
            inventory = self.registry.inventory
            declaration = next(v for v in inventory['instances'] if v['instanceId'] == instance)
            declaration_raw = canonical(declaration)
            endpoints = instance_endpoints(declaration)
            endpoints_raw = canonical(endpoints)
            stop_id = json.loads(row['runtime_proof'])['stopOperationId']

            def ledger(connection):
                records = [connection.execute(sql, (value,)).fetchone() for sql,value in (
                    ('SELECT * FROM runs WHERE id=?', row['run_id']),
                    ('SELECT * FROM run_admission WHERE run=?', row['run_id']),
                    ('SELECT * FROM runtime_operations WHERE id=?', stop_id),
                    ('SELECT * FROM runtime_operations WHERE id=?', open_operation_id))]
                require(all(record is not None for record in records),
                        'PROOF_REQUIRED', '元停止run・閉門・CLOSED終端の正本を保全します')
                return canonical(dict(zip(('run','gate','stop','opened'), map(dict, records)),
                    writers=[dict(v) for v in connection.execute('SELECT * FROM writer_sessions WHERE instance=? ORDER BY id', (instance,))],
                    calls=[dict(v) for v in connection.execute('SELECT o.* FROM writer_operations o JOIN writer_sessions w ON w.id=o.session WHERE w.instance=? ORDER BY o.id', (instance,))]))

            with self.runtime._transaction(instance, read_only=True) as connection:
                self._terminal_state(connection, token, terminal, deadline)
                original_ledger = ledger(connection)
            state = _json(proof['stateJson'].encode('utf-8'))
            intent = dict(instanceId=instance, leaseId=row['lease'], epoch=row['epoch'],
                generationId=row['generation'], artifactId=row['artifact'], runId=run_id, nonce=nonce,
                openOperationId=open_operation_id, readOperationId=read_operation_id, ready=deepcopy(ready),
                readyProofSha256=digest(canonical(proof)), previousRunId=row['run_id'],
                previousLedgerSha256=digest(original_ledger), approvalSha256=digest(approval_raw),
                profileSha256=digest(profile), runtimeManifestHash=approval['runtimeManifestHash'],
                worldRoot=state['paths']['worldRoot'], shopDirectory=state['paths']['shopDirectory'],
                endpoints=endpoints)
            encoded = json.dumps(dict(offlineStart=intent))
            held['intent'] = encoded
            expected_row = dict(row, state='STARTING', pending=1, run_id=run_id, runtime_proof=None, clean_proof=None)
            result = dict(status='OFFLINE_START_RESERVED', instanceId=instance, operationId=operation_id,
                requestSha256=fingerprint, runId=run_id, readyProofSha256=intent['readyProofSha256'])
            result_raw = canonical(result)
            reserved = complete = False
            held['result'] = result_raw

            def memory():
                proof_memory()
                require(self.starts.get(key) is held and held.get('input') == request and held.get('deadline') == deadline
                        and held.get('run') == run_id and held.get('nonce') == nonce and held.get('proof') is packet
                        and held.get('intent') == encoded and canonical(proof) == packet_raw
                        and held.get('reserved') is reserved and held.get('returned') is complete
                        and held.get('binding') is verify_reserved and held.get('result') == result_raw
                        and self.registry.inventory is inventory
                        and any(item is declaration for item in inventory['instances'])
                        and canonical(declaration) == declaration_raw and canonical(endpoints) == endpoints_raw,
                        'PROOF_MISMATCH', '新start意図と確定読取証拠の保持先が変更されました')
                require(self.runtime.managed is managed and managed.approvals.get(instance) is approval
                        and canonical(approval) == approval_raw,
                        'PROFILE_MISMATCH', '独立profile承認の保持object又は値が変更されました')
                require(time.monotonic() < deadline, 'START_TIMEOUT', '新startの固定期限を超過しました')

            packet_raw = canonical(proof)

            def inputs():
                require(evidence(current_check) == canonical(row), 'PROOF_MISMATCH', '元停止rowの固定証拠が一致しません')
                current, raw = managed.approved(row, generation)
                require(self.runtime.managed is managed and current is approval
                        and canonical(current) == approval_raw and raw == profile,
                        'PROFILE_MISMATCH', '独立profile承認の保持object又は原文が変更されました')
                memory()

            def pending(connection, published=None):
                current = self.registry._instance(connection, instance)
                self.registry._token(current, token)
                expected = expected_row if published is None else dict(expected_row, state='RUNNING', pending=0, runtime_proof=published[0])
                require(all(current[k] == expected[k] for k in expected if k != 'expires') and ledger(connection) == original_ledger,
                        'STALE_OPERATION', '新start予約又は元CLOSED/終了正本が変更されました')
                operation = connection.execute('SELECT * FROM runtime_operations WHERE id=?', (operation_id,)).fetchone()
                require(operation is not None and operation['instance'] == instance and operation['command'] == 'start'
                        and operation['status'] == ('PENDING' if published is None else 'COMPLETED') and operation['fingerprint'] == fingerprint
                        and operation['response'] == (encoded if published is None else published[0]),
                        'STALE_OPERATION', '新start操作の元指紋・意図が一致しません')
                refs = [dict(v) for v in connection.execute('SELECT * FROM reservations WHERE instance=?', (instance,))]
                require(refs == ([dict(operation=operation_id, reference='artifact:'+row['artifact'],
                                     instance=instance, lease=row['lease'], epoch=row['epoch'])] if published is None else [])
                        and not connection.execute("SELECT 1 FROM runtime_operations WHERE instance=? AND status!='COMPLETED' AND id!=?",
                                                   (instance,operation_id)).fetchone(),
                        'STALE_OPERATION', '新start専有予約と元offline予約不在が一致しません')
                self.registry.generation._observe_endpoints(connection, current, {'endpoints':endpoints})
                memory()

            def verify_reserved(connection=None, *, memory_only=False, published=None):
                if connection is not None:
                    pending(connection, published)
                    return encoded
                if not memory_only:
                    inputs()
                    with self.runtime._transaction(instance, read_only=True) as connection:
                        pending(connection, published)
                memory()
                require(canonical(returned) == result_raw, 'PROOF_MISMATCH', '新start予約の返却証拠が変更されました')
                require(time.monotonic() < deadline, 'START_TIMEOUT', '新start予約の返却期限を超過しました')
                return encoded

            held['binding'] = verify_reserved
            inputs()
            with self.runtime._transaction(instance) as connection:
                self._terminal_state(connection, token, terminal, deadline)
                current = self.registry._instance(connection, instance)
                require(all(current[k] == row[k] for k in row if k != 'expires') and ledger(connection) == original_ledger,
                        'STALE_OPERATION', '新start予約前の正本が変更されました')
                for table in ('operations','runtime_operations','reference_operations','writer_operations','snapshot_operations'):
                    require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (operation_id,)).fetchone(),
                            'OPERATION_CONFLICT', 'start IDが他の操作で使用されています')
                memory()
                reserve(connection, instance, run_id, endpoints)
                connection.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)",
                                   (operation_id,fingerprint,'start',instance,'PENDING',encoded))
                connection.execute("UPDATE instances SET state='STARTING',pending=1,run_id=?,runtime_proof=NULL,clean_proof=NULL WHERE id=?",
                                   (run_id,instance))
                connection.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',
                                   (operation_id,'artifact:'+row['artifact'],instance,row['lease'],row['epoch']))
                connection.execute('INSERT INTO audit(at,operation,command) VALUES (?,?,?)',
                                   (self.registry.clock(),operation_id,'start'))
                pending(connection)
            held['reserved'] = reserved = True
            returned = deepcopy(result)
            verify_reserved()
            held['returned'] = complete = True
            return returned, verify_reserved

    def record_start(self, token, *, operation_id, read_operation_id, open_operation_id, ready, deadline,
                     prepare=False, create=False, startup=False, connect=False, grant=False, acknowledge=False,
                     record_proof=False, finish=False, finish_release=None):
        """新start/portsの固定結果からrunと閉門正本を作る。

        prepare指定時はdescriptorとCLOSED/1まで結束し、結果と(session, callback)を返す。
        create指定時は同じsessionでMを生成し(session, process, callback)を返す。
        startup指定時はCLOSED/1の受入を行い(session, process, startup, callback)を返す。
        connect指定時は同じ枠でacceptし、認証済み接続の固定証拠まで引き継ぐ。
        grant指定時は同じ接続でHELLO/GRANTまで交換する。BOUND/ACKは含めない。
        acknowledge指定時は元GRANTからBOUND/ACKを交換し、同じ組と完成callbackを返す。
        record_proof指定時は同一期限でSTARTED proofを待ち、元ACKとrunへCAS記録する。
        finish指定時は同じ完成証拠でOPEN/RUNNINGとstart終端を原子的に確定する。
        未指定時はCLOSED/0までの結果とcallbackを返す。GRANTは行わない。
        """
        require(not finish or record_proof, 'INVALID_INPUT', '起動確定には元proofの記録が必要です')
        token = deepcopy(token)
        grant_phase = running = current_candidate = current_files = None

        def current(read, path, ready_raw, state, originals):
            nonlocal current_candidate, current_files
            if current_files is None:
                current_files = read, path, ready_raw, state, originals
            raw = read(path)
            if grant_phase is None:
                require(raw == ready_raw, 'PROOF_MISMATCH', 'GRANT直前まで現在Ready原文の一致が必要です')
            else:
                if current_candidate is None and raw != ready_raw:
                    current_candidate = raw, digest(raw)
                status = _json(running) if running is not None else None
                if current_candidate is None and status is not None:
                    observed = read(path.parent / 'states' / (status['sha256'] + '.json'))
                    current_candidate = observed, digest(observed)
                if current_candidate is None:
                    return
                observed, fingerprint = current_candidate
                target = _json(observed)
                require(type(target) is dict and target.keys() == state.keys() and type(target['format']) is int
                        and type(target['stateRevision']) is int
                        and state['stateRevision'] < 2**63 - 1
                        and target['stateRevision'] == state['stateRevision'] + 1
                        and target['previousStateSha256'] == digest(ready_raw)
                        and target['runUuid'] != state['runUuid']
                        and target['payload'] == dict(
                            type='io.github.sushiericworkspace.sushiericdatamodel.database.run.WorldRunPhase.Running',
                            owner=context['owner'], management=context['management'])
                        and all(target[name] == state[name] for name in
                                ('format','worldUuid','generation','paths','originSha256')),
                        'PROOF_MISMATCH', 'BOUNDのRunningが元Readyの正当な遷移と一致しません')
                _canonical_uuid(target['runUuid'])
                require(read(path.parent / 'states' / (fingerprint + '.json')) == observed
                        and raw in (ready_raw, observed),
                        'PROOF_MISMATCH', '現在Runningと認証済みBOUND/不変原文が一致しません')
                if status is not None:
                    require(fingerprint == status['sha256'] and target == status['state'],
                            'PROOF_MISMATCH', '保持したRunning候補と認証済みBOUNDが一致しません')

        def advance(phase, packet):
            nonlocal grant_phase, running
            if phase == 'grant':
                require(grant_phase is None, 'CONTROL_BUSY', '元GRANTの段階を再採用しません')
                grant_phase = packet
            else:
                require(phase == 'bound' and grant_phase is not None and running is None,
                        'CONTROL_BUSY', '認証済みBOUNDを一度だけ継承します')
                running = packet['payload']['statusJson'].encode('utf-8')

        packet = self.reserve_start(token, operation_id=operation_id, read_operation_id=read_operation_id,
            open_operation_id=open_operation_id, ready=ready, deadline=deadline,
            current_check=current if acknowledge else None)
        reservation, verify_reserved = packet
        intent = json.loads(verify_reserved())['offlineStart']
        runtime, registry = self.runtime, self.registry
        if prepare:
            root = registry.root
            approval = runtime.managed.approvals.get(intent['instanceId'])
        instance, run_id = intent['instanceId'], intent['runId']
        key = instance, operation_id
        with runtime._transaction(instance, read_only=True) as connection:
            verify_reserved(connection)
            row = dict(registry._instance(connection, instance))
        generation = runtime._active_generation(row)
        manifest = regular_bytes(generation / 'runtime/runtime.json')
        require(digest(manifest) == intent['runtimeManifestHash'],
                'PROFILE_MISMATCH', '新runに用いるmanifest原文が固定承認と一致しません')
        diagnostics = runtime._paths(instance) / 'diagnostics' / run_id
        run = dict(runId=run_id, instanceId=instance, generationId=intent['generationId'],
            startOperationId=operation_id, startRequestHash=reservation['requestSha256'],
            artifactId=intent['artifactId'], nonce=intent['nonce'], identity=None,
            proofPath=str(diagnostics / 'proof.json'), cwd=str(generation.resolve()),
            manifestHash=digest(manifest), endpoints=deepcopy(intent['endpoints']),
            stopRequested=False, jvmAndChildrenExited=False, runtimeSaveCompleted=False)
        record = json.dumps(run)
        original_record = record
        gate = dict(run=run_id, instance=instance, lease=intent['leaseId'], epoch=intent['epoch'],
            generation=intent['generationId'], nonce=intent['nonce'], gate='CLOSED', revision=0,
            descriptor_hash=None, start_operation=operation_id)
        gate_raw, reservation_raw = canonical(gate), canonical(reservation)
        recorded = complete = False
        preparation = None
        launch = None
        creation_run = creation_raw = creation = None
        held = dict(packet=packet, run=run, record=record, deadline=deadline, recorded=False, returned=False)
        with runtime._control_lock:
            require(key not in self.start_records, 'OPERATION_CONFLICT', '元run記録を保全し再生成しません')
            self.start_records[key] = held

        def memory():
            verify_reserved(memory_only=True)
            if preparation is not None:
                require(held.get('preparation') is preparation,
                        'PROOF_MISMATCH', '元descriptor準備結果の保持先が変更されました')
                preparation[1][1]()
                session, (raw, _) = preparation[:2]
                descriptor = session['descriptor']
                require(session['approval'] is approval
                        and digest(canonical(session['approval'])) == intent['approvalSha256']
                        and digest(session['profile_raw']) == intent['profileSha256'],
                        'PROFILE_MISMATCH', '準備したprofile原文と承認objectが元start意図と一致しません')
                require(raw == canonical(descriptor) and descriptor['managementRoot'] == str(root)
                        and all(descriptor[name] == intent[name] for name in
                                ('instanceId','leaseId','epoch','generationId','artifactId','runtimeManifestHash','worldRoot','shopDirectory'))
                        and descriptor['runtimeRunId'] == run_id and descriptor['startNonce'] == intent['nonce'],
                        'PROOF_MISMATCH', '元Readyと同じ新runのdescriptor原文が必要です')
                require(session['launch'] == launch, 'PROOF_MISMATCH', '準備したlaunchが元runと固定generationへ結束されていません')
            if creation_run is not None:
                require(held.get('creation_run') is creation_run and json.dumps(creation_run) == creation_raw,
                        'PROOF_MISMATCH', 'M生成中の元run原文が変更されました')
            require(self.runtime is runtime and self.registry is registry and runtime.registry is registry
                    and self.start_records.get(key) is held and held.get('packet') is packet
                    and packet[0] is reservation and packet[1] is verify_reserved
                    and canonical(reservation) == reservation_raw and held.get('run') is run
                    and json.dumps(run) == original_record and held.get('record') == original_record
                    and held.get('deadline') == deadline and held.get('recorded') is recorded
                    and held.get('returned') is complete and held.get('binding') is verify_recorded,
                    'PROOF_MISMATCH', '新runの固定入力と保持先が変更されました')
            require(time.monotonic() < deadline, 'START_TIMEOUT', '新run記録の固定期限を超過しました')

        def writing(connection, value, published=None):
            verify_reserved(connection, published=published)
            memory()
            require(value == record, 'PROOF_MISMATCH', '保存する新run原文が変更されました')

        def saved(connection, published=None):
            writing(connection, record, published)
            value = connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            admission = connection.execute('SELECT * FROM run_admission WHERE run=?', (run_id,)).fetchone()
            require(value is not None and dict(value) == dict(id=run_id, instance=instance,
                    generation=intent['generationId'], record=record)
                    and admission is not None and canonical(dict(admission)) == (gate_raw if published is None else published[1]),
                    'STALE_RUN', '新run原文と閉門正本が変更されました')

        def update(value, *, expected):
            nonlocal record, creation_raw
            require(value is creation_run and expected == record
                    and all(value[k] == v for k, v in run.items() if k != 'identity')
                    and set(value) <= set(run) | {'commandHash', 'creation'},
                    'PROOF_MISMATCH', '共通生成入口の元runと更新元原文が一致しません')
            target = json.dumps(value)
            creation_raw = target

            def writing_update(connection, encoded):
                verify_reserved(connection)
                memory()
                current = connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
                admission = connection.execute('SELECT * FROM run_admission WHERE run=?', (run_id,)).fetchone()
                require(encoded == target and current is not None
                        and dict(current) in (dict(id=run_id, instance=instance, generation=intent['generationId'], record=record),
                                              dict(id=run_id, instance=instance, generation=intent['generationId'], record=target))
                        and admission is not None and canonical(dict(admission)) == gate_raw,
                        'STALE_RUN', 'M生成記録の元原文又はCLOSED/1が変更されました')

            require(runtime._record(value, expected=record, verify=writing_update) == target,
                    'PROOF_MISMATCH', 'M生成記録の保存原文が一致しません')
            record = target
            return record

        result = dict(status='OFFLINE_START_RECORDED', instanceId=instance, operationId=operation_id,
            runId=run_id, recordSha256=digest(record.encode('utf-8')), admissionSha256=digest(gate_raw))
        result_raw = canonical(result)

        def verify_recorded(connection=None, *, memory_only=False, published=None):
            if connection is not None:
                saved(connection, published)
            elif not memory_only:
                verify_reserved(published=published)
                with runtime._transaction(instance, read_only=True) as connection:
                    saved(connection, published)
            memory()
            require(canonical(returned) == result_raw, 'PROOF_MISMATCH', '新run記録の返却結果が変更されました')
            require(time.monotonic() < deadline, 'START_TIMEOUT', '新run記録の返却期限を超過しました')
            return record

        held['binding'] = verify_recorded
        verify_reserved()
        memory()
        require(runtime._record(run, verify=writing) == record,
                'PROOF_MISMATCH', '新run記録の保存結果が一致しません')
        held['recorded'] = recorded = True
        returned = deepcopy(result)
        verify_recorded()
        if prepare:
            preparation = runtime.managed.prepare(row, run, generation, verify=verify_recorded, handoff=connect)
            held['preparation'] = preparation
            session, (raw, check) = preparation[:2]
            leaf = root / 'manager/control' / run_id
            launch = dict(bundle=str(generation/'runtime'), run=str(generation),
                auxiliary=str(leaf/'auxiliary'), privateTmp=str(leaf/'private-tmp'),
                controlDescriptor=descriptor_path(json.loads(raw)), controlDescriptorSha256=digest(raw),
                runtimeManifest=str(generation/'runtime/runtime.json'), proofPath=str(diagnostics/'proof.json'),
                instanceId=instance, generationId=intent['generationId'], startNonce=intent['nonce'])
            verify_recorded()
            session['endpoint'].pin.check(raw)
            verify_recorded()
            prepared_gate = dict(gate, revision=1, descriptor_hash=digest(raw))
            with runtime._transaction(instance) as connection:
                saved(connection)
                changed = connection.execute("UPDATE run_admission SET revision=1,descriptor_hash=? WHERE run=? AND revision=0 AND gate='CLOSED' AND descriptor_hash IS NULL",
                    (digest(raw), run_id))
                require(changed.rowcount == 1, 'STALE_RUN', '元CLOSED/0だけをdescriptorへ結束できます')
                memory()
            gate_raw = canonical(prepared_gate)
            result = dict(result, status='OFFLINE_START_PREPARED', descriptorSha256=digest(raw),
                          admissionSha256=digest(gate_raw))
            result_raw = canonical(result)
            returned = deepcopy(result)
            verify_recorded()
        if create:
            require(preparation is not None, 'PROOF_REQUIRED', '元descriptor準備結果が必要です')
            creation_run = deepcopy(run)
            creation_raw = json.dumps(creation_run)
            held['creation_run'] = creation_run
            diagnostics.mkdir(parents=True, exist_ok=True)
            make_private(diagnostics, directory=True)
            with runtime._create(row, creation_run, generation, diagnostics, run['endpoints'],
                    run['nonce'], run_id, record, deadline,
                    prepared=(preparation[0], verify_recorded, preparation[1][1]()),
                    record=update) as created:
                session, process, check = created
                creation = (session, process, check)
                held['creation'] = creation
                result = dict(result, status='OFFLINE_START_CREATED', recordSha256=digest(record.encode('utf-8')))
                result_raw = canonical(result)
                returned = deepcopy(result)

                def verify_created(connection=None, *, memory_only=False, published=None):
                    require(held.get('creation') is creation and creation[0] is preparation[0],
                            'PROOF_MISMATCH', '元M生成結果の保持先が変更されました')
                    verify_recorded(connection, memory_only=memory_only, published=published)
                    check()
                    return record

                verify_created()
            verify_created()
        if startup:
            managed = runtime.managed
            starter = managed.startup
            session, process, _ = creation
            endpoint = session['endpoint']
            context = managed._handshake_context(creation_run, session)
            verify_created()
            identity = creation_run['identity']
            boot = identity['bootId'] if session['descriptor']['endpoint']['transport'] == 'windows-pipe' else str(uuid.UUID(identity['bootId']))
            require(context['owner'] == dict(hostHash=session['descriptor']['controller']['hostHash'],
                    bootId=boot, pid=identity['pid'], startId=identity['startId']),
                    'PROCESS_MISMATCH', '元M生成証拠と同じnative本人が必要です')
            context_raw = canonical(context)
            held['startup_context'] = context_raw
            launch_raw = canonical(launch)
            proof = preparation[1][1]()
            observed = dict(argv=list(proof[2]), auxiliaryFiles=dict(proof[3]),
                            environment=dict(proof[4]), cwd=proof[5])
            startup_token = dict(instanceId=instance, leaseId=intent['leaseId'], epoch=intent['epoch'],
                                 expectedGeneration=intent['generationId'])
            token_raw = canonical(startup_token)

            def startup_source(candidate, connection=None, *, memory_only=False, published=None):
                verify_created(connection, memory_only=memory_only, published=published)
                require(runtime.managed is managed and managed.startup is starter
                        and held.get('startup_context') == context_raw
                        and canonical(candidate['token']) == token_raw
                        and canonical(candidate['context']) == context_raw
                        and digest(candidate['profile_raw']) == intent['profileSha256']
                        and canonical(candidate['launch_context']) == launch_raw
                        and candidate['observed'] == observed
                        and (candidate['endpoint'] is None or candidate['endpoint'] is endpoint),
                        'PROOF_MISMATCH', '元生成証拠とstartup入力のbindingが一致しません')
                require(time.monotonic() < deadline, 'START_TIMEOUT', 'startup入力の最終比較後に期限を超過しました')

            prepared_startup = starter.admit(startup_token, profile_raw=session['profile_raw'],
                launch_context=deepcopy(launch), observed=deepcopy(observed), context=json.loads(context_raw),
                revision=1, deadline=deadline, endpoint=endpoint, verify=startup_source,
                accept=preparation[2] if connect else None, advance=advance if acknowledge else None)
            candidate, check_startup = prepared_startup[:2]
            held['startup'] = prepared_startup
            checkpoint = ({key:row[key] for key in ('id','lease','epoch','generation','artifact','run_id')},
                          1, digest(preparation[1][0]), run['nonce'])
            result = dict(result, status='OFFLINE_STARTUP_PREPARED', contextSha256=digest(context_raw))
            result_raw = canonical(result)
            returned = deepcopy(result)

            def verify_startup(*, memory_only=False, published=None):
                check_startup(memory_only=memory_only, published=published)
                startup_source(candidate, memory_only=True)
                require(held.get('startup') is prepared_startup
                        and candidate['endpoint'] is endpoint
                        and candidate['origin'] == (operation_id, reservation['requestSha256'])
                        and candidate['checkpoint'] == checkpoint,
                        'PROOF_MISMATCH', '元CLOSED/1のstartup完成証拠が変更されました')
                require(time.monotonic() < deadline, 'START_TIMEOUT', 'startup準備の返却前に期限を超過しました')
                return record

            verify_startup()
            if connect:
                accepted = prepared_startup[2]()
                held['accepted'] = accepted
                result = dict(result, status='OFFLINE_START_ACCEPTED')
                result_raw = canonical(result)
                returned = deepcopy(result)

                def verify_accepted(*, memory_only=False, published=None):
                    verify_startup(memory_only=memory_only, published=published)
                    require(held.get('accepted') is accepted and candidate['connection'] is accepted[0],
                            'PROOF_MISMATCH', '認証済み元接続の確定結果が変更されました')
                    accepted[1]()
                    require(time.monotonic() < deadline, 'START_TIMEOUT', 'accept結果の最終比較後に期限を超過しました')
                    return record

                verify_accepted()
                if grant:
                    granted = prepared_startup[3]()
                    held['granted'] = granted
                    result = dict(result, status='OFFLINE_START_GRANTED',
                                  helloBodySha256=digest(granted[0][4:]), grantBodySha256=digest(granted[1][4:]))
                    result_raw = canonical(result)
                    returned = deepcopy(result)

                    def verify_granted(*, memory_only=False, published=None):
                        verify_accepted(memory_only=memory_only, published=published)
                        require(held.get('granted') is granted,
                                'PROOF_MISMATCH', '元GRANT完成結果の保持先が変更されました')
                        if acknowledge:
                            require(granted[4] is grant_phase and grant_phase == (granted[0], granted[1]),
                                    'PROOF_MISMATCH', 'GRANTと一体の固定段階が変更されました')
                        granted[2](memory_only=memory_only, published=published)
                        require(time.monotonic() < deadline, 'START_TIMEOUT', 'GRANT結果の最終返却期限を超過しました')
                        return record

                    verify_granted()
                    if acknowledge:
                        acknowledged = granted[3]()
                        held['acknowledged'] = acknowledged
                        result = dict(result, status='OFFLINE_START_ACKNOWLEDGED',
                                      boundBodySha256=digest(acknowledged[0][4:]),
                                      ackBodySha256=digest(acknowledged[1][4:]))
                        result_raw = canonical(result)
                        returned = deepcopy(result)

                        def verify_acknowledged(*, memory_only=False, published=None):
                            require(time.monotonic() < deadline, 'START_TIMEOUT', 'ACK継承照合の開始前に元起動期限を超過しました')
                            verify_granted(memory_only=memory_only, published=published)
                            require(held.get('acknowledged') is acknowledged,
                                    'PROOF_MISMATCH', '元ACK完成結果の保持先が変更されました')
                            acknowledged[2](memory_only=memory_only, published=published)
                            require(time.monotonic() < deadline, 'START_TIMEOUT', 'ACK結果の最終返却期限を超過しました')
                            return record

                        verify_acknowledged()
        if record_proof:
            proof_path = diagnostics / 'proof.json'
            initializing = None

            def owner():
                verify_acknowledged(memory_only=True)
                require(held.get('proof_initializing') is initializing,
                        'PROOF_MISMATCH', '元INITIALIZING原文の保持先が変更されました')
                require(process.poll() is None, 'START_FAILED', '起動proof確定前に元Mが終了しました')
                require(time.monotonic() < deadline, 'START_TIMEOUT', 'M poll後に元起動期限を超過しました')
                runtime_process.require_identity(creation_run['identity'], runtime.process_adapter.identity(process.pid))
                require(time.monotonic() < deadline, 'START_TIMEOUT', 'proof観測後に元起動期限を超過しました')

            while True:
                owner()
                verify_acknowledged()
                owner()
                try:
                    proof_path.lstat()
                except FileNotFoundError:
                    proof_raw = None
                else:
                    require(time.monotonic() < deadline, 'START_TIMEOUT', 'proof実読直前に元起動期限を超過しました')
                    proof_raw = regular_bytes(proof_path, maximum=16 * 1024 * 1024)
                owner()
                verify_acknowledged()
                owner()
                if proof_raw is not None:
                    value = _json(proof_raw)
                    require(time.monotonic() < deadline, 'START_TIMEOUT', 'proof解析後に元起動期限を超過しました')
                    if value.get('phase') != 'INITIALIZING':
                        proof = runtime._proof(creation_run, generation, proof=value, deadline=deadline)
                        owner()
                        break
                    runtime._proof(creation_run, generation, proof=value, initializing=True, deadline=deadline)
                    owner()
                    verify_acknowledged()
                    owner()
                    if initializing is None:
                        initializing = proof_raw, digest(proof_raw)
                        held['proof_initializing'] = initializing
                    else:
                        require(proof_raw == initializing[0], 'PROOF_MISMATCH', '元INITIALIZING原文が変更されました')
                else:
                    require(initializing is None, 'PROOF_MISMATCH', '公開済みINITIALIZING原文が失われました')
                require(time.monotonic() < deadline, 'START_TIMEOUT', 'proof待機前に元起動期限を超過しました')
                time.sleep(0.1)
            require(proof['startup']['roots']['world'] == intent['worldRoot'],
                    'ROOT_MISMATCH', '起動proofと元Readyのworld rootが一致しません')
            proof_run = json.loads(record)
            proof_run.update(startupProofJson=proof_raw.decode('utf-8'), startupProofSha256=digest(proof_raw),
                startupAcknowledgement=dict(
                    helloBodySha256=result['helloBodySha256'], grantBodySha256=result['grantBodySha256'],
                    boundBodySha256=result['boundBodySha256'], ackBodySha256=result['ackBodySha256'],
                    statusJson=running.decode('utf-8'), statusJsonSha256=digest(running)))
            proof_record = json.dumps(proof_run)
            proof_packet = proof_raw, proof_run, proof_record, acknowledged, verify_acknowledged, proof, canonical(proof), initializing
            held['proof'] = proof_packet

            def proof_memory():
                verify_acknowledged(memory_only=True)
                require(held.get('proof') is proof_packet and proof_packet[3] is acknowledged
                        and proof_packet[4] is verify_acknowledged and json.dumps(proof_run) == proof_record
                        and proof_packet[5] is proof and canonical(proof) == proof_packet[6]
                        and proof_packet[7] is initializing and held.get('proof_initializing') is initializing,
                        'PROOF_MISMATCH', '元ACKと起動proofの固定記録が変更されました')
                require(time.monotonic() < deadline, 'START_TIMEOUT', '起動proofの純比較後に期限を超過しました')

            def proof_files(*, final=False):
                if final:
                    read, path, ready_raw, state, originals = current_files
                    for original_path, raw, maximum in originals:
                        require(read(original_path, maximum) == raw,
                                'PROOF_MISMATCH', '最終Ready不変原文が変更されました')
                    current(read, path, ready_raw, state, originals)
                require(time.monotonic() < deadline, 'START_TIMEOUT', 'proof再読直前に元起動期限を超過しました')
                require(regular_bytes(proof_path, maximum=16 * 1024 * 1024) == proof_raw,
                        'PROOF_MISMATCH', '保持した起動proofの実原文が変更されました')
                owner()
                if not final:
                    require_listeners(run['endpoints'], proof['startup']['listeners'], runtime.process_adapter.sockets(process.pid))
                proof_memory()

            def writing_proof(connection, encoded):
                verify_reserved(connection)
                proof_memory()
                value = connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
                admission = connection.execute('SELECT * FROM run_admission WHERE run=?', (run_id,)).fetchone()
                require(encoded == proof_record and value is not None
                        and dict(value) in (dict(id=run_id, instance=instance, generation=intent['generationId'], record=record),
                                            dict(id=run_id, instance=instance, generation=intent['generationId'], record=proof_record))
                        and admission is not None and canonical(dict(admission)) == gate_raw,
                        'STALE_RUN', 'ACK/proof記録の元run原文又はCLOSED/1が変更されました')

            verify_acknowledged()
            proof_files()
            proof_memory()
            require(runtime._record(proof_run, expected=record, verify=writing_proof) == proof_record,
                    'PROOF_MISMATCH', 'ACK/proof正本の保存結果が一致しません')
            record = proof_record
            result = dict(result, status='OFFLINE_START_PROOF_RECORDED', startupProofSha256=digest(proof_raw),
                          recordSha256=digest(record.encode('utf-8')))
            result_raw = canonical(result)
            returned = deepcopy(result)

            def verify_start_proof(*, memory_only=False, published=None):
                verify_acknowledged(memory_only=memory_only, published=published)
                if not memory_only:
                    if finish and 'receiver' in held:
                        require(time.monotonic() < deadline, 'START_TIMEOUT', '受信thread観測前に起動期限を超過しました')
                        require(receiver.is_alive(), 'CONTROL_UNKNOWN', '元接続の受信threadが起動していません')
                        require(time.monotonic() < deadline, 'START_TIMEOUT', '受信thread観測後に起動期限を超過しました')
                    proof_files()
                    with runtime._transaction(instance, read_only=True) as connection:
                        saved(connection, published)
                        require(operations.readback(connection, instance, granted[0]) == granted[1]
                                and operations.readback(connection, instance, acknowledged[0]) == acknowledged[1],
                                'PROOF_MISMATCH', '起動proof観測後に元GRANT/ACK正本が変更されました')
                        proof_memory()
                    proof_files(final=True)
                proof_memory()
                return record

            verify_start_proof()
            if finish:
                result = dict(result, status='OFFLINE_START_RUNNING', generationId=intent['generationId'],
                              running=True, snapshotClean=False)
                open_gate = dict(json.loads(gate_raw), gate='OPEN', revision=2)
                open_raw = canonical(open_gate)
                result['admissionSha256'] = digest(open_raw)
                result_raw = canonical(result)
                published = (json.dumps(result), open_raw)
                completion = (published, proof_packet, verify_start_proof, result_raw)
                held['completion'] = completion
                committed = False
                held['committed'] = False
                returned = deepcopy(result)

                verify_start_proof()
                control = managed._bind_session(startup_token, creation_run, session, context)
                control_raw = canonical(context), canonical(control[2])
                connection_refs = accepted[0].peer, accepted[0].channel, accepted[0].endpoint
                held['control'] = control

                def control_memory():
                    require(held.get('control') is control and control[0] is session
                            and managed.sessions.get(instance) is session
                            and session.get('context') is control[1] is context
                            and session.get('token') is control[2]
                            and (canonical(context), canonical(control[2])) == control_raw
                            and control_raw == (context_raw, token_raw)
                            and runtime.game_processes.get(run_id) is control[3]
                            and control[3][0] is process and runtime.processes.get(run_id) is process
                            and starter.sessions.get(instance) is candidate
                            and candidate.get('connection') is accepted[0]
                            and endpoint.connection is accepted[0]
                            and accepted[0].peer is connection_refs[0]
                            and accepted[0].channel is connection_refs[1]
                            and accepted[0].endpoint is connection_refs[2],
                            'PROOF_MISMATCH', '監督sessionと元M・認証済み接続の保持bindingが変更されました')

                def receive():
                    finish_release.wait()
                    control_memory()
                    with runtime._lock_wait(time.monotonic() + 30), runtime._transaction(instance, read_only=True) as connection:
                        current_row, gate, operation = starter._checkpoint(connection, candidate)
                        if operation['status'] == 'PENDING':
                            require(current_row['state'] == 'STARTING' and current_row['pending']
                                    and gate['gate'] == 'CLOSED' and gate['revision'] == 1,
                                    'STALE_RUN', '未確定起動の元閉門予約が変更されました')
                            return False
                        require(operation['status'] == 'COMPLETED' and operation['response'] == published[0]
                                and ((current_row['state'] == 'RUNNING' and not current_row['pending']
                                      and current_row['runtime_proof'] == published[0]
                                      and gate['gate'] == 'OPEN' and gate['revision'] == 2)
                                     or (current_row['state'] == 'STOPPING' and current_row['pending']
                                         and gate['gate'] == 'BLOCKED' and gate['revision'] >= 3)),
                                'STALE_RUN', '同じ起動確定又は正当な停止予約だけを監督します')
                    control_memory()
                    return True

                control_memory()
                require(time.monotonic() < deadline, 'START_TIMEOUT', '受信開始前に起動期限を超過しました')
                receiver = managed.listen(instance, start=receive, channel=accepted[0])
                held['receiver'] = receiver

                def final_memory():
                    proof_memory()
                    control_memory()
                    require(held.get('receiver') is receiver and session.get('thread') is receiver,
                            'PROOF_MISMATCH', '元接続の受信thread保持先が変更されました')
                    require(held.get('completion') is completion and completion[0] is published
                            and completion[1] is proof_packet and completion[2] is verify_start_proof
                            and completion[3] == result_raw and canonical(returned) == result_raw
                            and held.get('committed') is committed,
                            'PROOF_MISMATCH', '起動確定結果と元proof完成証拠の保持先が変更されました')
                    require(time.monotonic() < deadline, 'START_TIMEOUT', '起動確定の固定期限を超過しました')

                verify_start_proof()
                final_memory()
                with runtime._transaction(instance) as connection:
                    final_memory()
                    saved(connection)
                    final_memory()
                    require(operations.readback(connection, instance, granted[0]) == granted[1],
                            'PROOF_MISMATCH', '起動確定前の元HELLO/GRANT正本が変更されました')
                    final_memory()
                    require(operations.readback(connection, instance, acknowledged[0]) == acknowledged[1],
                            'PROOF_MISMATCH', '起動確定前の元GRANT/ACK正本が変更されました')
                    final_memory()
                    _change(connection, row, 1, 'OPEN')
                    final_memory()
                    connection.execute("UPDATE instances SET state='RUNNING',pending=0,runtime_proof=? WHERE id=?",
                                       (published[0], instance))
                    final_memory()
                    connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=?",
                                       (published[0], operation_id))
                    final_memory()
                    require(connection.execute('DELETE FROM reservations WHERE operation=? AND reference=?',
                            (operation_id, 'artifact:'+row['artifact'])).rowcount == 1,
                            'STALE_OPERATION', '元startのartifact予約が一致しません')
                    final_memory()
                    saved(connection, published)
                    final_memory()
                held['committed'] = committed = True

                def verify_running(*, memory_only=False, result=returned):
                    require(result is returned, 'PROOF_MISMATCH', '起動完成時の元返却objectが必要です')
                    final_memory()
                    require(committed, 'STALE_OPERATION', '起動確定のcommit応答がありません')
                    verify_start_proof(memory_only=memory_only, published=published)
                    final_memory()
                    return record

                verify_running()
        held['returned'] = complete = True
        if finish:
            return returned, (creation[0], creation[1], candidate, verify_running)
        if record_proof:
            return returned, (creation[0], creation[1], candidate, verify_start_proof)
        if acknowledge:
            return returned, (creation[0], creation[1], candidate, verify_acknowledged)
        if grant:
            return returned, (creation[0], creation[1], candidate, verify_granted)
        if connect:
            return returned, (creation[0], creation[1], candidate, verify_accepted)
        if startup:
            return returned, (creation[0], creation[1], candidate, verify_startup)
        if create:
            return returned, (creation[0], creation[1], verify_created)
        if prepare:
            return returned, (preparation[0], verify_recorded)
        return returned, verify_recorded

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

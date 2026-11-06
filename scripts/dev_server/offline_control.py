"""停止済み管理runの正本予約とC OPEN/W PREPARED。編集・終了確定は後続。"""

from copy import deepcopy
import json

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

    def _state(self, connection, token, *, reserved):
        row = dict(self.registry._instance(connection, token['instanceId']))
        self.registry._token(row, token)
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
        proof = json.loads(row['runtime_proof']) if row['runtime_proof'] else {}
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

    def _opened(self, connection, token, controller):
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
        return opened, response, saved

    def prepare(self, token, *, operation_id, request, deadline):
        """元OPENを正本照合して独立Wを一度生成する。PREPAREDはDB作用・完了を表さない。

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
                require('worker' not in saved and token['instanceId'] not in self.workers,
                        'WRITER_PENDING', '元Wの生成意図と資源を保全します')
                for table in ('operations','runtime_operations','reference_operations','writer_operations','snapshot_operations'):
                    require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (operation_id,)).fetchone(),
                            'OPERATION_CONFLICT', '既存operationIdを再使用しません')
                saved['worker'] = worker_record
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (json.dumps(saved),opened['operationId']))

            def verify():
                with self.runtime._transaction(token['instanceId'], read_only=True) as connection:
                    _, _, current = self._opened(connection, token, controller)
                    require(current.get('worker') == worker_record, 'STALE_OPERATION', '元Wの予約が変更されました')

            verify()
            controller.acknowledge_startup(saved['startupResponseSha256'])
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
                require(current.get('worker') == worker_record, 'STALE_OPERATION', '元Wの予約が変更されました')
                completed_record = dict(worker_record, startupResponseSha256=digest(canonical(prepared)))
                current['worker'] = completed_record
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                                   (json.dumps(current),opened['operationId']))
            worker_record = completed_record
            worker.acknowledge_startup(worker_record['startupResponseSha256'])
            return prepared

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
                for table in ('operations','runtime_operations','reference_operations','writer_operations','snapshot_operations'):
                    require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (operation_id,)).fetchone(),
                            'OPERATION_CONFLICT', '同じoperationIdを再取得・再作用しません')
                require(not connection.execute('SELECT 1 FROM reservations WHERE instance=?', (row['id'],)).fetchone()
                        and not connection.execute("SELECT 1 FROM runtime_operations WHERE instance=? AND status!='COMPLETED'",
                                                   (row['id'],)).fetchone(), 'WRITER_PENDING', '旧予約と未完了操作を保全します')
                _change(connection, current, revision, 'BLOCKED')
                connection.execute("INSERT INTO runtime_operations VALUES (?,?,'offline-open',?,'PENDING',?)",
                                   (operation_id,fingerprint,row['id'],json.dumps({'binding':expected})))
                connection.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',
                                   (operation_id,'artifact:'+row['artifact'],row['id'],row['lease'],row['epoch']))
                connection.execute('UPDATE instances SET pending=1,clean_proof=NULL WHERE id=?', (row['id'],))
                connection.execute("INSERT INTO audit(at,operation,command) VALUES (?,?,'offline-open')",
                                   (self.registry.clock(),operation_id))

            def verify():
                with self.runtime._transaction(row['id'], read_only=True) as connection:
                    previous = self._reserved(connection,token,operation_id,fingerprint,expected)
                require(self.runtime.process_adapter.identity(previous['identity']['pid']) is None,
                        'PROCESS_MISMATCH', '旧MのPIDが存在します。結果不明を保全します')

            diagnostics = self.runtime._paths(row['id']) / 'diagnostics' / ('offline-' + operation_id)
            controller = OfflineProcess(bundle,diagnostics,native=self.runtime.managed.transport.native,
                                        verify=verify,process_adapter=self.runtime.process_adapter)
            self.controllers[row['id']] = controller
            frame = {'operationId':operation_id,'requestHash':fingerprint,'leaseId':row['lease'],'epoch':row['epoch'],
                     'generation':row['generation'],'drainSha256':digest(canonical(expected)),
                     'worldRoot':str(world),'shopDirectory':str(shop),'worldUuid':ready['worldUuid'],'runUuid':ready['runUuid'],
                     'expectedStateSha256':ready['stateSha256'],'expectedRevision':ready['stateRevision']}
            response = controller.start(frame,deadline)
            verify()
            with self.runtime._transaction(row['id']) as connection:
                self._reserved(connection,token,operation_id,fingerprint,expected)
                saved = {'binding':expected,'startupResponseSha256':digest(canonical(response))}
                connection.execute('UPDATE runtime_operations SET response=? WHERE id=?', (json.dumps(saved),operation_id))
            return response

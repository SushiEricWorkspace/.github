"""P専用の固定C/W生成・限定private要求とW終了観測。Registry更新は呼出元が担う。"""

from copy import deepcopy
import json
import os
import time
from pathlib import Path
import uuid

from . import runtime_process
from .build_inputs import canonical, digest, regular_bytes
from .database_proxy_pipe import ProxyPipe
from .local_ipc import MAX_MESSAGE, encode, make_private, plain_path
from .mod_control_runtime import process_identity
from .mod_control_wire import _hash, _canonical_uuid, _identity
from .offline_controller_bundle import offline_controller_command, offline_worker_command, offline_ready_command
from .registry import RegistryError, require, fields, identifier
from .runtime_bundle import verify_bundle
from .runtime_control import write_json


class OfflineProcess:
    """固定C又は保持Cに属するWを一度だけ生成する。

    Pは呼出前にobjectを保持し、verifyでlease/epoch/generation・遮断/drain・起動排他を
    継続照合する。nativeは既存transportの本人観測を使う。start後もhandleとpipeを保持し、
    例外時は再起動・再送しない。W実終了はobserve_exit、候補確定はcompleteで行う。
    """
    def __init__(self, bundle, diagnostics, *, native, verify, controller=None, process_adapter=runtime_process):
        self.bundle = Path(bundle)
        self.diagnostics = Path(diagnostics)
        self.native = native
        self.verify_owner = verify
        self.controller = controller
        self.adapter = process_adapter
        self.process = process_adapter.OwnedProcess()
        self.pipe = None
        self.creation_resources = None
        self.identity = self.parent = self.data_identity = self.parent_data = None
        self.request = self.response = None
        self.record = None
        self.record_number = 0
        self.started = self.ready = False
        self.startup_acknowledged = False
        self.assignment_started = self.assignment_ready = self.assignment_acknowledged = False
        self.assignment_request = self.assignment_response = None
        self.admission_started = self.candidate_ready = False
        self.admission_request = self.candidate_response = None
        self.exit_started = False
        self.exit_response = None
        self.completion_started = self.completion_ready = self.completion_acknowledged = False
        self.completion_request = self.completion_response = None
        self.game_exit_started = False
        self.game_exit_response = self.game_exit_result = None
        self.close_started = self.close_ready = False
        self.close_request = self.close_response = None
        self.close_binding = None
        self.owner_exit_started = False
        self.owner_exit_response = None
        self.owner_release = None
        self.owner_release_response = None
        self.worker_release = None
        self.worker_release_response = None
        self.writer_closed_response = None
        self.termination_response = None
        self.ready_check_started = False
        self.ready_check_input = self.ready_check_deadline = None
        self.ready_check_response = self.ready_check_result = None
        self.ready_check_release = None
        self.ready_check_artifact = None
        self.ready_check_handles = self.ready_check_fds = self.ready_check_creation_fds = None
        self.ready_check_binding = None
        self.operation_history = {}
        self.session = uuid.uuid4().hex if controller is None else controller.session
        self.nonce = uuid.uuid4().hex if controller is None else controller.nonce

    def archive_completed(self, worker):
        """正本履歴へ移した元操作のC原文とACKを保持し、次操作枠を作る。I/Oは行わない。"""
        operation_id = json.loads(worker.request)['operationId']
        require(self.controller is None and worker.controller is self
                and self.assignment_acknowledged and self.completion_ready and self.completion_acknowledged
                and json.loads(self.assignment_request)['operationId'] == operation_id
                and json.loads(self.completion_response)['operationId'] == operation_id
                and operation_id not in self.operation_history,
                'WRITER_PENDING', '正本記録とACK済みの元C操作だけを履歴へ保持します')
        names = ('assignment_started','assignment_ready','assignment_acknowledged','assignment_request','assignment_response',
                 'completion_started','completion_ready','completion_acknowledged','completion_request','completion_response')
        self.operation_history[operation_id] = {name:getattr(self, name) for name in names}
        self.operation_history[operation_id]['record'] = deepcopy(self.record)
        for name in names:
            setattr(self, name, None if name.endswith(('_request','_response')) else False)
        for name in ('assignedResponseSha256','assignedAcknowledgedSha256','completionResponseSha256','completionAcknowledgedSha256'):
            self.record.pop(name, None)

    def _record(self):
        write_json(self.diagnostics / f'creation-{self.record_number:02d}.json', self.record)
        self.record_number += 1

    def verify(self):
        """private I/O前にPと所有child、Wの場合は元Cも再照合する。"""
        self.verify_owner()
        runtime_process.require_identity(self.parent, self.adapter.identity(os.getpid()))
        require(process_identity(self.native, self.adapter, os.getpid()) == self.parent_data,
                'PROCESS_MISMATCH', 'Pのhost/boot/startが一致しません')
        require(self.process.poll() is None, 'WRITER_PENDING', '起動中helperが終了しました')
        runtime_process.require_identity(self.identity, self.adapter.identity(self.process.pid))
        require(process_identity(self.native, self.adapter, self.process.pid) == self.data_identity,
                'PROCESS_MISMATCH', 'helperのhost/boot/startが一致しません')
        require(self.process.pid in self.process.members(), 'PROCESS_MISMATCH', 'helperが所有groupにありません')
        if self.controller is not None:
            c = self.controller
            require(c.controller is None and c.ready and c.bundle.resolve() == self.bundle.resolve()
                    and c.parent_data == self.parent_data, 'PROCESS_MISMATCH', '元Cの起動bindingが一致しません')
            c.verify()
            require(c.process.group != self.process.group and c.process.pid != self.process.pid
                    and c.process.pid not in self.process.members() and self.process.pid not in c.process.members(),
                    'PROCESS_MISMATCH', 'C/Wは独立groupで所有する必要があります')

    def complete(self, worker, deadline, verify):
        """正本記録済み元Wの同候補だけを保持C本人へ一度送る。

        verifyはPの元終了証拠・予約とcompletion意図を照合する。旧W PIDへ再attachせず、
        Data応答は本文を変えず返す。正本記録後のackまで同pipeのbufferを保持する。
        """
        require(self.controller is None and worker.controller is self and self.assignment_acknowledged
                and worker.exit_started and worker.exit_response is not None and not self.completion_started,
                'WRITER_PENDING', '元Wの記録済み終了証拠だけを一度確定します')
        self.completion_started = True

        def checked():
            verify()
            self.verify()
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C候補確定の期限を超過しました')

        checked()
        prepared, candidate = json.loads(worker.request), json.loads(worker.candidate_response)
        assigned = json.loads(self.assignment_response)
        frame = {k:prepared[k] for k in ('operationId','requestHash','sessionId','nonce')}
        frame.update(command='complete', openOperationId=json.loads(self.request)['operationId'],
                     expectedStateSha256=candidate['stateSha256'], expectedRevision=candidate['stateRevision'],
                     worldUuid=candidate['worldUuid'], runUuid=candidate['runUuid'],
                     receiptSha256=candidate['receiptSha256'], editRequestSha256=assigned['editRequestSha256'])
        self.completion_request = canonical(frame)
        self.pipe.send(frame, deadline, checked)
        response = self.pipe.receive(deadline, checked)
        self.completion_response = canonical(response)
        require(response.keys() == {'status','operationId','requestHash','sessionId','nonce','stateSha256',
                                    'stateRevision','runUuid','candidateStateSha256','receiptSha256','result'}
                and response['status'] == 'COMPLETED'
                and all(response[k] == frame[k] for k in ('operationId','requestHash','sessionId','nonce','receiptSha256'))
                and response['candidateStateSha256'] == frame['expectedStateSha256']
                and type(response['stateRevision']) is int and response['stateRevision'] == frame['expectedRevision'] + 1
                and response['runUuid'] == json.loads(self.response)['runUuid'],
                'API_PROTOCOL', 'COMPLETEDの元候補/private bindingとIdle遷移が一致しません')
        _hash(response['stateSha256'])
        result = response['result']
        require(type(result) is dict and result.get('requestId') == frame['operationId']
                and result.get('status') in {'SUCCESS','REJECTED'},
                'API_PROTOCOL', 'Dataの元要求に対応した確定応答が必要です')
        checked()
        self.record['completionResponseSha256'] = digest(self.completion_response)
        self._record()
        self.completion_ready = True
        return deepcopy(response)

    def acknowledge_completion(self, response_sha256, deadline, verify):
        """同COMPLETEDの正本記録後だけC pipeを解放する。writerは終了しない。"""
        require(self.completion_ready and not self.completion_acknowledged
                and digest(self.completion_response) == response_sha256,
                'STALE_OPERATION', '記録済みCOMPLETED応答が一致しません')
        verify()
        self.verify()
        require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C確定応答記録後に期限を超過しました')
        self.record['completionAcknowledgedSha256'] = response_sha256
        self._record()
        self.pipe.acknowledged()
        self.completion_acknowledged = True

    def observe_exit(self, deadline, verify):
        """記録済み候補の元W/全child終了を保持handle/groupで一度観測する。

        旧W PIDを再解決せず、P/Cと正本を待機中も照合する。非zero終了・不明・期限切れは
        保全して拒否し、kill/再送/pipe解放しない。Cの候補確定とwriter終了は別途必要。
        """
        require(self.controller is not None and self.candidate_ready and not self.exit_started,
                'WRITER_PENDING', '元Wの記録済み候補だけを一度終了観測します')
        self.exit_started = True

        def checked():
            verify()
            self.controller.verify()
            require(self.process.pid == self.identity['pid'] == self.data_identity['pid']
                    and self.record['identity'] == self.identity
                    and self.record['controlIdentity'] == self.data_identity
                    and self.record['creation']['pid'] == self.process.pid
                    and self.record['creation']['ownerGroup'] == self.process.group
                    and self.process.group != self.controller.process.group,
                    'PROCESS_MISMATCH', '元Wの生成記録と保持groupが一致しません')

        while True:
            checked()
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W/全child終了期限を超過しました。自動killしません')
            code = self.process.poll()
            require(code is None or type(code) is int and code == 0,
                    'WRITER_PENDING', 'Wの非正常終了を保全します')
            members = self.process.members()
            if type(code) is int and code == 0 and members == []:
                break
            time.sleep(0.1)
        checked()
        require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W/全child終了の照合中に期限を超過しました')
        prepared = json.loads(self.request)
        response = dict(status='WORKER_EXITED', operationId=prepared['operationId'],
                        candidateResponseSha256=digest(self.candidate_response),
                        worker=deepcopy(self.data_identity), ownerGroup=self.process.group,
                        exitCode=code, allChildrenExited=True)
        self.exit_response = canonical(response)
        self.record['workerExit'] = response
        self._record()
        return deepcopy(response)

    def admit(self, deadline, verify):
        """記録済み元CのAssignedをWへ一度渡し、候補packetを照合する。

        本文は元prepareを使う。Wは候補送信直後にexitできるため、受信中はP/Cと正本を
        検証し、旧W PIDへ再attachしない。実終了/全child・C completeは別途必要。
        """
        c = self.controller
        require(c is not None and self.startup_acknowledged and c.assignment_acknowledged
                and not self.admission_started, 'WRITER_PENDING', '記録済みの元Assignedだけを一度admitします')
        self.admission_started = True

        def sending():
            verify()
            self.verify()
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W admission送信の期限を超過しました')

        def receiving():
            verify()
            c.verify()
            code = self.process.poll()
            require(code is None or type(code) is int and code == 0,
                    'WRITER_PENDING', 'Wの非正常終了を保全します')
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'W候補応答照合の期限を超過しました')

        sending()
        prepared, assigned = json.loads(self.request), json.loads(c.assignment_response)
        frame = {k:prepared[k] for k in ('operationId','requestHash','sessionId','nonce')}
        frame.update(command='admit', preparedStateSha256=prepared['expectedStateSha256'],
                     expectedStateSha256=assigned['stateSha256'], expectedRevision=assigned['stateRevision'],
                     runUuid=assigned['runUuid'], editRequestSha256=assigned['editRequestSha256'])
        self.admission_request = canonical(frame)
        self.pipe.send(frame, deadline, sending)
        response = self.pipe.receive(deadline, receiving)
        self.candidate_response = canonical(response)
        require(response.keys() == {'status','operationId','requestHash','sessionId','nonce','stateSha256',
                                    'receiptSha256','worldUuid','runUuid','stateRevision','editRequestSha256'}
                and response['status'] == 'CANDIDATE'
                and all(response[k] == frame[k] for k in ('operationId','requestHash','sessionId','nonce','editRequestSha256'))
                and response['worldUuid'] == prepared['worldUuid'] and response['runUuid'] == frame['operationId']
                and type(response['stateRevision']) is int and response['stateRevision'] == assigned['stateRevision'] + 2,
                'API_PROTOCOL', 'CANDIDATEのbindingと遷移が一致しません')
        _hash(response['stateSha256'])
        _hash(response['receiptSha256'])
        receiving()
        self.record['candidateResponseSha256'] = digest(self.candidate_response)
        self._record()
        self.candidate_ready = True
        return deepcopy(response)

    def assign(self, worker, deadline, verify):
        """記録済みPREPAREDの元Wを、同Cの現在の操作枠へ一度だけ登録する。

        verifyはPの正本assignment意図を再照合する。本文/本人/元Idleは保持Wから取得し、
        任意frameを受け付けない。応答は編集成功ではなく、失敗後も再送しない。
        """
        require(self.controller is None and worker.controller is self and self.startup_acknowledged
                and worker.startup_acknowledged and not self.assignment_started,
                'WRITER_PENDING', '記録済みの元C/Wだけを一度登録します')
        self.assignment_started = True

        def checked():
            verify()
            worker.verify()
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C Assigned登録の期限を超過しました')

        checked()
        prepared = json.loads(worker.request)
        frame = dict(command='assign', operationId=prepared['operationId'], requestHash=prepared['requestHash'],
                     sessionId=self.session, nonce=self.nonce, openOperationId=json.loads(self.request)['operationId'],
                     expectedStateSha256=prepared['expectedStateSha256'], expectedRevision=prepared['expectedRevision'],
                     runUuid=prepared['runUuid'], worker=deepcopy(worker.data_identity), request=prepared['request'])
        self.assignment_request = canonical(frame)
        self.pipe.send(frame, deadline, checked)
        response = self.pipe.receive(deadline, checked)
        self.assignment_response = canonical(response)
        require(response.keys() == {'status','operationId','requestHash','sessionId','nonce',
                                    'stateSha256','stateRevision','runUuid','editRequestSha256'}
                and response['status'] == 'ASSIGNED'
                and all(response[k] == frame[k] for k in ('operationId','requestHash','sessionId','nonce','runUuid'))
                and type(response['stateRevision']) is int and response['stateRevision'] == frame['expectedRevision'] + 1,
                'API_PROTOCOL', 'ASSIGNEDのprivate bindingと遷移が一致しません')
        _hash(response['stateSha256'])
        _hash(response['editRequestSha256'])
        checked()
        self.record['assignedResponseSha256'] = digest(self.assignment_response)
        self._record()
        self.assignment_ready = True
        return deepcopy(response)

    def acknowledge_assignment(self, response_sha256, worker, verify, *, deadline):
        """ASSIGNEDの正本記録後だけC pipeを解放する。W admissionや完了は行わない。"""
        require(self.assignment_ready and not self.assignment_acknowledged
                and digest(self.assignment_response) == response_sha256,
                'STALE_OPERATION', '記録済みASSIGNED応答が一致しません')
        verify()
        worker.verify()
        require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C Assigned記録後に期限を超過しました')
        self.record['assignedAcknowledgedSha256'] = response_sha256
        self._record()
        self.pipe.acknowledged()
        self.assignment_acknowledged = True

    def acknowledge_startup(self, response_sha256, *, deadline=None):
        """呼出元が正本へ記録した起動応答hashを照合し、同pipeのbufferを一度だけ解放する。

        失敗時は次frameへ進めず、再送・writer終了扱いもしない。
        """
        require(self.ready and not self.startup_acknowledged and digest(self.response) == response_sha256,
                'STALE_OPERATION', '記録済み起動応答が一致しません')
        self.verify()
        if deadline is not None:
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', '起動応答ACK前に操作期限を超過しました')
        self.record['startupAcknowledgedSha256'] = response_sha256
        self._record()
        if deadline is not None:
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', '起動応答ACKの記録後に操作期限を超過しました')
        self.pipe.acknowledged()
        self.startup_acknowledged = True

    def _launch(self, frame, command, verify):
        require(not self.started, 'WRITER_PENDING', '同じhelperを再生成しません')
        require(command == 'verify-ready' or not self.ready_check_started,
                'WRITER_PENDING', '元Ready読取の試行を保全します')
        self.started = True
        require(not set(frame) & {'command', 'controller', 'owner', 'worker', 'sessionId', 'nonce'},
                'INVALID_INPUT', '起動本人とprivate bindingはPが指定します')
        verify()
        if self.controller is not None:
            require(self.controller.controller is None and self.controller.ready,
                    'WRITER_PENDING', 'WにはOPEN照合済みの元Cが必要です')
            self.controller.verify()
        require(not any(os.environ.get(name) for name in
                        ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'CLASSPATH')),
                'UNREPRODUCIBLE_ENVIRONMENT', '暗黙のJVM入力を使用できません')
        factory = (offline_ready_command if command == 'verify-ready' else
                   offline_controller_command if self.controller is None else offline_worker_command)
        arguments = factory(self.bundle)
        self.parent = deepcopy(self.adapter.identity(os.getpid()))
        require(self.parent is not None, 'PROCESS_MISMATCH', 'P本人を取得できません')
        self.parent_data = process_identity(self.native, self.adapter, os.getpid())
        plain_path(self.diagnostics)
        self.diagnostics.mkdir(parents=True, exist_ok=False)
        make_private(self.diagnostics, directory=True)
        role = 'c' if self.controller is None else 'w'
        self.record = {'role': role, 'commandHash': runtime_process.command_hash(arguments),
                       'creation': None, 'identity': None, 'controlIdentity': None}
        if command == 'verify-ready':
            verify()
        self._record()
        self.pipe = ProxyPipe()
        self.creation_resources = (self.process, self.pipe)
        if command == 'verify-ready':
            resources = self.creation_resources
            fd_names = 'child_input','child_output','input','output'
            creation_fds = tuple(getattr(self.pipe, n) for n in fd_names)
            self.ready_check_creation_fds = creation_fds

            def preparing():
                verify()
                require(self.creation_resources is resources and self.process is resources[0]
                        and self.pipe is resources[1] and self.ready_check_creation_fds == creation_fds
                        and tuple(getattr(self.pipe, n) for n in fd_names) == creation_fds,
                        'PROCESS_MISMATCH', '生成準備後の元pipe FDが変更されました')

            preparing()

        def created():
            self.record['creation'] = {'pid': self.process.pid, 'stage': self.process.stage,
                                       'ownerGroup': self.process.group}
            if command == 'verify-ready':
                verify()
            self._record()
            actual = self.adapter.identity(self.process.pid)
            require(actual is not None and actual['pid'] == self.process.pid
                    and actual['owner'] == self.parent['owner'] and self.process.pid != os.getpid()
                    and Path(actual['cwd']).resolve() == self.bundle.resolve()
                    and Path(actual['executable']).resolve() == Path(arguments[0]).resolve()
                    and actual['commandHash'] == self.record['commandHash'],
                    'PROCESS_MISMATCH', '固定helperの生成本人が一致しません')
            self.identity = deepcopy(actual)
            self.data_identity = process_identity(self.native, self.adapter, self.process.pid)
            require(self.data_identity['startId'] == actual['startId']
                    and all(self.data_identity[k] == self.parent_data[k] for k in ('hostHash', 'bootId')),
                    'PROCESS_MISMATCH', 'helperのnative開始識別/host/bootが一致しません')
            self.record.update(identity=self.identity, controlIdentity=self.data_identity)
            if command == 'verify-ready':
                verify()
            self._record()

        if command == 'observe-game-exit':
            verify()
        with (self.diagnostics / 'stderr.log').open('xb') as log:
            make_private(self.diagnostics / 'stderr.log')
            name = 'offline-' + role + '-' + uuid.uuid4().hex
            if command in ('open', 'prepare'):
                verify()
            if command == 'verify-ready':
                self.process.start(arguments, self.bundle, log, name, created,
                                   input_fd=self.pipe.child_input, output_fd=self.pipe.child_output,
                                   before_create=preparing)
            else:
                self.process.start(arguments, self.bundle, log, name, created,
                                   input_fd=self.pipe.child_input, output_fd=self.pipe.child_output)
        self.pipe.close_child_ends()
        self.record['creation']['stage'] = self.process.stage
        if command == 'verify-ready':
            verify()
        self._record()
        self.verify()
        request = deepcopy(frame)
        request.update(command=command, controller=self.parent_data,
                       owner=self.data_identity if self.controller is None else self.controller.data_identity,
                       sessionId=self.session, nonce=self.nonce)
        if self.controller is not None:
            request['worker'] = self.data_identity
        require(len(encode(request)) + 4 <= MAX_MESSAGE, 'MESSAGE_TOO_LARGE', '起動frameが上限外です')
        self.request = canonical(request)
        return request

    def verify_ready(self, frame, deadline):
        """独立Readyを単発helperへ送り、正常exit/全child不在と正常解放後に観測値を返す。

        呼出元は先にこのobjectを保持し、verifyで現在原文・元CLOSED/終端・予約不在を
        継続照合する。同じdeadlineを生成準備後、I/O、解放、記録/返却まで使う。
        不明時は元object/原文/UNKNOWN進捗を保持し、再生成・再送・部分解放retryはしない。
        観測結果と確定bindingを組で返す。Registry更新やstart許可は行わない。
        """
        require(self.controller is None and not self.started and not self.ready_check_started,
                'WRITER_PENDING', '未使用の読取helperだけを一度起動します')
        self.ready_check_started = True
        frame = deepcopy(frame)
        fields(frame, {'operationId','requestHash','generation','worldRoot','shopDirectory','worldUuid',
                       'runUuid','expectedStateSha256','expectedRevision'})
        identifier(frame['operationId'], 'operationId')
        _hash(frame['requestHash']); _hash(frame['expectedStateSha256'])
        for name in ('generation','worldUuid','runUuid'):
            _canonical_uuid(frame[name])
        require(type(frame['expectedRevision']) is int and 0 <= frame['expectedRevision'] <= 2**63 - 1
                and all(type(frame[k]) is str and Path(frame[k]).is_absolute() for k in ('worldRoot','shopDirectory')),
                'INVALID_INPUT', 'Readyのrevisionと固定絶対pathが必要です')
        input_raw = canonical(frame)
        self.ready_check_input, self.ready_check_deadline = input_raw, deadline
        process = self.process
        parent = deepcopy(self.adapter.identity(os.getpid()))
        parent_data = process_identity(self.native, self.adapter, os.getpid())
        resources = artifact = None

        def checked():
            nonlocal resources
            if resources is None and self.creation_resources is not None:
                resources = self.creation_resources
            require(time.monotonic() < deadline, 'START_TIMEOUT', 'Ready読取の単一期限を超過しました')
            self.verify_owner()
            require(self.process is process and self.controller is None,
                    'PROCESS_MISMATCH', '元読取object/process bindingが変更されました')
            if resources is not None:
                require(self.creation_resources is resources and resources[0] is process and resources[1] is self.pipe,
                        'PROCESS_MISMATCH', '元読取handle/pipeの保持先が変更されました')
            require(self.ready_check_input == input_raw and self.ready_check_deadline == deadline,
                    'PROOF_MISMATCH', '元Ready入力と入口期限が変更されました')
            runtime_process.require_identity(parent, self.adapter.identity(os.getpid()))
            require(process_identity(self.native, self.adapter, os.getpid()) == parent_data,
                    'PROCESS_MISMATCH', '元Pの本人が変更されました')
            if artifact is not None:
                require(self.ready_check_artifact is artifact_record and self.ready_check_artifact == artifact
                        and inventory() == artifact['identities'],
                        'PROOF_MISMATCH', '固定artifactの原文又は実体が変更されました')
            self.verify_owner()
            require(time.monotonic() < deadline, 'START_TIMEOUT', 'Ready読取照合後に期限を超過しました')

        checked()
        manifest = verify_bundle(self.bundle)
        expected = {entry['path']:(entry['size'], entry['sha256']) for entry in manifest['files']}
        for name in ('runtime.json','COMPLETE'):
            raw = regular_bytes(self.bundle / name)
            require((json.loads(raw) == manifest if name == 'runtime.json' else raw.decode() == manifest['artifactId']),
                    'PROOF_MISMATCH', '元bundle manifestが変更されました')
            expected[name] = len(raw), digest(raw)

        def inventory():
            nodes = [self.bundle, *self.bundle.rglob('*')]
            require({p.relative_to(self.bundle).as_posix() for p in nodes if p.is_file()} == expected.keys(),
                    'PROOF_MISMATCH', '固定bundleのfile集合が変更されました')
            identities = {}
            for path in nodes:
                plain_path(path)
                before = path.lstat()
                name = path.relative_to(self.bundle).as_posix()
                if name in expected:
                    size, sha = expected[name]
                    raw = regular_bytes(path, maximum=size)
                    require(len(raw) == size and digest(raw) == sha,
                            'PROOF_MISMATCH', '固定bundleの実読取原文が変更されました')
                after = path.lstat()
                stamp = after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns
                require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) == stamp,
                        'PROOF_MISMATCH', '固定artifactの読取実体が変更されました')
                identities[name] = stamp
            return identities

        artifact = dict(artifactId=manifest['artifactId'], files=expected, identities=inventory())
        self.ready_check_artifact = deepcopy(artifact)
        artifact_record = self.ready_check_artifact
        checked()
        request = deepcopy(self._launch(frame, 'verify-ready', checked))
        identity, data_identity = deepcopy((self.identity, self.data_identity))
        group = process.group
        handle_names = 'handle','job','thread','process'
        handles = tuple(getattr(process,n,None) for n in handle_names)
        pipe = self.pipe
        fds = pipe.input,pipe.output
        self.ready_check_handles, self.ready_check_fds = handles,fds
        progress = dict(handle='NOT_STARTED', pipe='NOT_STARTED')
        release_state = 'NOT_STARTED','NOT_STARTED'
        self.ready_check_release = progress
        request_raw = self.request
        response_raw = None

        def observing():
            checked()
            require(self.identity == identity and self.data_identity == data_identity
                    and self.parent == parent and self.parent_data == parent_data
                    and self.session == request['sessionId'] and self.nonce == request['nonce']
                    and self.record['identity'] == identity and self.record['controlIdentity'] == data_identity
                    and process.pid == identity['pid'] == data_identity['pid'] and process.group == group
                    and self.record['creation']['pid'] == process.pid and self.record['creation']['ownerGroup'] == group
                    and self.request == request_raw and self.ready_check_release is progress
                    and progress == dict(zip(('handle','pipe'), release_state))
                    and self.ready_check_handles == handles and self.ready_check_fds == fds
                    and tuple(getattr(process,n,None) for n in handle_names) == handles
                    and pipe.child_input is None and pipe.child_output is None
                    and (all(getattr(pipe,n) == fd for n,fd in zip(('input','output'),fds))
                         if progress['pipe'] == 'NOT_STARTED' else
                         all(getattr(pipe,n) in (None,fd) for n,fd in zip(('input','output'),fds)))
                    and (response_raw is None or self.ready_check_response == response_raw),
                    'PROCESS_MISMATCH', '元読取の本人・原文・解放進捗が変更されました')
            if progress['handle'] == 'NOT_STARTED':
                self._observe_process(process, identity, data_identity)
            checked()

        self.pipe.send(request, deadline, observing)
        response = self.pipe.receive(deadline, observing)
        response_raw = canonical(response)
        self.ready_check_response = response_raw
        require(response.keys() == {'status','operationId','requestHash','sessionId','nonce','worldUuid','generation',
                                    'runUuid','stateSha256','stateRevision'}
                and response['status'] == 'READY_VERIFIED'
                and all(response[k] == request[k] for k in ('operationId','requestHash','sessionId','nonce',
                                                           'worldUuid','generation','runUuid'))
                and response['stateSha256'] == request['expectedStateSha256']
                and type(response['stateRevision']) is int and response['stateRevision'] == request['expectedRevision'],
                'API_PROTOCOL', '独立Readyと同じ公式検証応答が必要です')
        while True:
            observing()
            code, members = process.poll(), process.members()
            observing()
            if type(code) is int and code == 0 and members == []:
                break
            time.sleep(0.1)
        observing()
        progress['handle'] = 'UNKNOWN'
        release_state = 'UNKNOWN','NOT_STARTED'
        process.close(verify=observing)
        progress['handle'] = 'RELEASED'
        release_state = 'RELEASED','NOT_STARTED'
        observing()
        progress['pipe'] = 'UNKNOWN'
        release_state = 'RELEASED','UNKNOWN'
        self.pipe.close_after_exit(verify=observing)
        require(self.pipe.input is None and self.pipe.output is None and self.pipe.child_input is None
                and self.pipe.child_output is None, 'WRITER_PENDING', '読取pipeの解放が完了していません')
        progress['pipe'] = 'RELEASED'
        release_state = 'RELEASED','RELEASED'
        observing()
        result = dict(status='READY_READ_RELEASED', operationId=request['operationId'],
                      requestSha256=digest(request_raw), responseSha256=digest(response_raw),
                      artifactId=manifest['artifactId'], reader=deepcopy(data_identity), readerGroup=group,
                      exitCode=0, allChildrenExited=True, handlesReleased=True,
                      ready={k:response[k] for k in ('worldUuid','generation','runUuid','stateSha256','stateRevision')})
        observing()
        self.record['readyRead'] = deepcopy(result)
        observing()
        self._record()
        self.ready_check_result = canonical(result)
        returned = deepcopy(result)

        def _ready_binding(expected=None):
            process, pipe = self.process, self.pipe
            objects = (process, pipe, self.creation_resources, self.ready_check_artifact,
                       self.ready_check_release, self.ready_check_handles, getattr(process, 'process', None),
                       self.native, self.adapter, self.verify_owner, self.controller)
            if expected is not None:
                require(all(current is original for current, original in zip(objects, expected[0])),
                        'PROOF_MISMATCH', '完成した元readerの保持objectが変更されました')
            names = ('bundle','diagnostics','parent','parent_data','identity','data_identity','session','nonce',
                     'started','ready','startup_acknowledged','ready_check_started','ready_check_input',
                     'ready_check_deadline','request','response','ready_check_response','ready_check_result',
                     'ready_check_fds','ready_check_creation_fds','ready_check_artifact','ready_check_release',
                     'record','record_number')
            values = deepcopy((dict((name, getattr(self, name)) for name in names),
                               (process.pid, process.group, *(getattr(process, name, None)
                                for name in ('handle','job','thread'))),
                               (pipe.input, pipe.output, pipe.child_input, pipe.child_output)))
            if expected is not None:
                require(values == expected[1], 'PROOF_MISMATCH', '完成した元readerの保持値が変更されました')
            return objects, values

        binding = _ready_binding()
        observing()
        require(progress == {'handle':'RELEASED','pipe':'RELEASED'}
                and self.record.get('readyRead') == result and self.ready_check_result == canonical(result),
                'PROOF_MISMATCH', '正常読取の最終記録・原文・進捗が一致しません')
        checked()
        _ready_binding(binding)
        require(time.monotonic() < deadline, 'START_TIMEOUT', 'Ready完成bindingの返却前に期限を超過しました')
        completed_binding = binding[0], lambda: _ready_binding(binding)
        self.ready_check_binding = completed_binding
        return returned, completed_binding

    def start(self, frame, deadline):
        """C OPEN又はW PREPAREDを一度照合する。型付き要求本文は変更しない。"""
        require(not self.game_exit_started, 'WRITER_PENDING', '元game退出観測を保全します')
        def launching():
            self.verify_owner()
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'helper生成前に操作期限を超過しました')

        request = self._launch(frame, 'open' if self.controller is None else 'prepare', launching)
        self.pipe.send(request, deadline, self.verify)
        response = self.pipe.receive(deadline, self.verify)
        self.response = canonical(response)
        require(response.keys() == {'status', 'operationId', 'requestHash', 'sessionId', 'nonce',
                                    'stateSha256', 'stateRevision', 'runUuid'},
                'API_PROTOCOL', '起動応答の項目が一致しません')
        require(response['status'] == ('OPEN' if self.controller is None else 'PREPARED')
                and all(response[k] == request[k] for k in ('operationId', 'requestHash', 'sessionId', 'nonce')),
                'API_PROTOCOL', '起動応答のprivate bindingが一致しません')
        _hash(response['stateSha256'])
        _canonical_uuid(response['runUuid'])
        require(type(response['stateRevision']) is int and response['stateRevision'] >= 0,
                'API_PROTOCOL', '起動応答のrevisionが不正です')
        if self.controller is not None:
            require(all(response[key] == request[field] for key, field in (
                ('stateSha256', 'expectedStateSha256'), ('stateRevision', 'expectedRevision'), ('runUuid', 'runUuid'))),
                'API_PROTOCOL', 'PREPAREDが元Idleと一致しません')
        self.verify()
        require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'helper起動応答の照合後に操作期限を超過しました')
        self.record['startupResponseSha256'] = digest(self.response)
        self._record()
        self.ready = True
        return deepcopy(response)

    def _observe_process(self, process, expected_identity, expected_data):
        def normal_exit(process):
            code = process.poll()
            require(type(code) is int and code == 0,
                    'WRITER_PENDING', '本人不在の元handleに正常終了証拠がありません')
            return code

        code = process.poll()
        require(code is None or type(code) is int and code == 0,
                'WRITER_PENDING', '元processの非正常終了を保全します')
        if code is not None:
            return code
        actual = self.adapter.identity(process.pid)
        if actual is None:
            return normal_exit(process)
        runtime_process.require_identity(expected_identity, actual)
        try:
            observed_data = process_identity(self.native, self.adapter, process.pid)
        except RegistryError as error:
            if error.code != 'PROCESS_MISMATCH':
                raise
            return normal_exit(process)
        require(observed_data == expected_data,
                'PROCESS_MISMATCH', '生存照会の元process本人が一致しません')
        members = process.members()
        require(type(members) is list, 'PROCESS_MISMATCH', '所有groupの観測が不明です')
        if process.pid not in members:
            return normal_exit(process)
        return None

    def close_owner(self, latest, *, operation_id, request_hash, deadline, verify):
        """照合済み最新Idleの元Cへcloseを一度送り、対応するData応答を保持する。"""
        require(self.controller is None and self.ready and not self.close_started and not self.game_exit_started,
                'WRITER_PENDING', '保持した元Cのclose入口が必要です')
        self.close_started = True
        verify()
        self.verify()
        held = self.completion_response if self.completion_ready else self.response
        require(held is not None and canonical(latest) == held,
                'STALE_OPERATION', '元Cの最新Idle応答が一致しません')
        identity, data = deepcopy(self.identity), deepcopy(self.data_identity)
        creation = deepcopy(self.record['creation'])
        parent, parent_data = deepcopy(self.parent), deepcopy(self.parent_data)
        group = self.process.group
        self.close_binding = dict(process=self.process, pipe=self.pipe, identity=identity, data=data,
                                  creation=creation, parent=parent, parent_data=parent_data,
                                  group=group, command_hash=self.record['commandHash'], deadline=deadline)
        frame = {'command':'close', 'operationId':operation_id, 'requestHash':request_hash,
                 'sessionId':self.session, 'nonce':self.nonce,
                 'openOperationId':json.loads(self.request)['operationId'],
                 'expectedStateSha256':latest['stateSha256'], 'expectedRevision':latest['stateRevision']}

        def checked():
            verify()
            self.verify_owner()
            runtime_process.require_identity(parent, self.adapter.identity(os.getpid()))
            actual_parent = process_identity(self.native, self.adapter, os.getpid())
            self._observe_process(self.process, identity, data)
            require(self.parent == parent and actual_parent == parent_data and self.parent_data == parent_data
                    and self.identity == identity and self.data_identity == data
                    and self.record['identity'] == identity and self.record['controlIdentity'] == data
                    and self.record['creation'] == creation
                    and self.process.pid == identity['pid'] == data['pid'] == creation['pid']
                    and self.process.group == group == creation['ownerGroup'],
                    'PROCESS_MISMATCH', 'close中の元P/C生成bindingが一致しません')
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C closeの本人照合後に期限を超過しました')

        checked()
        self.close_request = canonical(frame)
        self.record['ownerCloseRequestSha256'] = digest(self.close_request)
        self._record()
        checked()
        self.pipe.send(frame, deadline, checked)
        response = self.pipe.receive(deadline, checked)
        self.close_response = canonical(response)
        require(response.keys() == {'status','operationId','requestHash','sessionId','nonce',
                                    'stateSha256','stateRevision','runUuid'}
                and response['status'] == 'OWNER_CLOSED'
                and all(response[k] == frame[k] for k in ('operationId','requestHash','sessionId','nonce'))
                and response['runUuid'] == latest['runUuid']
                and type(response['stateRevision']) is int and response['stateRevision'] == latest['stateRevision'] + 1,
                'API_PROTOCOL', '元C closeの対応Ready応答が一致しません')
        _hash(response['stateSha256'])
        checked()
        self.record['ownerCloseResponseSha256'] = digest(self.close_response)
        self._record()
        checked()
        self.close_binding.update(request=self.close_request, response=self.close_response)
        self.close_ready = True
        return deepcopy(response)

    def _verify_close_binding(self, deadline):
        binding = self.close_binding
        request, response = binding['request'], binding['response']
        runtime_process.require_identity(binding['parent'], self.adapter.identity(os.getpid()))
        require(process_identity(self.native, self.adapter, os.getpid()) == binding['parent_data']
                and self.parent == binding['parent'] and self.parent_data == binding['parent_data']
                and self.process is binding['process'] and self.pipe is binding['pipe']
                and self.identity == binding['identity'] and self.data_identity == binding['data']
                and self.record['identity'] == binding['identity']
                and self.record['controlIdentity'] == binding['data']
                and self.record['creation'] == binding['creation']
                and self.record['commandHash'] == binding['command_hash']
                and self.process.pid == binding['identity']['pid'] == binding['data']['pid'] == binding['creation']['pid']
                and self.process.group == binding['group'] == binding['creation']['ownerGroup']
                and self.close_request == request and self.close_response == response
                and self.record.get('ownerCloseRequestSha256') == digest(request)
                and self.record.get('ownerCloseResponseSha256') == digest(response),
                'PROCESS_MISMATCH', 'C終了観測の元P/C/pipe/close生成bindingが一致しません')
        require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'C/全child終了観測の期限を超過しました')

    def observe_owner_exit(self, *, deadline, verify):
        """close時の元C/全childの正常終了を一度観測し、handle/pipeと原文を保持する。

        verifyは同closeの正本応答と予約を継続照合する。終了済みPIDを再解決せず、
        不明・非zero・期限超過は再観測/kill/資源解放せず呼出元へ伝える。
        """
        require(self.controller is None and self.close_ready and self.close_binding is not None
                and not self.owner_exit_started,
                'WRITER_PENDING', '元Cの記録済みcloseだけを一度終了観測します')
        self.owner_exit_started = True
        binding = self.close_binding
        deadline = min(deadline, binding['deadline'])
        request, response = self.close_request, self.close_response

        def checked():
            verify()
            self.verify_owner()
            self._verify_close_binding(deadline)

        while True:
            checked()
            code = self._observe_process(self.process, binding['identity'], binding['data'])
            checked()
            members = self.process.members()
            checked()
            require(type(members) is list, 'PROCESS_MISMATCH', 'C所有groupの観測が不明です')
            if type(code) is int and code == 0 and members == []:
                break
            time.sleep(0.1)
        result = dict(status='OWNER_EXIT_OBSERVED', operationId=json.loads(request)['operationId'],
                      closeRequestSha256=digest(request), closeResponseSha256=digest(response),
                      owner=deepcopy(binding['data']), ownerGroup=binding['group'],
                      exitCode=0, allChildrenExited=True)
        self.owner_exit_response = canonical(result)
        checked()
        self.record['ownerExit'] = deepcopy(result)
        self._record()
        checked()
        return deepcopy(result)

    def observe_game_exit(self, frame, *, game_process, game_identity, game_group, deadline):
        """元Mと単発観測Cの正常exit/全child不在から同候補のReadyを局所観測する。

        呼出元はこのobjectと元Mの生成handle/identity/groupを保持し、verify callbackで
        正本予約・遮断/drain・全writer終了・認証済Requested/COMPLETION・候補原本を
        継続照合する。返却Readyは観測値であり、次openの期待値を自動承認しない。
        Registryへの記録/次openは行わず、失敗時は資源と原文を保持して再入口を拒否する。
        """
        require(self.controller is None and not self.started and not self.game_exit_started,
                'WRITER_PENDING', '未使用Cで元game退出を一度だけ観測します')
        self.game_exit_started = True
        frame, game_identity = deepcopy(frame), deepcopy(game_identity)
        game = frame['gameOwner']
        _identity(game)
        require(game_process.pid == game_identity['pid'] == game['pid']
                and game_identity['startId'] == game['startId'] and game_group is not None,
                'PROCESS_MISMATCH', '元Mの生成本人が一致しません')
        parent = deepcopy(self.adapter.identity(os.getpid()))
        parent_data = process_identity(self.native, self.adapter, os.getpid())
        require(game != parent_data and all(game[k] == parent_data[k] for k in ('hostHash','bootId')),
                'PROCESS_MISMATCH', '元MとPのhost/boot/本人が一致しません')
        self.game_exit_frame = frame
        self.game_exit_process = game_process
        self.game_exit_identity = game_identity
        self.game_exit_group = game_group

        def checked():
            self.verify_owner()
            runtime_process.require_identity(parent, self.adapter.identity(os.getpid()))
            require(process_identity(self.native, self.adapter, os.getpid()) == parent_data,
                    'PROCESS_MISMATCH', 'Pのhost/boot/startが一致しません')
            require(game_process.pid == game['pid'] and game_process.group == game_group,
                    'PROCESS_MISMATCH', '元Mの保持handle/groupが一致しません')
            require(time.monotonic() < deadline, 'STOP_TIMEOUT', 'game退出観測の期限を超過しました')

        def exited(process, expected_identity, expected_data):
            while True:
                checked()
                code = self._observe_process(process, expected_identity, expected_data)
                members = process.members()
                checked()
                if type(code) is int and code == 0 and members == []:
                    return
                time.sleep(0.1)

        exited(game_process, game_identity, game)
        request = deepcopy(self._launch(frame, 'observe-game-exit', checked))
        identity = deepcopy(self.identity)

        def observing():
            checked()
            self._observe_process(self.process, identity, request['owner'])
            checked()
            require(self.process.pid == identity['pid'] == self.data_identity['pid']
                    and self.identity == identity and self.record['identity'] == identity
                    and self.record['controlIdentity'] == self.data_identity
                    and self.data_identity == request['owner']
                    and self.record['creation']['pid'] == self.process.pid
                    and self.record['creation']['ownerGroup'] == self.process.group
                    and self.process.group != game_group,
                    'PROCESS_MISMATCH', '観測Cの保持本人/groupが一致しません')

        observing()
        self.pipe.send(request, deadline, observing)
        response = self.pipe.receive(deadline, observing)
        self.game_exit_response = canonical(response)
        require(response.keys() == {'status','operationId','requestHash','sessionId','nonce','stateSha256',
                                    'stateRevision','worldUuid','runUuid','candidateStateSha256','saveProofSha256'}
                and response['status'] == 'GAME_EXITED'
                and all(response[k] == request[k] for k in ('operationId','requestHash','sessionId','nonce','saveProofSha256'))
                and response['candidateStateSha256'] == request['expectedStateSha256']
                and all(response[k] == request[k] for k in ('worldUuid','runUuid'))
                and type(response['stateRevision']) is int
                and response['stateRevision'] == request['expectedRevision'] + 1,
                'API_PROTOCOL', '同game候補に対応するReady応答が必要です')
        _hash(response['stateSha256'])
        ready = {k:response[k] for k in ('worldUuid','runUuid','stateSha256','stateRevision')}
        observing()
        exited(self.process, identity, request['owner'])
        observing()
        result = dict(status='GAME_EXIT_OBSERVED', operationId=request['operationId'],
                      requestSha256=digest(self.request), responseSha256=digest(self.game_exit_response),
                      gameOwner=deepcopy(request['gameOwner']), gameGroup=game_group, observer=deepcopy(self.data_identity),
                      observerGroup=self.process.group, exitCode=0, allChildrenExited=True, ready=ready)
        observing()
        self.record['gameExit'] = result
        self._record()
        observing()
        self.game_exit_result = canonical(result)
        return deepcopy(result)

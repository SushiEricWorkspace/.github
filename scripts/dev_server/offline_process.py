"""P専用の固定C/W生成・限定private要求とW終了観測。Registry更新は呼出元が担う。"""

from copy import deepcopy
import json
import os
import time
from pathlib import Path
import uuid

from . import runtime_process
from .build_inputs import canonical, digest
from .database_proxy_pipe import ProxyPipe
from .local_ipc import MAX_MESSAGE, encode, make_private, plain_path
from .mod_control_runtime import process_identity
from .mod_control_wire import _hash, _canonical_uuid, _identity
from .offline_controller_bundle import offline_controller_command, offline_worker_command
from .registry import RegistryError, require
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
        factory = offline_controller_command if self.controller is None else offline_worker_command
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
        self._record()
        self.pipe = ProxyPipe()

        def created():
            self.record['creation'] = {'pid': self.process.pid, 'stage': self.process.stage,
                                       'ownerGroup': self.process.group}
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
            self._record()

        if command == 'observe-game-exit':
            verify()
        with (self.diagnostics / 'stderr.log').open('xb') as log:
            make_private(self.diagnostics / 'stderr.log')
            name = 'offline-' + role + '-' + uuid.uuid4().hex
            if command in ('open', 'prepare'):
                verify()
            self.process.start(arguments, self.bundle, log, name, created,
                               input_fd=self.pipe.child_input, output_fd=self.pipe.child_output)
        self.pipe.close_child_ends()
        self.record['creation']['stage'] = self.process.stage
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
        self.close_ready = True
        return deepcopy(response)

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

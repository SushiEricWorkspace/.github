"""P専用の固定C/W生成と最初のprivate要求。終了・Registry更新は扱わない。"""

from copy import deepcopy
import os
from pathlib import Path
import uuid

from . import runtime_process
from .build_inputs import canonical, digest
from .database_proxy_pipe import ProxyPipe
from .local_ipc import MAX_MESSAGE, encode, make_private, plain_path
from .mod_control_runtime import process_identity
from .mod_control_wire import _hash, _canonical_uuid
from .offline_controller_bundle import offline_controller_command, offline_worker_command
from .registry import require
from .runtime_control import write_json


class OfflineProcess:
    """固定C又は保持Cに属するWを一度だけ生成する。

    Pは呼出前にobjectを保持し、verifyでlease/epoch/generation・遮断/drain・起動排他を
    継続照合する。nativeは既存transportの本人観測を使う。start後もhandleとpipeを保持し、
    例外時は再起動・再送しない。実終了/C complete/Registry終端は別途必要。
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
        self.session = uuid.uuid4().hex if controller is None else controller.session
        self.nonce = uuid.uuid4().hex if controller is None else controller.nonce

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

    def start(self, frame, deadline):
        """本人欄・command・session/nonceをPが付け、C OPEN又はW PREPAREDを一度照合する。

        frameは既存Java契約の残りの項目。型付きrequest本文は変更しない。
        戻り値は照合済み起動応答のコピーであり、編集成功・writer終了の証拠ではない。
        """
        require(not self.started, 'WRITER_PENDING', '同じhelperを再生成しません')
        self.started = True
        require(not set(frame) & {'command', 'controller', 'owner', 'worker', 'sessionId', 'nonce'},
                'INVALID_INPUT', '起動本人とprivate bindingはPが指定します')
        self.verify_owner()
        if self.controller is not None:
            require(self.controller.controller is None and self.controller.ready,
                    'WRITER_PENDING', 'WにはOPEN照合済みの元Cが必要です')
            self.controller.verify()
        require(not any(os.environ.get(name) for name in
                        ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'CLASSPATH')),
                'UNREPRODUCIBLE_ENVIRONMENT', '暗黙のJVM入力を使用できません')
        command = offline_controller_command if self.controller is None else offline_worker_command
        arguments = command(self.bundle)
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

        with (self.diagnostics / 'stderr.log').open('xb') as log:
            make_private(self.diagnostics / 'stderr.log')
            self.process.start(arguments, self.bundle, log, 'offline-' + role + '-' + uuid.uuid4().hex, created,
                               input_fd=self.pipe.child_input, output_fd=self.pipe.child_output)
        self.pipe.close_child_ends()
        self.record['creation']['stage'] = self.process.stage
        self._record()
        self.verify()
        request = deepcopy(frame)
        request.update(command='open' if self.controller is None else 'prepare', controller=self.parent_data,
                       owner=self.data_identity if self.controller is None else self.controller.data_identity,
                       sessionId=self.session, nonce=self.nonce)
        if self.controller is not None:
            request['worker'] = self.data_identity
        require(len(encode(request)) + 4 <= MAX_MESSAGE, 'MESSAGE_TOO_LARGE', '起動frameが上限外です')
        self.request = canonical(request)
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
        self.record['startupResponseSha256'] = digest(self.response)
        self._record()
        self.ready = True
        return deepcopy(response)

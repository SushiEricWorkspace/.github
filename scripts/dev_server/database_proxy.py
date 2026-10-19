"""固定DB proxy childと私設stdioを所有する。writer終端の正本はRegistryだけとする。"""
import json
import os
from pathlib import Path
import time

from .build_inputs import canonical, digest
from .database_proxy_bundle import proxy_command
from .database_proxy_pipe import ProxyPipe
from .local_ipc import MAX_MESSAGE, encode, make_private, plain_path
from .registry import require
from .runtime_control import write_json
from . import runtime_process


class DatabaseProxy:
    """1要求/1応答だけを保持し、正本記録前のbufferを破棄・再送しない。

    呼出側は生成前にこのobjectを保持し、verifyでwriter/runの現在本人を照合する。
    startの途中失敗後も別childへ置換しない。request本文・応答本文は永続記録へ書かない。
    """
    def __init__(self, bundle, diagnostics, binding, *, verify, process_adapter=runtime_process):
        self.bundle = Path(bundle)
        self.diagnostics = Path(diagnostics)
        self.binding_hash = digest(canonical(binding))
        self.verify_owner = verify
        self.adapter = process_adapter
        self.process = process_adapter.OwnedProcess()
        self.pipe = None
        self.identity = None
        self.record = None
        self.record_number = 0
        self.started = False
        self.exited = False
        self.request_bytes = None
        self.response_bytes = None
        self.sent_complete = False
        self.bound = False
        self.pending_operation = None
        self.closing_operation = None

    def _record(self):
        write_json(self.diagnostics / f'creation-{self.record_number:02d}.json', self.record)
        self.record_number += 1

    def start(self):
        require(not self.started, 'WRITER_PENDING', '同じproxy childを再生成しません')
        self.started = True
        self.verify_owner()
        require(not any(os.environ.get(name) for name in
                        ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'CLASSPATH')),
                'UNREPRODUCIBLE_ENVIRONMENT', '暗黙のJVM入力を使用できません')
        arguments = proxy_command(self.bundle)
        plain_path(self.diagnostics)
        self.diagnostics.mkdir(parents=True, exist_ok=False)
        make_private(self.diagnostics, directory=True)
        self.record = {'bindingSha256': self.binding_hash,
                       'commandHash': runtime_process.command_hash(arguments), 'creation': None, 'identity': None}
        self._record()
        self.pipe = ProxyPipe()

        def created():
            self.record['creation'] = {'pid': self.process.pid, 'stage': self.process.stage,
                                       'ownerGroup': self.process.group}
            self._record()
            identity = self.adapter.identity(self.process.pid)
            owner = self.adapter.identity(os.getpid())
            require(identity is not None and owner is not None and identity['owner'] == owner['owner']
                    and Path(identity['cwd']).resolve() == self.bundle.resolve()
                    and Path(identity['executable']).resolve() == Path(arguments[0]).resolve()
                    and identity['commandHash'] == self.record['commandHash'],
                    'PROCESS_MISMATCH', '生成した固定proxyの本人が一致しません')
            self.identity = identity
            self.record['identity'] = identity
            self._record()

        with (self.diagnostics / 'stderr.log').open('xb') as log:
            make_private(self.diagnostics / 'stderr.log')
            self.process.start(arguments, self.bundle, log, 'db-' + self.binding_hash[:40], created,
                               input_fd=self.pipe.child_input, output_fd=self.pipe.child_output)
        self.pipe.close_child_ends()
        self.record['creation']['stage'] = self.process.stage
        self._record()
        self.verify()

    def verify(self, *, read_deadline=None):
        self.verify_owner()
        require(self.identity is not None, 'PROCESS_UNVERIFIED', 'proxy生成本人が未確認です')
        while not self.exited:
            code = self.process.poll()
            if code is not None:
                require(code == 0, 'WRITER_PENDING', 'proxyが正常終了していません')
                return
            try:
                actual = self.adapter.identity(self.process.pid)
            except OSError:
                code = self.process.poll()
                if code == 0:
                    return
                if (code is not None or read_deadline is None or not self.sent_complete
                        or time.monotonic() >= read_deadline):
                    raise
                time.sleep(min(0.01, max(0, read_deadline - time.monotonic())))
                self.verify_owner()
            else:
                if actual is None and self.process.poll() == 0:
                    return
                runtime_process.require_identity(self.identity, actual)
                return


    def begin(self, frame, deadline):
        """新frameは一度だけ送る。partial/timeout後は残bytesも追送しない。"""
        require(self.request_bytes is None and self.response_bytes is None and not self.exited,
                'WRITER_PENDING', '保持中の私設要求を上書きできません')
        if self.pending_operation is not None:
            require(frame.get('command') in {'resolve', 'close'}, 'WRITER_PENDING', '未完要求を別操作で置換できません')
            if frame['command'] == 'resolve':
                require((frame.get('operationId'), frame.get('requestHash')) == self.pending_operation,
                        'OPERATION_CONFLICT', '遅延応答は元要求だけを照合します')
        if self.closing_operation is not None:
            require(frame.get('command') in {'resolve', 'close'}, 'WRITER_PENDING', 'close受付後の新要求は禁止です')
        if frame.get('command') == 'close':
            pair = (frame.get('operationId'), frame.get('requestHash'))
            require(self.closing_operation is None or pair == self.closing_operation,
                    'OPERATION_CONFLICT', '別close要求へ置換できません')
            self.closing_operation = pair
        body = encode(frame)
        require(len(body) + 4 <= MAX_MESSAGE, 'MESSAGE_TOO_LARGE', '私設frame上限です')
        self.request_bytes = body
        self.sent_complete = False
        self.verify()
        self.pipe.send(json.loads(body), deadline, self.verify)
        self.sent_complete = True
        return self.receive(deadline)

    def receive(self, deadline):
        """送出済みframeの応答だけを読む。保持応答も本人照合後に返す。"""
        require(self.request_bytes is not None, 'WRITER_PENDING', '照合する私設要求がありません')
        self.verify(read_deadline=deadline)
        if self.response_bytes is None:
            response = self.pipe.receive(deadline, lambda: self.verify(read_deadline=deadline))
            request = json.loads(self.request_bytes)
            require(response.get('operationId') == request['operationId']
                    and response.get('requestHash') == request['requestHash'],
                    'API_PROTOCOL', '私設応答の要求識別が一致しません')
            require(response.get('status') in {'OPEN', 'RESULT', 'REJECTED', 'PENDING', 'UNKNOWN',
                                               'CLOSED', 'UNBOUND_CLOSED'},
                    'API_PROTOCOL', '未知の私設応答です')
            body = canonical(response)
            require(len(body) + 4 <= MAX_MESSAGE, 'MESSAGE_TOO_LARGE', '私設応答上限です')
            self.response_bytes = body
        return json.loads(self.response_bytes)

    def acknowledge(self, response_hash):
        """呼出側が正本へ同応答hashを確定してから、次の私設frameを許可する。"""
        self.verify()
        require(self.response_bytes is not None and digest(self.response_bytes) == response_hash,
                'API_PROTOCOL', '記録した応答hashが一致しません')
        response = json.loads(self.response_bytes)
        require(response['status'] != 'UNKNOWN', 'WRITER_PENDING', 'UNKNOWN応答を解放しません')
        request = json.loads(self.request_bytes)
        if response['status'] == 'PENDING':
            if self.pending_operation is None or request['command'] != 'close':
                self.pending_operation = (request['operationId'], request['requestHash'])
        elif response['status'] in {'RESULT', 'REJECTED'}:
            self.pending_operation = None
        if response['status'] == 'OPEN':
            self.bound = True
        self.pipe.acknowledged()
        self.request_bytes = self.response_bytes = None
        self.sent_complete = False

    def finish_exit(self, deadline):
        """終端frameと実exit0/全child不在を照合する。exitだけで結果不明を解消しない。"""
        self.verify(read_deadline=deadline)
        require(self.response_bytes is not None, 'WRITER_PENDING', '終了frameがありません')
        response = json.loads(self.response_bytes)
        expected = 'CLOSED' if self.bound else 'UNBOUND_CLOSED'
        require(response.get('status') == expected and response.get('historyEnded') is True
                and type(response.get('pending')) is int and response['pending'] == 0
                and (response.get('peerClosed') is True or not self.bound),
                'WRITER_PENDING', 'DB要求/履歴/通信の終了が未確認です')
        while not self.exited:
            self.verify(read_deadline=deadline)
            code = self.process.poll()
            if code == 0 and self.process.members() == []:
                self.record['observedExit'] = {'exit': 0, 'children': []}
                self._record()
                self.pipe.close_after_exit()
                self.process.close()
                self.exited = True
                break
            require(code is None or code == 0, 'WRITER_PENDING', 'proxyが異常終了しました')
            if time.monotonic() >= deadline:
                raise TimeoutError('proxy全child終了の待機期限です')
            time.sleep(0.01)
        return {'proxyExited': True, 'proxyChildrenExited': True}
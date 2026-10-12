"""SSHの標準入出力にだけ使用する、上限付きJSON frameとprivate証跡。"""
import hashlib
from pathlib import Path
import uuid

from .local_ipc import MAX_MESSAGE, decode, encode, require_private
from .registry import identifier, require
from . import storage

CLI_COMMANDS = {"status", "acquire", "renew", "lease-check", "release", "prepare", "restore", "reset", "start", "stop", "snapshot", "recover"}
COMMANDS = CLI_COMMANDS | {"open", "io", "monitor-open", "monitor-read", "monitor-close", "close"}


def read_frame(stream):
    def read(size):
        result = bytearray()
        while len(result) < size:
            part = stream.read(size - len(result))
            if not part:
                raise EOFError()
            result.extend(part)
        return bytes(result)
    length = int.from_bytes(read(4), "big")
    require(0 < length <= MAX_MESSAGE, "MESSAGE_TOO_LARGE", "SSH frameの上限を超えました")
    return decode(read(length))


def write_frame(stream, document):
    body = encode(document)
    pending = memoryview(len(body).to_bytes(4, "big") + body)
    while pending:
        written = stream.write(pending)
        if not written:
            raise BrokenPipeError()
        pending = pending[written:]
    stream.flush()


class ConnectionJournal:
    """本文・tokenを記録せず、接続と受理済み操作の未確定状態を耐久化する。"""
    def __init__(self, directory: Path, binding: dict):
        require_private(directory.parent, directory=True)
        if not directory.exists():
            storage.mkdir(directory)
        require_private(directory, directory=True)
        self.directory = directory / uuid.uuid4().hex
        storage.mkdir(self.directory)
        self.record = dict(binding, state="READY")
        self.state("READY")

    def _write(self, path, document):
        if path.exists():
            require_private(path)
            require(path.is_file() and path.stat().st_nlink == 1, "INVALID_STATE", "独立した証跡が必要です")
            staged = path.with_name(path.name + "." + uuid.uuid4().hex)
            storage.write(staged, encode(document))
            storage.move(staged, path, replace=True)
        else:
            storage.write(path, encode(document))

    def state(self, value):
        self.record["state"] = value
        self._write(self.directory / "connection.json", self.record)

    def accepted(self, document):
        operation = identifier(document["operationId"], "operationId")
        path = self.directory / (operation + ".json")
        require(not path.exists(), "REQUEST_REPLAY", "同じ接続で要求IDを再受理しません")
        fingerprint = hashlib.sha256(encode(document)).hexdigest()
        record = {"operationId": operation, "command": document["command"],
                  "fingerprint": fingerprint, "state": "PENDING"}
        self._write(path, record)
        return path, record

    def finished(self, entry, response):
        path, record = entry
        record["state"] = ("COMPLETED" if response["ok"] else
                           "REJECTED" if response["error"].get("rejectedBeforeEffect") else "UNKNOWN")
        record["responseSha256"] = hashlib.sha256(encode(response)).hexdigest()
        self._write(path, record)

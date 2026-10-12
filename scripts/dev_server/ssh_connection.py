"""承認済みprofileへのOpenSSH接続。自動再接続・再送・kill・返却は行わない。"""
import base64
from pathlib import Path, PurePosixPath, PureWindowsPath
import queue
import shlex
import subprocess
import threading
import uuid

from .connection_profiles import profile_hash, validate_profile
from .local_ipc import encode, plain_path
from .registry import RegistryError, identifier, require, ttl
from .ssh_protocol import COMMANDS, ConnectionJournal, read_frame, write_frame


def remote_command(profile):
    """profileの固定パスだけを引用する。protocol本文をshell文字列に加えない。"""
    validate_profile(profile)
    arguments = [profile["python"], "-B", profile["cli"], "ssh-broker", "--root", profile["managementRoot"]]
    if profile["targetOs"] == "macos":
        return "cd -- " + shlex.quote(str(PurePosixPath(profile["cli"]).parent)) + " && exec " + shlex.join(arguments)
    # stdinは子へ引き継ぎ、PowerShellが再符号化するnative出力だけbyteのまま中継する。
    bridge = """using System; using System.Diagnostics;
public static class BrokerPipe {
 public static int Run(string executable, string arguments, string directory) {
  var info = new ProcessStartInfo(executable, arguments) { UseShellExecute = false,
   RedirectStandardOutput = true, WorkingDirectory = directory };
  using (var process = Process.Start(info)) {
   var output = process.StandardOutput.BaseStream.CopyToAsync(Console.OpenStandardOutput());
   process.WaitForExit(); output.GetAwaiter().GetResult(); return process.ExitCode;
  }
 }
}"""
    literal = lambda value: "'" + value.replace("'", "''") + "'"
    script = "Add-Type -TypeDefinition " + literal(bridge) + "; exit ([BrokerPipe]::Run(" + literal(arguments[0]) + "," + literal(subprocess.list2cmdline(arguments[1:])) + "," + literal(str(PureWindowsPath(profile["cli"]).parent)) + "))"
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def ssh_arguments(profile, known_hosts: Path, *, ssh="ssh", timeout=5):
    validate_profile(profile)
    require(profile["transport"] == "ssh", "INVALID_INPUT", "SSH profileが必要です")
    ttl(timeout)
    require(known_hosts.is_absolute(), "INVALID_INPUT", "既確認のknown_hostsを絶対パスで指定してください")
    plain_path(known_hosts)
    require(known_hosts.is_file(), "INVALID_INPUT", "既確認のknown_hostsがありません")
    path = known_hosts.as_posix()
    require(not any(char in path for char in ('"', '%', '$', '\n', '\r')), "INVALID_INPUT", "known_hostsの展開文字は使用できません")
    options = ["BatchMode=yes", "StrictHostKeyChecking=yes", "UpdateHostKeys=no", "VerifyHostKeyDNS=no",
               'UserKnownHostsFile="' + path + '"', "GlobalKnownHostsFile=none", "ForwardAgent=no",
               "ClearAllForwardings=yes", "PermitLocalCommand=no", "RemoteCommand=none", "RequestTTY=no",
               "ControlMaster=no", "ControlPath=none", "ConnectionAttempts=1", "LogLevel=ERROR",
               "KnownHostsCommand=none", "CheckHostIP=no", "ControlPersist=no", "Tunnel=no", "EscapeChar=none",
               "ProxyCommand=none", "ProxyJump=none", "Hostname=" + profile["host"], "HostKeyAlias=" + profile["host"],
               "ConnectTimeout=" + str(timeout)]
    return [ssh, "-T", "-a", "-x", *[part for option in options for part in ("-o", option)],
            "-l", profile["sshUser"], profile["host"], remote_command(profile)]


class SshConnection:
    """一接続・一枠を直列操作する。戻るCLI envelopeのok/API successを保持する。

    journal_parentはGit外のConnectionStore.directoryを指定する。各要求のIDは一度だけ使用し、
    timeout/応答喪失後はこの接続を再利用しない。close成功にもSSH子プロセスの正常終了を要する。
    """
    def __init__(self, profile, instance_id, client_id, *, known_hosts, journal_parent, ssh="ssh", timeout=5):
        validate_profile(profile)
        identifier(instance_id, "instanceId")
        identifier(client_id, "clientId")
        require(instance_id in {item["instanceId"] for item in profile["instances"]}, "NOT_FOUND", "profileの枠候補が必要です")
        self.journal = ConnectionJournal(journal_parent / "ssh-clients",
                                         {"profileHash": profile_hash(profile), "instanceId": instance_id, "clientId": client_id})
        self.lock = threading.Lock()
        self.responses = queue.Queue(maxsize=1)
        self.unknown = False
        self.closed = False
        try:
            self.process = subprocess.Popen(ssh_arguments(profile, known_hosts, ssh=ssh, timeout=timeout),
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            bufsize=0)
            self.reader = threading.Thread(target=self._read, daemon=True, name="ssh-broker-response")
            self.reader.start()
            response = self.request("hello", {"profile": profile, "instanceId": instance_id, "clientId": client_id},
                                    operation_id=uuid.uuid4().hex, timeout=timeout)
            require(response["ok"] and response["result"].get("profileHash") == profile_hash(profile),
                    "SSH_UNAVAILABLE", "SSH認証・host key・接続先契約を確認してください")
        except BaseException:
            self._unknown()
            raise

    def _read(self):
        try:
            while True:
                self.responses.put_nowait(read_frame(self.process.stdout))
        except (OSError, EOFError, ValueError, RegistryError, queue.Full):
            try:
                self.responses.put_nowait(None)
            except queue.Full:
                pass
        finally:
            self.process.stdout.close()

    def _unknown(self):
        already_unknown = self.unknown
        self.unknown = True
        self.journal.state("UNKNOWN")
        if hasattr(self, "process") and not already_unknown:
            def close_input():
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            # 同期write中のcloseが期限を延ばしても、UNKNOWNの返却を待たせない。
            threading.Thread(target=close_input, daemon=True, name="ssh-broker-abandon").start()

    def request(self, command, payload, *, operation_id, timeout=5):
        identifier(operation_id, "operationId")
        ttl(timeout)
        require(type(command) is str and command in COMMANDS | {"hello"}, "INVALID_INPUT", "SSH仲介の許可対象外です")
        document = {"version": 1, "command": command, "operationId": operation_id, "payload": payload}
        encode(document)
        with self.lock:
            require(not self.unknown and not self.closed, "CONNECTION_PENDING", "終了済み・結果不明の接続は再利用できません")
            entry = self.journal.accepted(document)
            try:
                def send():
                    try:
                        write_frame(self.process.stdin, document)
                    except (OSError, ValueError):
                        try:
                            self.responses.put_nowait(None)
                        except queue.Full:
                            pass
                threading.Thread(target=send, daemon=True, name="ssh-broker-request").start()
                response = self.responses.get(timeout=timeout)
                require(type(response) is dict and response.get("operationId") == operation_id
                        and type(response.get("ok")) is bool and type(response.get("result") if response["ok"] else response.get("error")) is dict,
                        "SSH_UNAVAILABLE", "SSH認証または応答の確認に失敗しました")
                self.journal.finished(entry, response)
                if not response["ok"] and not response["error"].get("rejectedBeforeEffect"):
                    self._unknown()
                if command == "close" and response["ok"]:
                    self.process.stdin.close()
                    require(self.process.wait(timeout=timeout) == 0, "SSH_UNAVAILABLE", "SSH正常終了を確認できません")
                    self.reader.join(timeout)
                    self.closed = True
                    self.journal.state("CLOSED")
                return response
            except (OSError, ValueError, RegistryError, queue.Empty, subprocess.TimeoutExpired):
                self._unknown()
                raise RegistryError("SSH_UNAVAILABLE", "SSH操作の結果が不明です。証跡を保全し再送・解放しません") from None

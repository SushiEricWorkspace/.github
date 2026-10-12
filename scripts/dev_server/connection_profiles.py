"""Manager・人間用入口が共有する非秘密プロファイルと一回限りの貸出引継ぎ。"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
import time
import uuid

from .build_lock import build_lock
from .contracts import absolute_path
from .local_ipc import MAX_MESSAGE, decode, encode, plain_path, require_private
from .registry import RegistryError, fields, identifier, require
from . import storage

PROFILE_FIELDS = {"version", "profileId", "displayName", "transport", "targetOs", "host",
                  "managementRoot", "python", "cli", "instances", "approval"}
TOKEN_FIELDS = {"instanceId", "leaseId", "epoch", "expectedGeneration"}
HANDOFF_FIELDS = {"version", "handoffId", "profileHash", "owner", "sourceClient", "targetClient",
                  "sessionId", "token", "runId", "nonce", "expiresAt", "state"}


def validate_profile(profile: dict) -> None:
    """未知項目・秘密用項目を拒否する。承認の実在や対象ホストの実体は証明しない。"""
    fields(profile, PROFILE_FIELDS | ({"sshUser"} if type(profile) is dict and profile.get("transport") == "ssh" else set()))
    if profile["transport"] == "ssh":
        require(type(profile["sshUser"]) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@\\-]{0,127}", profile["sshUser"]) is not None,
                "INVALID_INPUT", "承認済みのSSHユーザー名が必要です")
    require(type(profile["version"]) is int and profile["version"] == 1,
            "INVALID_INPUT", "profile version=1が必要です")
    identifier(profile["profileId"], "profileId")
    require(profile["transport"] in ("local", "ssh") and profile["targetOs"] in ("windows", "macos"),
            "INVALID_INPUT", "transport/targetOsが不正です")
    require(type(profile["displayName"]) is str and bool(profile["displayName"])
            and all(ord(char) >= 32 and ord(char) != 127 for char in profile["displayName"]),
            "INVALID_INPUT", "表示名は空でない一行の文字列にしてください")
    require(type(profile["host"]) is str and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,253}", profile["host"]) is not None,
            "INVALID_INPUT", "hostは明示したホスト名または既存SSH設定のHost名にしてください")
    for key in ("managementRoot", "python", "cli"):
        require(type(profile[key]) is str, "INVALID_INPUT", "対象ホストの絶対パスが必要です")
        path = absolute_path(profile[key], key)
        require(isinstance(path, PureWindowsPath) == (profile["targetOs"] == "windows"),
                "INVALID_INPUT", "パス形式と対象ホストOSが一致しません")
    require(type(profile["instances"]) is list and bool(profile["instances"]),
            "INVALID_INPUT", "承認済み枠候補が必要です")
    ids = []
    for instance in profile["instances"]:
        fields(instance, {"instanceId", "displayName"})
        ids.append(identifier(instance["instanceId"], "instanceId"))
        require(type(instance["displayName"]) is str and bool(instance["displayName"])
                and all(ord(char) >= 32 and ord(char) != 127 for char in instance["displayName"]),
                "INVALID_INPUT", "枠表示名は空でない一行の文字列にしてください")
    require(len(ids) == len(set(ids)), "INVALID_INPUT", "枠候補が重複しています")
    fields(profile["approval"], {"operator", "reference"})
    identifier(profile["approval"]["operator"], "operator")
    require(type(profile["approval"]["reference"]) is str and bool(profile["approval"]["reference"])
            and all(ord(char) >= 32 and ord(char) != 127 for char in profile["approval"]["reference"]),
            "INVALID_INPUT", "ホスト・rootの運用承認を参照する記録が必要です")


def profile_hash(profile: dict) -> str:
    """編集前の引継ぎが別ホスト・root・CLIへ流用されないよう全固定設定を束縛する。"""
    validate_profile(profile)
    return hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def validate_handoff(record: dict) -> None:
    fields(record, HANDOFF_FIELDS)
    require(type(record["version"]) is int and record["version"] == 1,
            "INVALID_INPUT", "handoff version=1が必要です")
    for key in ("handoffId", "owner", "sourceClient", "targetClient", "sessionId", "runId", "nonce"):
        identifier(record[key], key)
    require(record["sourceClient"] != record["targetClient"], "INVALID_INPUT", "引継ぎ先を明示してください")
    require(type(record["profileHash"]) is str and re.fullmatch(r"[0-9a-f]{64}", record["profileHash"]) is not None,
            "INVALID_INPUT", "profile hashが不正です")
    fields(record["token"], TOKEN_FIELDS)
    for key in ("instanceId", "leaseId", "expectedGeneration"):
        identifier(record["token"][key], key)
    require(type(record["token"]["epoch"]) is int and record["token"]["epoch"] > 0,
            "INVALID_INPUT", "貸出epochが不正です")
    require(type(record["expiresAt"]) in (int, float) and math.isfinite(record["expiresAt"])
            and record["expiresAt"] > 0, "INVALID_INPUT", "貸出期限が不正です")
    require(record["state"] in ("OFFERED", "CLAIMING", "CLAIMED", "UNKNOWN"),
            "INVALID_INPUT", "引継ぎ状態が不正です")


def check_handoff(profile: dict, record: dict, checked: dict, *, owner: str,
                  target_client: str, now: float) -> None:
    """同じCLIのlease-check結果と照合する。statusのコピーを貸出の証明に使わない。"""
    validate_handoff(record)
    require(record["profileHash"] == profile_hash(profile), "PROFILE_CHANGED", "固定設定が変わっています")
    require(record["owner"] == owner and record["targetClient"] == target_client,
            "OWNER_MISMATCH", "引継ぎのownerまたは接続先clientが一致しません")
    token = record["token"]
    require(token["instanceId"] in {item["instanceId"] for item in profile["instances"]},
            "INVALID_INPUT", "プロファイルの枠候補にありません")
    require(record["expiresAt"] > now and type(checked.get("expiresAt")) in (int, float)
            and math.isfinite(checked["expiresAt"]) and checked["expiresAt"] > now,
            "LEASE_EXPIRED", "期限切れの引継ぎは自動更新・解放しません")
    require(checked.get("owner") == owner and checked.get("instanceId") == token["instanceId"],
            "OWNER_MISMATCH", "現在の貸出ownerまたは枠が一致しません")
    require(type(checked.get("epoch")) is int and checked["epoch"] == token["epoch"],
            "STALE_LEASE", "貸出epochが一致しません")
    require(checked.get("generation") == token["expectedGeneration"] and checked.get("generationBound") is True,
            "STALE_GENERATION", "貸出に固定した世代が一致しません")
    require(checked.get("runId") == record["runId"] and checked.get("nonce") == record["nonce"],
            "STALE_RUN", "起動識別が一致しません")
    require(checked.get("state") == "RUNNING" and checked.get("pending") == 0
            and not any(item.get("status") == "UNKNOWN" for item in checked.get("writerSessions", [])),
            "RECOVERY_REQUIRED", "稼働状態・未完了writerを照合してください")


def local_request(profile: dict, command: str, payload: dict, *, operation_id: str,
                  timeout: int = 5) -> dict:
    """貸出検査専用のローカル入口。秘密本文はstdinに送り、診断・argvへ転載しない。"""
    validate_profile(profile)
    require(command == "lease-check", "INVALID_INPUT", "この入口は貸出検査専用です")
    require(profile["transport"] == "local" and profile["targetOs"] == {"win32": "windows", "darwin": "macos"}.get(sys.platform),
            "UNAVAILABLE", "対象ホストのローカル実行または別途SSH仲介が必要です")
    identifier(operation_id, "operationId")
    require(type(timeout) is int and timeout > 0, "INVALID_INPUT", "待機期限は正整数にしてください")
    arguments = [profile["python"], "-B", profile["cli"], command, "--root", profile["managementRoot"],
                 "--operation-id", operation_id, "--payload-stdin", "--timeout-seconds", str(timeout)]
    try:
        process = subprocess.run(arguments, input=encode(payload), capture_output=True, timeout=timeout + 2)
        require(len(process.stdout) <= MAX_MESSAGE, "UNAVAILABLE", "貸出検査の応答上限を超えました")
        response = decode(process.stdout)
        require(response.get("operationId") == operation_id and type(response.get("ok")) is bool,
                "UNAVAILABLE", "貸出検査の応答識別が一致しません")
        require(process.returncode == 0 and response["ok"] is True and type(response.get("result")) is dict,
                "UNAVAILABLE", "貸出検査が拒否されました。自動解除せず診断してください")
        return response["result"]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise RegistryError("UNAVAILABLE", "貸出検査の結果が不明です。自動再送・解放しません") from None


class ConnectionStore:
    """Git外の明示したprivateディレクトリに固定設定と引継ぎを分離して保存する。"""
    def __init__(self, directory: Path):
        require(directory.is_absolute(), "INVALID_INPUT", "保存先はGit外の明示した絶対パスにしてください")
        plain_path(directory)
        require(not any((parent / ".git").exists() or parent.name == ".git"
                        for parent in (directory, *directory.parents)),
                "INVALID_INPUT", "プロファイル・引継ぎをGit内へ保存できません")
        if not directory.exists():
            storage.mkdir(directory)
        require_private(directory, directory=True)
        self.directory = directory
        for name in ("profiles", "handoffs"):
            child = directory / name
            if not child.exists():
                storage.mkdir(child)
            require_private(child, directory=True)
        lock = directory / "handoffs.lock"
        if not lock.exists():
            storage.write(lock, b"0")
        require_private(lock)

    def _path(self, kind, name):
        identifier(name, "recordId")
        return self.directory / kind / (name + ".json")

    def _read(self, path):
        require_private(path)
        require(path.is_file() and path.stat().st_nlink == 1, "INVALID_INPUT", "独立した通常記録が必要です")
        with path.open("rb") as handle:
            body = handle.read(MAX_MESSAGE + 1)
        require(len(body) <= MAX_MESSAGE, "INVALID_INPUT", "記録の上限を超えています")
        try:
            return decode(body)
        except ValueError:
            raise RegistryError("INVALID_INPUT", "記録は重複項目なしの有限JSON objectにしてください") from None

    def _write(self, path, record, *, replace=False):
        data = encode(record)
        if replace:
            staged = path.with_name(path.name + "." + uuid.uuid4().hex)
            storage.write(staged, data)
            storage.move(staged, path, replace=True)
        else:
            storage.write(path, data)

    def save_profile(self, profile: dict, *, replace=False) -> None:
        """新規登録。編集はreplaceを明示し、既存private記録だけを置換する。"""
        validate_profile(profile)
        self._write(self._path("profiles", profile["profileId"]), profile, replace=replace)

    def profile(self, profile_id: str) -> dict:
        profile = self._read(self._path("profiles", profile_id))
        validate_profile(profile)
        require(profile["profileId"] == profile_id, "INVALID_INPUT", "profileの記録IDが一致しません")
        return profile

    def offer(self, profile: dict, token: dict, checked: dict, *, owner: str,
              source_client: str, target_client: str, now: float | None = None) -> str:
        """発行元のlease-check後に作成。返り値はIDのみ。更新・返却責任は発行元に残る。"""
        now = time.time() if now is None else now
        record = {"version": 1, "handoffId": uuid.uuid4().hex, "profileHash": profile_hash(profile),
                  "owner": owner, "sourceClient": source_client, "targetClient": target_client,
                  "sessionId": uuid.uuid4().hex, "token": deepcopy(token), "runId": checked.get("runId"),
                  "nonce": checked.get("nonce"), "expiresAt": checked.get("expiresAt"), "state": "OFFERED"}
        check_handoff(profile, record, checked, owner=owner, target_client=target_client, now=now)
        with build_lock(self.directory / "handoffs.lock"):
            self._write(self._path("handoffs", record["handoffId"]), record)
        return record["handoffId"]

    def claim(self, profile: dict, handoff_id: str, *, owner: str, target_client: str,
              lease_check, clock=time.time) -> dict:
        """明示された一記録だけを一度取得する。callbackは同じホストCLIのlease-checkを呼ぶ。

        CLAIMINGを先に耐久化する。応答喪失・途中終了は再取得せず保全する。
        返るsessionIdで新たなwriter-openを行い、その結果不明も呼出元が保全する。
        """
        with build_lock(self.directory / "handoffs.lock"):
            path = self._path("handoffs", handoff_id)
            record = self._read(path)
            validate_handoff(record)
            require(record["handoffId"] == handoff_id, "INVALID_INPUT", "引継ぎIDが一致しません")
            require(record["state"] == "OFFERED", "HANDOFF_PENDING", "取得済み・結果不明の引継ぎは再取得できません")
            require(record["owner"] == owner and record["targetClient"] == target_client,
                    "OWNER_MISMATCH", "他owner/clientの引継ぎは取得できません")
            require(record["profileHash"] == profile_hash(profile), "PROFILE_CHANGED", "固定設定が変わっています")
            require(record["expiresAt"] > clock(), "LEASE_EXPIRED", "引継ぎ記録が期限切れです")
            record["state"] = "CLAIMING"
            self._write(path, record, replace=True)
            try:
                checked = lease_check(dict(record["token"], owner=owner))
                check_handoff(profile, record, checked, owner=owner, target_client=target_client, now=clock())
                record["state"] = "CLAIMED"
                self._write(path, record, replace=True)
            except BaseException:
                record["state"] = "UNKNOWN"
                self._write(path, record, replace=True)
                raise
            return record

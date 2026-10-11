"""Windowsの承認済み停止rootで、永続2枠/GameTest1枠と通常ファイル割当量を計測する。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_server.backup_verification import validate_trial_paths
from dev_server.local_ipc import make_private, plain_path, request
from dev_server.registry import Registry, RegistryError, require
from dev_server.runtime_bundle import _files
from dev_server import runtime_process
from dev_server.supervisor import new_request


def file_capacity(root):
    """通常ファイルの論理bytesとNTFS割当bytesを集計する。FS metadataは含めない。"""
    class StandardInfo(ctypes.Structure):
        _fields_ = [("allocation", ctypes.c_longlong), ("length", ctypes.c_longlong),
                    ("links", wintypes.DWORD), ("delete_pending", ctypes.c_ubyte), ("directory", ctypes.c_ubyte)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    entries = _files(root)
    allocated = 0
    for entry in entries:
        path = root / entry["path"]
        plain_path(path)
        handle = kernel.CreateFileW("\\\\?\\" + str(path), 0x80, 7, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = StandardInfo()
            if not kernel.GetFileInformationByHandleEx(handle, 1, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            require(not info.directory and not info.delete_pending and info.links == 1
                    and info.length == entry["size"] and info.allocation >= 0,
                    "PROOF_MISMATCH", "計測中の通常ファイルが変化しました")
            allocated += info.allocation
        finally:
            kernel.CloseHandle(handle)
    return {"files": len(entries), "logicalBytes": sum(entry["size"] for entry in entries),
            "fileAllocatedBytes": allocated, "includesFilesystemMetadata": False}


def memory_sample(runs):
    """所有識別を前後照合した3JVMのworking setとprivate commitを合計する。"""
    require(len(runs) == 3 and all(run is not None and run["identity"] is not None for run in runs),
            "INVALID_STATE", "3つのJVM識別が必要です")
    def verify():
        for run in runs:
            runtime_process.require_identity(run["identity"], runtime_process.identity(run["identity"]["pid"]))
    verify()
    ids = ",".join(str(int(run["identity"]["pid"])) for run in runs)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
            f"Get-Process -Id {ids} -ErrorAction Stop | Select-Object Id,WorkingSet64,PrivateMemorySize64 | ConvertTo-Json -Compress"],
            check=False, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
    verify()
    require(result.returncode == 0, "PROCESS_UNVERIFIED", "所有JVMのメモリを取得できませんでした")
    values = json.loads(result.stdout)
    require(type(values) is list and {value["Id"] for value in values} == {run["identity"]["pid"] for run in runs},
            "PROCESS_MISMATCH", "メモリ計測のPID集合が一致しません")
    return {"at": time.time(), "processes": values, "sumWorkingSetBytes": sum(value["WorkingSet64"] for value in values),
            "sumPrivateCommittedBytes": sum(value["PrivateMemorySize64"] for value in values)}


def main():
    parser = argparse.ArgumentParser()
    for name in ("repo", "root", "evidence"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--snapshot", required=True)
    args = parser.parse_args()
    require(os.name == "nt", "UNSUPPORTED_OS", "この計測ハーネスはWindows専用です")
    for path in (args.repo, args.root, args.evidence):
        plain_path(path)
        require(path.is_absolute(), "INVALID_INPUT", "絶対パスを指定してください")
    validate_trial_paths(args.root, [args.evidence])
    registry = Registry(args.root)
    require(registry.inventory["operator"] == args.operator, "INVALID_INPUT", "承認した運用者が一致しません")
    require(not args.evidence.exists(), "EXISTING_DATA", "既存証跡を上書きしません")
    with registry.transaction(read_only=True) as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM instances ORDER BY id")]
    require(len(rows) == 2 and all(row["lease"] is None and row["state"] == "RECOVERY_REQUIRED" for row in rows),
            "INVALID_STATE", "正常終了した試験rootの未貸出2枠だけを指定してください")
    args.evidence.mkdir(parents=True)
    make_private(args.evidence, directory=True)
    report = {"testOnly": True, "productionEnabled": False, "steps": []}
    def record(step, result):
        report["steps"].append({"step": step, "result": result})
        path = args.evidence / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        make_private(path)
        print(json.dumps({"step": step, "result": result}, ensure_ascii=False), flush=True)
    def call(command, payload, operation):
        response = request(args.root, new_request(command, payload, operation_id=operation), timeout=900)
        require(response["ok"], response.get("error", {}).get("code", "REQUEST_FAILED"),
                response.get("error", {}).get("message", "計測要求が拒否されました"))
        return response["result"]
    def runs(ids):
        with Registry(args.root).transaction(read_only=True) as connection:
            values = [connection.execute("SELECT record FROM runs WHERE id=?", (identifier,)).fetchone() for identifier in ids]
        return [json.loads(row[0]) if row else None for row in values]
    try:
        record("capacity-before", file_capacity(args.root))
        with (args.evidence / "supervisor.log").open("xb") as log:
            process = subprocess.Popen([sys.executable, "-B", str(args.repo / "scripts/dev-server.py"), "supervise", "--root", str(args.root)],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
        record("supervisor", {"pid": process.pid})
        deadline = time.monotonic() + 30
        while True:
            require(process.poll() is None, "INVALID_STATE", "試験supervisorが終了しました")
            try:
                call("doctor", {}, "capacity-doctor")
                break
            except OSError:
                require(time.monotonic() < deadline, "TIMEOUT", "試験supervisorに接続できません")
                time.sleep(0.2)
        tokens, running = [], []
        for row in rows:
            identifier = row["id"]
            token = {"instanceId": identifier, "leaseId": None, "epoch": row["epoch"], "expectedGeneration": row["generation"]}
            call("recover", dict(token, action="resume", targetOperationId=None, ttlSeconds=7200), "capacity-recover-" + identifier)
            lease = call("acquire", {"instanceId": identifier, "owner": "codex-issue-537-capacity", "ttlSeconds": 7200}, "capacity-acquire-" + identifier)
            token.update(leaseId=lease["leaseId"], epoch=lease["epoch"], expectedGeneration=lease["generation"])
            generation = "capacity-" + identifier
            started = time.monotonic()
            restored = call("restore", dict(token, generationId=generation, snapshotId=args.snapshot, artifactId=row["artifact"], acceptEula=True), "capacity-restore-" + identifier)
            token["expectedGeneration"] = restored["generation"]
            record("restore-" + identifier, {"seconds": time.monotonic() - started})
            started = time.monotonic()
            value = call("start", dict(token, timeoutSeconds=240), "capacity-start-" + identifier)
            record("start-" + identifier, dict(value, seconds=time.monotonic() - started))
            tokens.append(token)
            running.append(value["runId"])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(call, "gametest", {"runId": "capacity-game-test", "owner": "codex-issue-537-capacity",
                                 "artifactId": rows[0]["artifact"], "timeoutSeconds": 240, "acceptEula": True}, "capacity-game-test")
            samples, skipped = [], []
            while not future.done():
                values = runs([*running, "capacity-game-test"])
                if values[-1] is not None and values[-1]["identity"] is not None:
                    try:
                        samples.append(memory_sample(values))
                    except RegistryError as error:
                        require(error.code in {"PROCESS_MISMATCH", "PROCESS_UNVERIFIED"}, error.code, str(error))
                        skipped.append({"at": time.time(), "code": error.code})
                    except OSError as error:
                        skipped.append({"at": time.time(), "code": "NATIVE_SAMPLE_UNAVAILABLE", "winerror": error.winerror})
                time.sleep(1)
            record("game-test", future.result())
        require(samples, "CONCURRENCY_NOT_OBSERVED", "3JVM併走中のメモリを計測できませんでした")
        record("memory", {"samples": samples, "skippedSamples": skipped, "peakSumWorkingSetBytes": max(value["sumWorkingSetBytes"] for value in samples),
               "peakSumPrivateCommittedBytes": max(value["sumPrivateCommittedBytes"] for value in samples),
               "worstCaseGuarantee": False})
        for token in reversed(tokens):
            identifier = token["instanceId"]
            record("stop-" + identifier, call("stop", dict(token, timeoutSeconds=180), "capacity-stop-" + identifier))
            call("release", token, "capacity-release-" + identifier)
        call("shutdown", {}, "capacity-shutdown")
        process.wait(timeout=30)
        require(process.returncode == 0, "INVALID_STATE", "試験supervisorが正常終了していません")
        record("capacity-after", file_capacity(args.root))
        record("finished", {"supervisorExitCode": process.returncode})
    except BaseException as error:
        record("failure-preserved", {"type": type(error).__name__, "message": str(error), "automaticKillOrRetry": False})
        raise


if __name__ == "__main__":
    main()

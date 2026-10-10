"""固定seedの生成仕様と合成契約文書。実ワールド/NBT/JAR/停止証跡ではない。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from dev_server.contracts import LAYOUT_ID, ROOT_FILES


FIXTURE_FILE = Path(__file__).with_name("fixtures-v1.json")
FIXTURES = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))


def settings_expectations() -> dict:
    """A23の入力・可搬結果・復元期待値。変換処理ではなく固定の合成試験仕様。"""
    return {
        "source": {
            "server.properties": {"level-name": "world", "level-seed": 52452502,
                                  "server-port": 25665, "server-ip": "127.0.0.1", "rcon.port": 25675,
                                  "rcon.password": "synthetic-source-secret"},
            "config/SushiEricServerMod/management/config.yml": {"port": 25680},
        },
        "portable": {
            "server.properties": {"level-name": "world", "level-seed": 52452502},
            "config/SushiEricServerMod/management/config.yml": {},
        },
        "restored": {
            "server.properties": {"level-name": "world", "level-seed": 52452502,
                                  "server-port": 25666, "server-ip": "127.0.0.1", "rcon.port": 25676,
                                  "rcon.password": "synthetic-destination-secret"},
            "config/SushiEricServerMod/management/config.yml": {"port": 25681},
        },
    }


def file_entries(data: dict[str, bytes]) -> list[dict]:
    """合成データの期待サイズ/hashを列挙する。ディスクへの書き込みは行わない。"""
    return [{"path": path, "kind": "regular", "size": len(content),
             "sha256": hashlib.sha256(content).hexdigest()} for path, content in sorted(data.items())]


def contract_documents(name: str) -> tuple[dict, dict[str, bytes]]:
    """各契約の検証用文書と通常ファイルの合成集合を毎回独立して返す。"""
    fixture = deepcopy(next(fixture for fixture in FIXTURES if fixture["name"] == name))
    expected = fixture["expected"]
    payload = {file: b"[]\n" for file in ROOT_FILES - {"server.properties"}}
    payload["server.properties"] = f"level-name=world\nlevel-seed={fixture['seed']}\n".encode()
    payload["config/SushiEricServerMod/config.yml"] = f"mining:\n  ore_revival_second: {expected['oreRevivalSecond']}\n".encode()
    payload[expected["modProfileFile"]] = f"money: {expected['money']}\n".encode()
    # NBTと独自定義は後続harnessが現在のAPIで生成する。ここでは偽.dat/.ymlを作らない。
    for path in expected["dimensionMarkers"] + [expected["unknownRegularFile"]]:
        payload[path] = b"synthetic-contract-marker\n"
    layout = {"schemaVersion": 1, "layoutId": LAYOUT_ID, "roots": {"world": "world", "config": "config"},
              "externalRoots": [], "databaseAdapters": [], "rootFiles": sorted(ROOT_FILES),
              "excludedPaths": ["world/session.lock", "logs", "crash-reports"], "transformVersion": 1}
    artifact_id = hashlib.sha256(b"synthetic-contract-runtime-not-a-launchable-bundle").hexdigest()
    clean = {"instanceId": "persistent-01", "generation": "generation-fixture-01", "runId": "contract-run-01",
             "nonce": "synthetic-nonce-not-a-stop-proof", "stopRequested": True, "jvmAndChildrenExited": True,
             "externalWritersExited": True, "backgroundWritersDrained": True,
             "saveResults": [{"writer": "synthetic-writer", "success": True, "pendingCount": 0}]}
    instances = [{"schemaVersion": 1, "instanceId": f"persistent-{index + 1:02}", "host": "localhost",
                  "minecraftBind": "127.0.0.1", "managementBind": "127.0.0.1",
                  "ports": {"minecraft": 25665 + index, "rcon": 25675 + index, "management": 25680 + index},
                  "queryEnabled": False, "jdwpEnabled": False} for index in range(2)]
    runtime = {"schemaVersion": 1, "artifactId": artifact_id, "layoutId": LAYOUT_ID, "development": True,
               "javaVersion": "21.0.8", "commonCoordinate": "io.github.sushiericworkspace:sushieric-common-mod-dev:0.1.0-dev.20261010000000000",
               "files": file_entries({"synthetic.classpath-entry": b"not-a-runtime"}),
               "sources": [{"repo": repo, "commit": "0" * 40, "dirtyPatchSha256": hashlib.sha256(b"").hexdigest(),
                            "untrackedInputs": []} for repo in ("Common", "SushiEricServerMod")],
               "tools": [{"name": name, "version": "synthetic-version", "sha256": artifact_id}
                         for name in ("jdk", "gradle", "loom", "minecraft", "yarn", "fabric-loader", "fabric-api", "kotlin")],
               "launch": {"mainClass": "synthetic.NotLaunchable", "classpath": ["synthetic.classpath-entry"], "jvmArgs": []}}
    manifest = {"schemaVersion": 1, "snapshotId": f"contract-{name}", "generation": clean["generation"],
                "artifactId": artifact_id, "layout": deepcopy(layout), "transformVersion": 1,
                "visibility": "private", "cleanStop": deepcopy(clean), "files": file_entries(payload)}
    restored_clean = dict(deepcopy(clean), instanceId="persistent-02")
    validation = {"schemaVersion": 1, "validationId": "contract-validation-01", "snapshotId": manifest["snapshotId"],
                  "artifactId": artifact_id, "fixture": name, "sourceInstanceId": "persistent-01", "restoredInstanceId": "persistent-02",
                  "runId": clean["runId"], "nonce": clean["nonce"], "cleanStop": restored_clean,
                  "passedChecks": expected["checks"], "reportFiles": file_entries({"contract-report.txt": b"synthetic-report"})}
    inventory = {"schemaVersion": 1, "operator": "fixture-operator", "managementRoot": "/synthetic/minecraft-dev",
                 "quotaBytes": 100000, "reserveBytes": 10000,
                 "backup": {"destination": "/Volumes/synthetic-backup", "separateMediumConfirmedBy": "fixture-operator"},
                 "instances": instances, "gameTestConcurrency": 1, "networkTestPortRange": [25700, 25799]}
    return {"fixture": fixture, "dataLayout": layout, "instance": instances[0], "inventory": inventory,
            "runtime": runtime, "manifest": manifest, "validation": validation}, payload

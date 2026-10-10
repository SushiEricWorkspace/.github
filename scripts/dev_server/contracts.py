"""#525の純粋な契約検証。コピー、JVM起動、秘密変換、運用証跡の認証は行わない。"""

import hashlib
import json
import re
import unicodedata
from pathlib import Path, PurePosixPath


class ContractError(ValueError):
    """契約の項目パスと拒否理由を返す。値に含まれる秘密は出力しない。"""


CONTRACT = json.loads(Path(__file__).with_name("contracts-v1.json").read_text(encoding="utf-8"))
TYPES = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
ROOT_FILES = {"server.properties", "ops.json", "whitelist.json", "banned-players.json", "banned-ips.json"}
LAYOUT_ID = "sushieric-generation-v1"


def _require(condition: bool, path: str, reason: str) -> None:
    if not condition:
        raise ContractError(f"{path}: {reason}")


def validate_relative_path(value: str, path: str = "path") -> None:
    """正規化で救済せず、POSIX形式の安全な相対パスだけを受理する。"""
    parts = value.split("/")
    _require(bool(value) and not value.startswith("/") and "\\" not in value and ":" not in value
             and all(part not in ("", ".", "..") for part in parts)
             and all(ord(char) >= 32 and ord(char) != 127 for char in value), path, "不正な相対パスです")


def _validate(value, schema: dict, path: str) -> None:
    if "ref" in schema:
        name = schema["ref"]
        schema = CONTRACT["definitions"].get(name) or CONTRACT["contracts"][name]
    _require(type(value) is TYPES[schema["type"]], path, "型が契約と一致しません")
    if "enum" in schema:
        _require(value in schema["enum"], path, "許可されていない値です")
    if isinstance(value, dict):
        fields = schema["properties"]
        _require(value.keys() == fields.keys(), path, "必須項目の欠落または未知項目があります")
        for key, child in fields.items():
            _validate(value[key], child, f"{path}.{key}")
    elif isinstance(value, list):
        _require(len(value) >= schema.get("minItems", 0), path, "要素が不足しています")
        _require(len(value) <= schema.get("maxItems", len(value)), path, "要素が多すぎます")
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{index}]")
    elif isinstance(value, str):
        _require(len(value) >= schema.get("minLength", 0), path, "空の値は指定できません")
        if "pattern" in schema:
            _require(re.fullmatch(schema["pattern"], value) is not None, path, "形式が不正です")
        if schema.get("format") == "relativePath":
            validate_relative_path(value, path)
        if schema.get("format") == "absolutePath":
            _require(value.startswith("/") and value != "/", path, "macOSの明示的な絶対パスが必要です")
            validate_relative_path(value[1:], path)
    elif type(value) is int:
        _require(value >= schema.get("minimum", value) and value <= schema.get("maximum", value), path, "範囲外です")


def _unique_files(files: list[dict], path: str) -> None:
    # macOSの大小文字/Unicode正規化による同名衝突も安全側に拒否する。
    names = [unicodedata.normalize("NFC", file["path"]).casefold() for file in files]
    _require(len(names) == len(set(names)), path, "重複パスがあります")
    for name in names:
        _require(not any(parent.as_posix() in names for parent in PurePosixPath(name).parents), path, "ファイルと親パスが衝突しています")


def validate(kind: str, value: dict) -> None:
    """文書の形と項目間制約を検査する。実際のFS・プロセス状態の証明ではない。"""
    _require(kind in CONTRACT["contracts"], "contract", "未知の契約です")
    _validate(value, CONTRACT["contracts"][kind], kind)
    if kind == "inventory":
        _require(value["reserveBytes"] < value["quotaBytes"], kind, "reserveBytesはquotaBytesより小さくしてください")
        backup = value["backup"]
        _require(backup["separateMediumConfirmedBy"] == value["operator"], kind, "運用者による別媒体の確認が必要です")
        root, destination = PurePosixPath(value["managementRoot"]), PurePosixPath(backup["destination"])
        _require(root != destination and root not in destination.parents and destination not in root.parents, kind, "バックアップ先と管理rootを分離してください")
        ids, ports = [], []
        for instance in value["instances"]:
            validate("instance", instance)
            ids.append(instance["instanceId"])
            ports.extend(instance["ports"].values())
        _require(len(set(ids)) == len(ids) and len(set(ports)) == len(ports), kind, "instanceまたは固定ポートが重複しています")
        lower, upper = value["networkTestPortRange"]
        _require(lower <= upper and not any(lower <= port <= upper for port in ports), kind, "通信試験範囲が不正です")
    if kind == "instance":
        _require(len(set(value["ports"].values())) == 3, kind, "固定ポートが重複しています")
    if kind == "dataLayout":
        world = value["roots"]["world"]
        _require(world.split("/")[0].casefold() not in {"config", "logs", "crash-reports"} | ROOT_FILES, kind, "worldと他のrootが重複しています")
        _require(set(value["rootFiles"]) == ROOT_FILES and len(value["rootFiles"]) == len(ROOT_FILES), kind, "設定・アカウントファイルを同世代へ含めてください")
        _require(value["excludedPaths"] == [f"{world}/session.lock", "logs", "crash-reports"], kind, "除外は契約の固定集合だけを指定してください")
    if kind in {"runtime", "manifest"}:
        _unique_files(value["files"], kind)
    if kind == "runtime":
        _require(value["layoutId"] == LAYOUT_ID, kind, "未知のdata-layoutです")
        coordinate = value["commonCoordinate"]
        _require(re.fullmatch(r"io\.github\.sushiericworkspace:sushieric-common-mod-dev:0\.1\.0-dev\.[0-9]{17}", coordinate) is not None, kind, "Commonの完全な開発座標が必要です")
        files = {entry["path"] for entry in value["files"]}
        _require(bool(value["launch"]["classpath"]) and all(
            entry in files or any(file.startswith(entry + "/") for file in files)
            for entry in value["launch"]["classpath"]), kind, "classpathはbundle内の記録済みファイル/ディレクトリに限定します")
        _require({entry["repo"] for entry in value["sources"]} >= {"Common", "SushiEricServerMod"}, kind, "CommonとModの入力記録が必要です")
        _require({entry["name"] for entry in value["tools"]} >= {"jdk", "gradle", "loom", "minecraft", "yarn", "fabric-loader", "fabric-api", "kotlin"}, kind, "実行・ビルドツールの固定記録が必要です")
        for key, name in (("sources", "repo"), ("tools", "name")):
            _require(len({entry[name] for entry in value[key]}) == len(value[key]), kind, "入力またはツール記録が重複しています")
    if kind == "manifest":
        validate("dataLayout", value["layout"])
        _require(value["generation"] == value["cleanStop"]["generation"], kind, "停止証跡の世代が一致しません")
        layout = value["layout"]
        for file in value["files"]:
            name = file["path"]
            _require(name in ROOT_FILES or any(name.startswith(root + "/") for root in layout["roots"].values()), kind, "未登録rootのファイルです")
            _require(not any(name == excluded or name.startswith(excluded + "/") for excluded in layout["excludedPaths"]), kind, "除外ファイルが含まれています")
        _require(ROOT_FILES <= {file["path"] for file in value["files"]}, kind, "可搬設定またはアカウントファイルが不足しています")
        _require(all(any(file["path"].startswith(root + "/") for file in value["files"]) for root in layout["roots"].values()), kind, "worldとconfigを同じ世代へ含めてください")
    if kind == "fixture":
        uuid, expected = value["testUuid"], value["expected"]
        _require(expected["vanillaPlayerFile"] == f"world/playerdata/{uuid}.dat"
                 and expected["modProfileFile"] == f"config/SushiEricServerMod/player_data/{uuid}/profile.yml", kind, "バニラとModのテストUUIDが一致しません")
    if kind == "validation":
        _require(value["sourceInstanceId"] != value["restoredInstanceId"], kind, "別枠での復元確認が必要です")
        proof = value["cleanStop"]
        _require((value["restoredInstanceId"], value["runId"], value["nonce"]) == (proof["instanceId"], proof["runId"], proof["nonce"]), kind, "復元試験と停止証跡の識別が一致しません")
        _unique_files(value["reportFiles"], kind)
        _require(len(set(value["passedChecks"])) == len(value["passedChecks"]), kind, "確認IDが重複しています")


def require_compatible(manifest: dict, runtime: dict) -> None:
    """初期運用では同じartifactとlayoutだけを許可し、未知の移行を推測しない。"""
    validate("manifest", manifest)
    validate("runtime", runtime)
    _require(manifest["artifactId"] == runtime["artifactId"] and manifest["layout"]["layoutId"] == runtime["layoutId"], "compatibility", "artifactまたはdata-layoutの互換性が不明です")


def verify_file_set(entries: list[dict], observed: dict[str, bytes]) -> None:
    """収集済み通常ファイルの集合・サイズ・hashを照合する。FS探索や公開は行わない。"""
    _validate(entries, CONTRACT["definitions"]["files"], "files")
    _unique_files(entries, "files")
    _require({entry["path"] for entry in entries} == observed.keys(), "files", "欠損または余分なファイルがあります")
    for entry in entries:
        data = observed[entry["path"]]
        _require(entry["size"] == len(data) and entry["sha256"] == hashlib.sha256(data).hexdigest(), "files", "サイズまたはSHA-256が一致しません")


def validate_portable_settings(settings: dict[str, dict]) -> None:
    """解析済み設定の許可キーを検査する。未知キーの内容・秘密値は出力しない。"""
    allowed = {
        "server.properties": {"level-name", "level-seed"},
        "config/SushiEricServerMod/config.yml": {"mining.ore_revival_second", "money.history_save_interval_second"},
        "config/SushiEricServerMod/management/config.yml": set(),
    }
    for file, values in settings.items():
        _require(file in allowed and type(values) is dict, "settings", "未分類の設定ファイルです")
        _require(values.keys() <= allowed[file], "settings", "秘密・ポート・未知の設定項目を含みます")

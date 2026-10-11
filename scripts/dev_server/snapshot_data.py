"""既知のゲーム保存rootを通常コピーするための収集・可搬設定変換。"""
import os
from pathlib import Path
import re
import stat

from .build_inputs import digest, regular_bytes
from .contracts import LAYOUT_ID, ROOT_FILES, absolute_path, validate_relative_path
from .local_ipc import plain_path
from .registry import require


PORTABLE_PROPERTIES = set("""
accepts-transfers allow-flight broadcast-console-to-ops broadcast-rcon-to-ops
bug-report-link difficulty enable-code-of-conduct enable-status enforce-secure-profile
enforce-whitelist entity-broadcast-range-percentage force-gamemode function-permission-level
gamemode generate-structures generator-settings hardcore hide-online-players
initial-disabled-packs initial-enabled-packs level-name level-seed level-type log-ips
max-chained-neighbor-updates max-players max-tick-time max-world-size motd
network-compression-threshold online-mode op-permission-level pause-when-empty-seconds
player-idle-timeout prevent-proxy-connections rate-limit region-file-compression
require-resource-pack resource-pack resource-pack-id resource-pack-prompt resource-pack-sha1
simulation-distance spawn-protection status-heartbeat-interval sync-chunk-writes
text-filtering-version use-native-transport view-distance white-list
""".split())
INSTANCE_PROPERTIES = {"server-ip", "server-port", "enable-rcon", "rcon.port", "rcon.password", "query.port",
                       "management-server-host", "management-server-port", "management-server-secret",
                       "management-server-allowed-origins", "management-server-tls-enabled",
                       "management-server-tls-keystore", "management-server-tls-keystore-password"}
DISABLED_PROPERTIES = {"enable-query", "enable-jmx-monitoring", "management-server-enabled"}
DATA_DIRECTORIES = {"player_data", "item_data", "ore_data", "shop_data", "recipe_data", "shop_history"}
DATA_FILES = {"placed_ores.yml", "placed_special_blocks.yml", "manager-compat.json"}
CONFIG_KEYS = {"mining.ore_revival_second", "money.history_save_interval_second"}


def properties(data: bytes) -> dict[str, str]:
    """バニラ生成のkey=value形式を読む。重複・継続行・未知keyを推測しない。"""
    result = {}
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        require("=" in line, "SETTINGS_UNSUPPORTED", "server.propertiesの形式が未対応です")
        key, value = line.split("=", 1)
        require(re.fullmatch(r"[a-z][a-z0-9.-]*", key) is not None and key not in result,
                "SETTINGS_UNSUPPORTED", "server.propertiesの項目が重複または未対応です")
        require((len(value) - len(value.rstrip("\\"))) % 2 == 0,
                "SETTINGS_UNSUPPORTED", "server.propertiesの継続行は未対応です")
        result[key] = value
    require(result.keys() <= PORTABLE_PROPERTIES | INSTANCE_PROPERTIES | DISABLED_PROPERTIES | {"text-filtering-config"},
            "SETTINGS_UNSUPPORTED", "server.propertiesに未分類の項目があります")
    require(all(result.get(key, "false") == "false" for key in DISABLED_PROPERTIES)
            and result.get("text-filtering-config", "") == ""
            and result.get("management-server-tls-keystore", "") == "",
            "SETTINGS_UNSUPPORTED", "未対応の外部サービス・秘密設定があります")
    return result


def portable_properties(data: bytes) -> bytes:
    values = properties(data)
    validate_relative_path(values.get("level-name", ""), "level-name")
    require("\\" not in values["level-name"], "ROOT_MISMATCH", "world rootのescapeは未対応です")
    return "".join(f"{key}={values[key]}\n" for key in sorted(values.keys() & PORTABLE_PROPERTIES)).encode()


def numeric_yaml(data: bytes, allowed: set[str]) -> bytes:
    """現行の数値設定だけを受理し、コメント・未知キー・YAML拡張を持ち込まない。"""
    values, parent, indent, parents = {}, None, None, set()
    for raw in data.decode("utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        match = re.fullmatch(r"( *)([a-z][a-z_]*):(?: *([0-9]+))?", line)
        require(match is not None, "SETTINGS_UNSUPPORTED", "設定YAMLの形式が未対応です")
        spaces, name, value = match.groups()
        if value is None:
            require(not spaces and name not in parents and any(key.startswith(name + ".") for key in allowed),
                    "SETTINGS_UNSUPPORTED", "設定階層が不正です")
            parents.add(name)
            parent, indent = name, None
            continue
        if spaces:
            require(parent is not None and (indent is None or len(spaces) == indent),
                    "SETTINGS_UNSUPPORTED", "設定階層が不正です")
            indent = len(spaces)
        key = parent + "." + name if spaces else name
        require(key in allowed and key not in values, "SETTINGS_UNSUPPORTED", "設定に未知・重複項目があります")
        values[key] = int(value)
    require(values, "SETTINGS_UNSUPPORTED", "必要な設定項目がありません")
    lines, group = [], None
    for key, value in sorted(values.items()):
        if "." in key:
            section, name = key.split(".")
            if group != section:
                lines.append(section + ":")
                group = section
            lines.append(f"  {name}: {value}")
        else:
            lines.append(f"{key}: {value}")
    return ("\n".join(lines) + "\n").encode()


def transform(name: str, data: bytes) -> bytes:
    if name == "server.properties":
        return portable_properties(data)
    if name == "config/SushiEricServerMod/config.yml":
        return numeric_yaml(data, CONFIG_KEYS)
    if name == "config/SushiEricServerMod/management/config.yml":
        numeric_yaml(data, {"port"})
        return b"{}\n"
    return data


def collect(generation: Path) -> tuple[dict, list[dict]]:
    """未知通常ファイルも全worldから収集する。リンク・hardlink・外部DBは拒否する。"""
    plain_path(generation)
    world = properties(regular_bytes(generation / "server.properties")).get("level-name", "")
    validate_relative_path(world, "level-name")
    layout = {"schemaVersion": 1, "layoutId": LAYOUT_ID, "roots": {"world": world, "config": "config"},
              "externalRoots": [], "databaseAdapters": [], "rootFiles": sorted(ROOT_FILES),
              "excludedPaths": [world + "/session.lock", "logs", "crash-reports"], "transformVersion": 1}
    from .contracts import validate
    validate("dataLayout", layout)
    allowed_top = {"runtime", "generation.json", "eula.txt", "logs", "crash-reports", "mods", "usercache.json",
                   world.split("/")[0], "config"} | ROOT_FILES
    require({path.name for path in generation.iterdir()} <= allowed_top,
            "ROOT_MISMATCH", "generationに未登録rootがあります")
    for path in generation.iterdir():
        plain_path(path)
    mods = generation / "mods"
    require(not mods.exists() or (mods.is_dir() and not any(mods.iterdir())),
            "ROOT_MISMATCH", "bundle外のModは未対応です")
    paths = [generation / name for name in ROOT_FILES]
    def fail(error):
        raise error
    for root in (world, "config"):
        directory = generation / root
        plain_path(directory)
        require(directory.is_dir(), "ROOT_MISMATCH", "保存rootがありません")
        for parent, dirs, names in os.walk(directory, followlinks=False, onerror=fail):
            for name in dirs + names:
                path = Path(parent) / name
                plain_path(path)
                if name in names and path.relative_to(generation).as_posix() != world + "/session.lock":
                    paths.append(path)
    entries = []
    for path in sorted(paths, key=lambda path: path.relative_to(generation).as_posix()):
        name = path.relative_to(generation).as_posix()
        validate_relative_path(name)
        # Windowsでも解釈が一意になる名前に限定する。
        absolute_path("C:/snapshot/" + name)
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                "UNSUPPORTED_FILE", "リンク・特殊ファイルは保存できません")
        require(path.suffix.lower() not in {".db", ".sqlite", ".sqlite3", ".wal", ".shm"},
                "ROOT_MISMATCH", "DB adapterが未定義です")
        if name.startswith("config/"):
            relative = path.relative_to(generation / "config/SushiEricServerMod") if name.startswith("config/SushiEricServerMod/") else None
            require(relative is not None and (relative.parts[0] in DATA_DIRECTORIES
                    or relative.as_posix() in DATA_FILES | {"config.yml", "management/config.yml"}),
                    "SETTINGS_UNSUPPORTED", "configに未分類の設定があります")
        data = regular_bytes(path)
        require(not data.startswith((b"SQLite format 3\x00", b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83",
                                    b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7")),
                "ROOT_MISMATCH", "SQLite adapterが未定義です")
        entries.append({"path": name, "kind": "regular", "size": len(data), "sha256": digest(data)})
    return layout, entries


def entry(name: str, data: bytes) -> dict:
    return {"path": name, "kind": "regular", "size": len(data), "sha256": digest(data)}

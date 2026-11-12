"""固定DataModel Host配布をbundleへ収録する。稼働管理DBへの直接write入口は作らない。"""
import io
import json
import os
import platform
from pathlib import Path
import re
import stat
import xml.etree.ElementTree as ET
import zipfile

from .build_inputs import canonical, digest, regular_bytes
from .contracts import ContractError, validate_relative_path

HOST_MAIN = "io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterCli"
HOST_MODULE = "io.github.sushiericworkspace:sushieric-datamodel-host-adapter-dev"
DESCRIPTOR = "adapter/host.json"


def _require(condition, message):
    if not condition:
        raise ContractError("adapter: " + message)


def _check_jar(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as jar:
            manifests = [entry for entry in jar.infolist()
                         if entry.filename.upper() == "META-INF/MANIFEST.MF"]
            _require(len(manifests) <= 1, "JAR manifestが重複しています")
            if not manifests:
                return
            lines = []
            for line in jar.read(manifests[0]).splitlines():
                if line.startswith(b" "):
                    _require(bool(lines), "JAR manifestの継続行が不正です")
                    lines[-1] += line[1:]
                else:
                    lines.append(line)
            for line in lines:
                key, separator, value = line.partition(b":")
                _require(not (separator and key.lower() == b"class-path" and value.strip()),
                         "JAR manifestの追加classpathは使用できません")
    except zipfile.BadZipFile as error:
        raise ContractError("adapter: runtime JAR形式が不正です") from error


def _archive(data):
    entries = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for entry in archive.infolist():
            if entry.is_dir():
                _require(entry.filename == "lib/", "未知の配布directoryです")
                continue
            name = entry.filename
            validate_relative_path(name)
            _require(not stat.S_ISLNK(entry.external_attr >> 16), "配布内リンクは使用できません")
            _require(name in {"README.md", "world-run-state.md", "world-import-candidate.md"} or
                     re.fullmatch(r"lib/[^/]+\.jar", name) is not None, "未知の配布fileです")
            _require(";" not in name, "配布名にclasspath区切り文字は使用できません")
            _require(name not in entries, "配布fileが重複しています")
            content = archive.read(entry)
            if name.startswith("lib/"):
                _check_jar(content)
            entries[name] = content
    _require("README.md" in entries and any(name.startswith("lib/") for name in entries),
             "READMEとruntime JARが必要です")
    return entries


def _pom(data, coordinate):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ContractError("adapter: POM形式が不正です") from error
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    gav = [root.findtext("m:" + key, namespaces=ns) for key in ("groupId", "artifactId", "version")]
    _require(all(type(value) is str for value in gav) and ":".join(gav) == coordinate, "POMと完全座標が一致しません")


def freeze_host_adapter(freezer, lock, data_model_coordinate, data_model_jar):
    """同じDataModel発行のZIP/POMと全展開byteを固定する。"""
    version = data_model_coordinate.split(":")[2]
    coordinate = HOST_MODULE + ":" + version
    publications = [item for item in lock.get("publications", []) if
                    item["coordinate"].startswith(HOST_MODULE + ":")]
    _require(len(publications) == 2 and all(item["coordinate"] == coordinate for item in publications),
             "同じDataModel版のHost ZIP/POM発行lockが必要です")
    module = HOST_MODULE.split(":")[1]
    expected = {f"{module}-{version}-runtime.zip", f"{module}-{version}.pom"}
    _require({Path(item["path"]).name for item in publications} == expected,
             "Host ZIP/POMの実体が一致しません")
    paths = {}
    for item in publications:
        path = Path(item["path"])
        data = freezer.read(path)
        _require(digest(data) == item["sha256"], "発行lockから配布が変わりました")
        paths[path.suffix] = path
    _pom(freezer.read(paths[".pom"]), coordinate)
    entries = _archive(freezer.read(paths[".zip"]))
    model_hash = digest(freezer.read(data_model_jar))
    own = [name for name, data in entries.items() if name.startswith("lib/") and digest(data) == model_hash]
    _require(len(own) == 1, "配布のDataModel JARとMod固定JARが一致しません")
    with zipfile.ZipFile(io.BytesIO(entries[own[0]])) as jar:
        _require(HOST_MAIN.replace(".", "/") + ".class" in jar.namelist(), "公開Host entrypointがありません")
    files = []
    for name, data in sorted(entries.items()):
        target = "adapter/host/" + name
        freezer.write(target, data)
        files.append({"path": target, "kind": "regular", "size": len(data), "sha256": digest(data)})
    descriptor = {"version": 1, "coordinate": coordinate, "dataModelCoordinate": data_model_coordinate,
                  "archive": freezer.copy(paths[".zip"]), "pom": freezer.copy(paths[".pom"]),
                  "platform": platform.system(), "machine": platform.machine(), "mainClass": HOST_MAIN,
                  "classpath": ["adapter/host/" + own[0]] + sorted("adapter/host/" + name for name in entries if name.startswith("lib/") and name != own[0]),
                  "dataModelJar": "adapter/host/" + own[0], "files": files}
    freezer.write(DESCRIPTOR, canonical(descriptor))


def verify_host_adapter(root):
    """全bundle検証後の固定配布を照合する。未同梱bundleに代替JARを探索しない。"""
    descriptor = json.loads(regular_bytes(root / DESCRIPTOR))
    _require(type(descriptor) is dict and descriptor.keys() == {
        "version", "coordinate", "dataModelCoordinate", "archive", "pom", "platform", "machine",
        "mainClass", "classpath", "dataModelJar", "files"}, "未知の配布契約です")
    _require(type(descriptor["version"]) is int and descriptor["version"] == 1 and
             descriptor["mainClass"] == HOST_MAIN, "未知のentrypointです")
    runtime = json.loads(regular_bytes(root / "runtime.json"))
    _require(descriptor["dataModelCoordinate"] == runtime["dataModelCoordinate"], "DataModel座標が異なります")
    _require(descriptor["coordinate"] == HOST_MODULE + ":" + runtime["dataModelCoordinate"].split(":")[2],
             "Host座標が異なります")
    for name in ("archive", "pom", "dataModelJar"):
        _require(type(descriptor[name]) is str, "配布pathが不正です")
        validate_relative_path(descriptor[name])
    _pom(regular_bytes(root / descriptor["pom"]), descriptor["coordinate"])
    entries = _archive(regular_bytes(root / descriptor["archive"]))
    expected = [{"path": "adapter/host/" + name, "kind": "regular", "size": len(data), "sha256": digest(data)}
                for name, data in sorted(entries.items())]
    _require(descriptor["files"] == expected, "配布file集合・hashが一致しません")
    libraries = sorted("adapter/host/" + name for name in entries if name.startswith("lib/"))
    _require(descriptor["dataModelJar"] in libraries, "DataModel JARが配布にありません")
    _require(descriptor["classpath"] == [descriptor["dataModelJar"]] + [name for name in libraries if name != descriptor["dataModelJar"]],
             "配布classpathが一致しません")
    for entry in expected:
        _require(digest(regular_bytes(root / entry["path"])) == entry["sha256"], "展開fileが一致しません")
    dependencies = json.loads(regular_bytes(root / "inputs/dependencies.json"))
    model = [entry for entry in dependencies if entry["coordinate"] == runtime["dataModelCoordinate"]
             and entry["file"].endswith(".jar")]
    _require(len(model) == 1, "固定DataModel JARが一意ではありません")
    validate_relative_path(model[0]["file"])
    _require(descriptor["dataModelJar"] in descriptor["classpath"] and
             regular_bytes(root / descriptor["dataModelJar"]) == regular_bytes(root / model[0]["file"]),
             "ModとHostのDataModel JARが異なります")
    _require(type(descriptor["platform"]) is str and bool(descriptor["platform"]) and
             type(descriptor["machine"]) is str and bool(descriptor["machine"]), "作成OSがありません")
    return descriptor


def host_adapter_command(root: Path):
    """固定bundleのJDK/公開CLIだけを選ぶ。stdin本文・world/shopはこの関数で追加しない。"""
    from .runtime_bundle import verify_bundle
    verify_bundle(root)
    descriptor = verify_host_adapter(root)
    _require(descriptor["platform"] == platform.system() and descriptor["machine"] == platform.machine(),
             "作成OS/アーキテクチャが異なります")
    java = root / "jdk/bin" / ("java.exe" if os.name == "nt" else "java")
    classpath = [str((root / name).absolute()) for name in descriptor["classpath"]]
    _require(all(os.pathsep not in name for name in classpath), "bundle pathにclasspath区切り文字は使用できません")
    return [str(java), "-cp", os.pathsep.join(classpath), HOST_MAIN]

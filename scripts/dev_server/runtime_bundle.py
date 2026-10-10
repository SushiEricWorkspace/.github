"""Loomの取得済みレシピを不変bundleへ変換する。世代・サーバーの管理は行わない。"""

import json
import os
import platform
import re
import shutil
import stat
import tempfile
from pathlib import Path

from .build_inputs import canonical, digest, portable_lock, regular_bytes, verify_sources
from .contracts import ContractError, LAYOUT_ID, validate, validate_relative_path


def _require(condition: bool, message: str):
    if not condition:
        raise ContractError(message)


def _directory(path: Path):
    """既存の親も含めてリンク・reparse pointを拒否する。"""
    for parent in [path, *path.parents]:
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            _require(stat.S_ISDIR(info.st_mode) and not
                     getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT,
                     "directory: 通常ディレクトリ以外は使用できません")


def _files(root: Path) -> list[dict]:
    entries = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        _directory(Path(directory))
        for name in dirs:
            _directory(Path(directory) / name)
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            validate_relative_path(relative)
            data = regular_bytes(path)
            entries.append({"path": relative, "kind": "regular", "size": len(data), "sha256": digest(data)})
    return sorted(entries, key=lambda entry: entry["path"])


def _assert_portable(value: str):
    # bundle/runのプレースホルダー以外に、絶対パスを隠した起動引数を許さない。
    remainder = value.replace("${bundle}", "BUNDLE").replace("${run}", "RUN")
    _require(not re.search(r"[A-Za-z]:[\\/]|(?:^|[=;: ])/", remainder),
             "launch: 未分類の外部パスが残っています")


def _artifact(recipe: dict, group: str, module: str, version: str) -> Path:
    candidates = []
    for item in recipe["artifacts"]:
        resolved_group, resolved_module, resolved_version = item["coordinate"].split(":")
        original = resolved_group == group and resolved_module == module
        remapped = resolved_group == "remapped." + group and re.fullmatch(re.escape(module) + r"-[0-9a-f]+", resolved_module)
        if (original or remapped) and resolved_version == version:
            candidates.append(Path(item["path"]))
    _require(len(candidates) == 1, f"tool: {module}の解決済み実体が一意ではありません")
    return candidates[0]


class Freezer:
    """コピー元の全入力hashを記録し、公開前に再照合する。"""

    def __init__(self, staging: Path):
        self.root = staging
        self.references = {}
        self.inputs = {}

    def write(self, name: str, data: bytes):
        validate_relative_path(name)
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(data)

    def read(self, path: Path) -> bytes:
        data = regular_bytes(path)
        _require(path not in self.inputs or self.inputs[path] == digest(data), "input: コピー中に変更されました")
        self.inputs[path] = digest(data)
        return data

    def copy(self, path: Path) -> str:
        path = path.absolute()
        if path in self.references:
            return self.references[path]
        if path.is_dir():
            _directory(path)
            entries = _files(path)
            # 空のクラスディレクトリもclasspathの順序を変えず記録する。
            fingerprint = digest(canonical(entries))
            name = f"classes/{fingerprint}"
            self.references[path] = name
            self.inputs[path] = entries
            if not (self.root / name).exists():
                if not entries:
                    self.write(name + "/.runtime-empty", b"")
                for entry in entries:
                    data = self.read(path / entry["path"])
                    _require(digest(data) == entry["sha256"], "input: ディレクトリがコピー中に変更されました")
                    self.write(name + "/" + entry["path"], data)
        else:
            data = self.read(path)
            name = f"files/{digest(data)}/{path.name}"
            validate_relative_path(name)
            if not (self.root / name).exists():
                self.write(name, data)
            self.references[path] = name
        return name

    def copy_tree(self, source: Path, prefix: str):
        _directory(source)
        entries = _files(source)
        self.inputs[source] = entries
        for entry in entries:
            path = source / entry["path"]
            data = self.read(path)
            _require(digest(data) == entry["sha256"], "tool: コピー中に変更されました")
            self.write(prefix + "/" + entry["path"], data)
            shutil.copymode(path, self.root / prefix / entry["path"])
        return digest(canonical(entries))

    def recheck(self):
        for path, expected in self.inputs.items():
            observed = _files(path) if isinstance(expected, list) else digest(regular_bytes(path))
            _require(observed == expected, "input: 出力の確定前に入力が変更されました")

    def config(self, path: Path, profile: str) -> str:
        section = None
        lines = []
        for line in self.read(path).decode("utf-8").splitlines():
            if line and not line.startswith(("\t", " ")):
                section = line
            if section not in {"commonProperties", "serverProperties", "serverArgs"}:
                # clientのassets参照はサーバーレシピでは読まれない。
                continue
            if line.startswith("\t") and "=" in line:
                key, value = line[1:].split("=", 1)
                if key == "log4j.configurationFile":
                    value = "${bundle}/" + self.copy(Path(value))
                elif key == "fabric.remapClasspathFile":
                    paths = self.read(Path(value)).decode("utf-8").strip().split(os.pathsep)
                    remapped = os.pathsep.join("${bundle}/" + self.copy(Path(item)) for item in paths)
                    name = f"launch/{profile}-remap-classpath.txt"
                    self.write(name, remapped.encode())
                    value = f"${{run}}/.launch/{profile}-remap-classpath.txt"
                elif key == "fabric.classPathGroups":
                    groups = []
                    for group in value.split(os.pathsep * 2):
                        groups.append(os.pathsep.join("${bundle}/" + self.copy(Path(item))
                                                     for item in group.split(os.pathsep) if item))
                    value = (os.pathsep * 2).join(groups)
                _assert_portable(value)
                line = f"\t{key}={value}"
            lines.append(line)
        _require("\tfabric.development=true" in lines, "launch: 開発環境ではありません")
        name = f"launch/{profile}-launch.cfg"
        self.write(name, ("\n".join(lines) + "\n").encode())
        return f"${{run}}/.launch/{profile}-launch.cfg"

    def profile(self, name: str, recipe: dict) -> dict:
        _require(recipe["environment"] == {}, "launch: 明示環境変数のadapterが未定義です")
        classpath = [self.copy(Path(path)) for path in recipe["classpath"]]
        jvm = []
        config_count = 0
        for argument in recipe["jvmArgs"]:
            if argument.startswith("@"):
                # Java argfileのclasspathだけを展開。未知のargfileは救済しない。
                lines = self.read(Path(argument[1:])).decode("utf-8").splitlines()
                _require(len(lines) == 2 and lines[0] == "-classpath", "launch: 未知のLoom argfileです")
                # Javaの非引用引数にあるWindowsのbackslashはshell escapeではない。
                value = json.loads(lines[1]) if lines[1].startswith('"') else lines[1]
                _require([os.path.normcase(os.path.normpath(item)) for item in value.split(os.pathsep)] ==
                         [os.path.normcase(os.path.normpath(item)) for item in recipe["classpath"]],
                         "launch: argfileとJavaExecのclasspathが一致しません")
                continue
            if argument.startswith("-Dfabric.dli.config="):
                argument = "-Dfabric.dli.config=" + self.config(Path(argument.split("=", 1)[1]), name)
                config_count += 1
            elif argument.startswith("-Dfabric-api.gametest.report-file="):
                argument = "-Dfabric-api.gametest.report-file=${run}/report.xml"
            _assert_portable(argument)
            jvm.append(argument)
        _require(config_count == 1, "launch: Loomの設定ファイルが一意ではありません")
        for argument in recipe["args"]:
            _assert_portable(argument)
        if name == "gameTest":
            _require("-Dsushieric.management.disabled=true" in jvm, "launch: GameTestのManagement APIは禁止です")
        return {"mainClass": recipe["mainClass"], "classpath": classpath, "jvmArgs": jvm, "args": recipe["args"]}


def freeze(recipe_path: Path, source_lock_path: Path, store: Path) -> Path:
    """完成したbundleだけをhash名で公開する。失敗stagingは診断用に残す。"""
    recipe = json.loads(regular_bytes(recipe_path))
    lock = json.loads(regular_bytes(source_lock_path))
    _require(recipe["recipeVersion"] == 1 and recipe["development"] is True, "recipe: 未知の起動レシピです")
    _require(recipe["javaVersion"].startswith("21."), "jdk: JDK 21が必要です")
    verify_sources(lock)
    store = store.absolute()
    _directory(store)
    _require(not any(store == Path(source["root"]) or Path(source["root"]) in store.parents
                     for source in lock["sources"]), "store: worktreeの外へ保存してください")
    store.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=store))
    freezer = Freezer(staging)
    _require(bool(recipe.get("dependencyInputs")), "recipe: コンパイル前の依存lockがありません")
    for entry in recipe["dependencyInputs"]:
        _require(digest(freezer.read(Path(entry["path"]))) == entry["sha256"],
                 "dependency: コンパイル前から依存・ツールが変更されています")
    profiles = {name: freezer.profile(name, recipe["profiles"][name]) for name in ("server", "gameTest")}
    freezer.write("launch/profiles.json", canonical({"version": 1, "platform": platform.system(),
                                                   "machine": platform.machine(), "profiles": profiles}))
    freezer.write("inputs/source-lock.json", canonical(portable_lock(lock)))
    # 未追跡の実入力も保存して、hashだけでは再現不能にならないようにする。
    for source in lock["sources"]:
        for entry in source["record"]["untrackedInputs"]:
            data = freezer.read(Path(source["root"]) / entry["path"])
            _require(digest(data) == entry["sha256"], "source: 未追跡入力が変更されています")
            freezer.write(f"inputs/{source['record']['repo']}/{entry['path']}", data)
    dependencies = []
    for artifact in recipe["artifacts"] + recipe["plugins"]:
        path = Path(artifact["path"])
        dependencies.append({"coordinate": artifact["coordinate"], "file": freezer.copy(path)})
        # 公開したCommonのPOM/JARは双方必須。動的metadataでは代用しない。
        if artifact["coordinate"] == recipe["commonCoordinate"]:
            pom = path.with_suffix(".pom")
            dependencies.append({"coordinate": artifact["coordinate"], "file": freezer.copy(pom)})
    freezer.write("inputs/dependencies.json", canonical(dependencies))
    publications = []
    for item in lock.get("publications", []):
        path = Path(item["path"])
        _require(digest(freezer.read(path)) == item["sha256"], "publication: 発行直後から変更されています")
        publications.append({"coordinate": item["coordinate"], "sha256": item["sha256"], "file": freezer.copy(path)})
    freezer.write("inputs/publications.json", canonical(publications))
    tools = [{"name": "jdk", "version": recipe["javaVersion"],
              "sha256": freezer.copy_tree(Path(recipe["javaHome"]), "jdk")},
             {"name": "gradle", "version": recipe["gradleVersion"],
              "sha256": freezer.copy_tree(Path(recipe["gradleHome"]) / "lib", "tools/gradle")}]
    _require(recipe["loomJar"], "loom: 解決済みpluginがありません")
    loom_path = recipe["loomJar"]
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", loom_path):
        loom_path = loom_path[1:]
    loom = Path(loom_path)
    loom_version = re.search(r"fabric-loom-([0-9.]+)\.jar$", loom.name)
    _require(loom_version is not None, "loom: 実体のバージョンを取得できません")
    tool_files = {"loom": (loom, loom_version.group(1)),
                  "minecraft": (next(Path(item["path"]) for item in recipe["artifacts"]
                                     if item["coordinate"].startswith("net.minecraft:")), recipe["minecraftVersion"]),
                  "yarn": (next(Path(item["path"]) for item in recipe["artifacts"]
                                if item["coordinate"].startswith("net.fabricmc:yarn:")), recipe["yarnVersion"])}
    for name, group, module, version in (("fabric-loader", "net.fabricmc", "fabric-loader", recipe["loaderVersion"]),
                                         ("fabric-api", "net.fabricmc.fabric-api", "fabric-api", recipe["fabricApiVersion"]),
                                         ("kotlin", "org.jetbrains.kotlin", "kotlin-stdlib", recipe["kotlinVersion"])):
        tool_files[name] = (_artifact(recipe, group, module, version), version)
    for name, (path, version) in tool_files.items():
        freezer.copy(path)
        tools.append({"name": name, "version": version, "sha256": digest(freezer.read(path))})
    freezer.recheck()
    verify_sources(lock)
    manifest = {"schemaVersion": 1, "artifactId": "0" * 64, "layoutId": LAYOUT_ID, "development": True,
                "javaVersion": recipe["javaVersion"], "commonCoordinate": recipe["commonCoordinate"],
                "files": _files(staging), "sources": [source["record"] for source in lock["sources"]],
                "tools": tools, "launch": {key: profiles["server"][key] for key in ("mainClass", "classpath", "jvmArgs")}}
    manifest["artifactId"] = digest(canonical({key: value for key, value in manifest.items() if key != "artifactId"}))
    validate("runtime", manifest)
    freezer.write("runtime.json", canonical(manifest))
    freezer.write("COMPLETE", manifest["artifactId"].encode())
    verify_bundle(staging)
    destination = store / manifest["artifactId"]
    _require(not destination.exists(), "store: 同じartifactを上書きしません")
    staging.rename(destination)
    return destination


def verify_bundle(root: Path) -> dict:
    """起動前にbundle全体を検査する。Gradle・worktreeは参照しない。"""
    _directory(root)
    manifest = json.loads(regular_bytes(root / "runtime.json"))
    validate("runtime", manifest)
    expected_id = digest(canonical({key: value for key, value in manifest.items() if key != "artifactId"}))
    _require(manifest["artifactId"] == expected_id and regular_bytes(root / "COMPLETE").decode() == expected_id,
             "runtime: artifactIdまたはCOMPLETEが一致しません")
    observed = [entry for entry in _files(root) if entry["path"] not in {"runtime.json", "COMPLETE"}]
    _require(observed == manifest["files"], "runtime: ファイル集合・サイズ・hashが一致しません")
    return manifest


def launch_command(root: Path, profile: str, run: Path) -> list[str]:
    """検証済みbundleからコマンドを組む。JVMの開始・世代操作は呼出側の責務。"""
    verify_bundle(root)
    descriptor = json.loads(regular_bytes(root / "launch/profiles.json"))
    _require(descriptor["version"] == 1 and descriptor["platform"] == platform.system()
             and descriptor["machine"] == platform.machine(), "launch: 作成OS/アーキテクチャと一致しません")
    recipe = descriptor["profiles"][profile]
    _directory(run)
    _require(root != run and root not in run.parents and run not in root.parents, "launch: bundleと実行データを分離してください")
    auxiliary = run / ".launch"
    # 古い起動設定を上書きしない。ハーネスがrun-idごとの新規ディレクトリを用意する。
    auxiliary.mkdir(parents=True, exist_ok=False)
    replace = lambda value: value.replace("${bundle}", str(root.absolute())).replace("${run}", str(run.absolute()))
    for suffix in ("launch.cfg", "remap-classpath.txt"):
        source = root / f"launch/{profile}-{suffix}"
        data = replace(regular_bytes(source).decode())
        (auxiliary / f"{profile}-{suffix}").write_text(data, encoding="utf-8")
    java = root / "jdk/bin" / ("java.exe" if os.name == "nt" else "java")
    arguments = [*map(replace, recipe["jvmArgs"]), "-cp",
                 os.pathsep.join(str(root / entry) for entry in recipe["classpath"]), recipe["mainClass"], *recipe["args"]]
    # Windowsのコマンド長上限を避ける。Javaの引用内ではbackslashをescapeする。
    argfile = auxiliary / "java.args"
    argfile.write_text("\n".join(json.dumps(arg, ensure_ascii=False) for arg in arguments) + "\n", encoding="utf-8")
    return [str(java), "@" + str(argfile)]

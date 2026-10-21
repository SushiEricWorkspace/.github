"""Loomの取得済みレシピを不変bundleへ変換する。世代・サーバーの管理は行わない。"""

import json
import os
import platform
import re
import shutil
import stat
import tempfile
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath

from .build_inputs import canonical, digest, portable_lock, regular_bytes, verify_sources
from .contracts import ContractError, LAYOUT_ID, validate, validate_relative_path


def _require(condition: bool, message: str):
    if not condition:
        raise ContractError(message)


def require_data_generation(manifest: dict, generation: str) -> None:
    """verify_bundle済みDataModel runtime（v2）の管理世代だけをcanonical UUIDへ限定する。

    旧Common runtime（v1）やtemplateId等の名前は対象にせず、既存値を変換しない。
    """
    if manifest["schemaVersion"] != 2:
        return
    message = "DataModel runtimeの管理世代はcanonical UUIDで指定してください"
    _require(type(generation) is str, message)
    try:
        canonical = str(uuid.UUID(generation))
    except ValueError:
        raise ContractError(message) from None
    _require(canonical == generation, message)


def _managed_argfile(arguments: list[str], encoding: str) -> bytes:
    codec = {"utf-8": "utf-8", "windows-31j": "cp932"}.get(encoding)
    _require(codec is not None, "managed launch: 未対応のencodingです")
    lines = []
    for argument in arguments:
        _require(type(argument) is str and argument and not any((ord(c) < 32 or ord(c) == 127) for c in argument),
                 "managed launch: 引数が不正です")
        try:
            raw = argument.encode(codec)
            _require(raw.decode(codec) == argument, "managed launch: encodingが往復一致しません")
        except UnicodeError:
            raise ContractError("managed launch: 引数をencodingで表現できません") from None
        lines.append(json.dumps(raw.decode("latin-1"), ensure_ascii=False).encode("latin-1"))
    return b"\n".join(lines) + b"\n"


def validate_managed_launch(profile: dict, context: dict, observed: dict) -> None:
    """承認済みM/server templateとP正本値から展開済み起動入力の完全一致を純粋に検査する。

    file/OS/環境を読み取らず、引数も変更しない。不一致はContractError。
    profile承認・native本人・実起動の検証ではなく、通常launchには未接続。
    """
    def fields(value, names):
        _require(type(value) is dict and value.keys() == set(names), "managed launch: field集合が不正です")

    def text(value, multiline=False):
        _require(type(value) is str and bool(value) and not any(
            (ord(c) < 32 or ord(c) == 127) and not (multiline and c in "\n\t") for c in value), "managed launch: textが不正です")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ContractError("managed launch: UTF-8 textが不正です") from None

    def expand(value, allowed):
        text(value, multiline=True)
        tokens = re.findall(r"\$\{([^{}]+)\}", value)
        _require(all(name in allowed for name in tokens), "managed launch: tokenのslotが不正です")
        _require("${" not in re.sub(r"\$\{[^{}]+\}", "", value), "managed launch: token構文が不正です")
        return re.sub(r"\$\{([^{}]+)\}", lambda match: context[match[1]], value)

    fields(profile, ("version", "purpose", "role", "platform", "argfileEncoding", "mainClass", "classpath",
                     "jvmArgs", "args", "environment", "dliText", "remapClasspath"))
    _require(type(profile["version"]) is int and profile["version"] == 1
             and profile["purpose"] == "managed-launch-exact" and profile["role"] == "M",
             "managed launch: version/purpose/roleが不正です")
    _require(type(profile["platform"]) is str and profile["platform"] in ("Windows", "Darwin"),
             "managed launch: OSが不正です")
    _require(type(profile["argfileEncoding"]) is str and profile["argfileEncoding"] in
             (("utf-8", "windows-31j") if profile["platform"] == "Windows" else ("utf-8",)),
             "managed launch: encodingがOSと一致しません")
    paths = ("bundle", "run", "auxiliary", "privateTmp", "controlDescriptor", "runtimeManifest", "proofPath")
    fields(context, (*paths, "instanceId", "generationId", "startNonce", "controlDescriptorSha256"))
    path_type = PureWindowsPath if profile["platform"] == "Windows" else PurePosixPath
    for name, value in context.items():
        text(value)
        _require("${" not in value, "managed launch: context内のtokenは禁止です")
        if name in paths:
            _require(path_type(value).is_absolute(), "managed launch: 絶対pathが必要です")
    for name in ("instanceId", "startNonce"):
        _require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", context[name]) is not None,
                 "managed launch: 識別子が不正です")
    require_data_generation({"schemaVersion": 2}, context["generationId"])
    _require(re.fullmatch(r"[0-9a-f]{64}", context["controlDescriptorSha256"]) is not None,
             "managed launch: descriptor hashが不正です")
    fields(observed, ("argv", "cwd", "environment", "auxiliaryFiles"))
    text(observed["cwd"])
    _require(path_type(observed["cwd"]).is_absolute(), "managed launch: cwdが不正です")

    def environment(value):
        _require(type(value) is dict, "managed launch: envはmappingが必要です")
        keys = set()
        for key, item in value.items():
            text(key)
            text(item)
            _require(key.isascii() and "=" not in key and "${" not in key, "managed launch: env keyが不正です")
            comparison = key.casefold() if profile["platform"] == "Windows" else key
            _require(comparison not in keys, "managed launch: env keyが重複しています")
            keys.add(comparison)

    environment(profile["environment"])
    environment(observed["environment"])
    expected_env = {key: expand(value, {"privateTmp"}) for key, value in profile["environment"].items()}
    text(profile["mainClass"])
    expand(profile["mainClass"], set())
    for name in ("jvmArgs", "args", "classpath", "remapClasspath"):
        _require(type(profile[name]) is list, "managed launch: 引数/pathはlistが必要です")
        for value in profile[name]:
            text(value)
    for name in ("classpath", "remapClasspath"):
        _require(bool(profile[name]) and len(profile[name]) == len(set(profile[name])),
                 "managed launch: classpathが空又は重複しています")
        for value in profile[name]:
            validate_relative_path(value)
            expand(value, set())
    for value in profile["args"]:
        expand(value, set())
        _require(not value.startswith("@"), "managed launch: 追加argfileは禁止です")
    properties = {"sushieric.managed." + name: name for name in
                  ("instanceId", "generationId", "startNonce", "proofPath", "runtimeManifest",
                   "controlDescriptor", "controlDescriptorSha256")}
    properties.update({"java.io.tmpdir": "privateTmp", "fabric.dli.config": "auxiliary"})
    expected_properties = {key: "${" + name + "}" for key, name in properties.items()}
    expected_properties["fabric.dli.config"] += "/server-launch.cfg"
    flags, actual_properties, jvm = set(), {}, []
    for argument in profile["jvmArgs"]:
        _require(not argument.startswith("@") and argument not in ("-cp", "-classpath", "--class-path")
                 and not argument.startswith("--class-path="), "managed launch: classpath/argfile混入は禁止です")
        allowed = set()
        if argument.startswith("-D"):
            key, separator, value = argument[2:].partition("=")
            _require(bool(key) and bool(separator), "managed launch: propertyが不正です")
            flag = "property:" + key
            actual_properties[key] = value
            if key in properties:
                allowed.add(properties[key])
            elif key == "org.sqlite.tmpdir":
                _require(value == "${privateTmp}", "managed launch: SQLite temp slotが不正です")
                allowed.add("privateTmp")
        elif argument.startswith(("-Xms", "-Xmx")):
            flag = argument[:4]
        elif argument.startswith("-XX:"):
            flag = "XX:" + argument[4:].lstrip("+-").split("=", 1)[0]
        else:
            flag = argument
        _require(flag not in flags, "managed launch: JVM flag/propertyが重複しています")
        flags.add(flag)
        jvm.append(expand(argument, allowed))
    _require(all(actual_properties.get(key) == value for key, value in expected_properties.items()),
             "managed launch: 必須propertyのtokenが一致しません")
    _require("-XX:+DisableAttachMechanism" in profile["jvmArgs"], "managed launch: attach禁止が必要です")
    text(profile["dliText"], multiline=True)
    _require("\ufeff" not in profile["dliText"], "managed launch: DLIのBOMは禁止です")
    dli = expand(profile["dliText"], {"bundle", "auxiliary"})
    if profile["platform"] == "Windows":
        dli = dli.replace("\n", "\r\n")
    separator = ";" if profile["platform"] == "Windows" else ":"
    bundle = path_type(context["bundle"])
    classpath = separator.join(str(bundle / value) for value in profile["classpath"])
    remap = separator.join(str(bundle / value) for value in profile["remapClasspath"])
    arguments = [*jvm, "-cp", classpath, profile["mainClass"], *profile["args"]]
    expected_files = {"java.args": _managed_argfile(arguments, profile["argfileEncoding"]),
                      "server-launch.cfg": dli.encode("utf-8"),
                      "server-remap-classpath.txt": remap.encode("utf-8")}
    fields(observed["auxiliaryFiles"], expected_files)
    _require(all(type(value) is bytes for value in observed["auxiliaryFiles"].values()),
             "managed launch: 補助fileはbytesが必要です")
    _require(type(observed["argv"]) is list, "managed launch: argvはlistが必要です")
    for argument in observed["argv"]:
        text(argument)
        expand(argument, set())
    java = bundle / "jdk/bin" / ("java.exe" if profile["platform"] == "Windows" else "java")
    expected_argv = [str(java), "@" + str(path_type(context["auxiliary"]) / "java.args")]
    _require(observed["argv"] == expected_argv and observed["cwd"] == context["run"]
             and observed["environment"] == expected_env and observed["auxiliaryFiles"] == expected_files,
             "managed launch: 展開後の起動入力が一致しません")


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
    def fail(error):
        raise error
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=fail):
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
    _require(type(recipe.get("recipeVersion")) is int and recipe["recipeVersion"] == 2
             and recipe["development"] is True and "dataModelCoordinate" in recipe
             and "commonCoordinate" not in recipe, "recipe: 未知の起動レシピです")
    _require(recipe["javaVersion"].startswith("21."), "jdk: JDK 21が必要です")
    verify_sources(lock)
    data_model = [item for item in recipe["artifacts"] if item["coordinate"] == recipe["dataModelCoordinate"]]
    _require(len(data_model) == 1, "recipe: DataModelの解決済みJARが一意ではありません")
    data_model_jar = Path(data_model[0]["path"])
    dependency_paths = {entry["path"] for entry in recipe.get("dependencyInputs", [])}
    _require({str(data_model_jar), str(data_model_jar.with_suffix(".pom"))} <= dependency_paths,
             "recipe: DataModelのJAR/POMのコンパイル前lockが必要です")
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
        # 公開したSushiEricDataModelのPOM/JARは双方必須。動的metadataでは代用しない。
        if artifact["coordinate"] == recipe["dataModelCoordinate"]:
            pom = path.with_suffix(".pom")
            dependencies.append({"coordinate": artifact["coordinate"], "file": freezer.copy(pom)})
    freezer.write("inputs/dependencies.json", canonical(dependencies))
    publications = []
    for item in lock.get("publications", []):
        path = Path(item["path"])
        _require(digest(freezer.read(path)) == item["sha256"], "publication: 発行直後から変更されています")
        publications.append({"coordinate": item["coordinate"], "sha256": item["sha256"], "file": freezer.copy(path)})
    freezer.write("inputs/publications.json", canonical(publications))
    from .database_adapter import freeze_host_adapter
    freeze_host_adapter(freezer, lock, recipe["dataModelCoordinate"], data_model_jar)
    from .database_proxy_bundle import freeze_proxy
    freeze_proxy(freezer, lock, recipe, profiles)
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
    manifest = {"schemaVersion": 2, "artifactId": "0" * 64, "layoutId": LAYOUT_ID, "development": True,
                "javaVersion": recipe["javaVersion"], "dataModelCoordinate": recipe["dataModelCoordinate"],
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
    from .database_adapter import DESCRIPTOR, verify_host_adapter
    if (root / DESCRIPTOR).exists():
        verify_host_adapter(root)
    from .database_proxy_bundle import DESCRIPTOR as PROXY_DESCRIPTOR, DIRECTORY as PROXY_DIRECTORY, verify_proxy
    if (root / PROXY_DIRECTORY).exists() or (root / PROXY_DESCRIPTOR).exists():
        verify_proxy(root)
    return manifest


def launch_command(root: Path, profile: str, run: Path, *, auxiliary: Path | None = None,
                   jvm_properties: dict[str, str] | None = None) -> list[str]:
    """補助設定だけを新規auxiliaryへ出力し、runのcwd/rootは変更しない。

    管理propertyは全5項目または未指定。JVMの開始・世代操作は呼出側の責務。
    """
    verify_bundle(root)
    descriptor = json.loads(regular_bytes(root / "launch/profiles.json"))
    _require(descriptor["version"] == 1 and descriptor["platform"] == platform.system()
             and descriptor["machine"] == platform.machine(), "launch: 作成OS/アーキテクチャと一致しません")
    recipe = descriptor["profiles"][profile]
    _directory(run)
    _require(root != run and root not in run.parents, "launch: bundle内へ実行データを置けません")
    auxiliary = auxiliary if auxiliary is not None else run / ".launch"
    properties = jvm_properties if jvm_properties is not None else {}
    _require(not properties or properties.keys() == {"instanceId", "generationId", "startNonce", "proofPath", "runtimeManifest"},
             "launch: 管理propertyは全項目が必要です")
    _require(all(type(value) is str and value for value in properties.values()), "launch: 管理propertyが不正です")
    _require(auxiliary.is_absolute() and not auxiliary.exists(), "launch: 新しい絶対補助pathが必要です")
    _require(root != auxiliary and root not in auxiliary.parents, "launch: bundleへ起動補助を書き込めません")
    # 古い起動設定を上書きしない。ハーネスがrun-idごとの新規ディレクトリを用意する。
    auxiliary.mkdir(parents=True, exist_ok=False)
    replace = lambda value: value.replace("${run}/.launch", str(auxiliary)).replace("${bundle}", str(root.absolute())).replace("${run}", str(run.absolute()))
    for suffix in ("launch.cfg", "remap-classpath.txt"):
        source = root / f"launch/{profile}-{suffix}"
        data = replace(regular_bytes(source).decode())
        (auxiliary / f"{profile}-{suffix}").write_text(data, encoding="utf-8")
    java = root / "jdk/bin" / ("java.exe" if os.name == "nt" else "java")
    additions = ["-Dsushieric.managed." + key + "=" + value for key, value in properties.items()]
    arguments = [*additions, *map(replace, recipe["jvmArgs"]), "-cp",
                 os.pathsep.join(str(root / entry) for entry in recipe["classpath"]), recipe["mainClass"], *recipe["args"]]
    # Windowsのコマンド長上限を避ける。Javaの引用内ではbackslashをescapeする。
    argfile = auxiliary / "java.args"
    encoding = "mbcs" if os.name == "nt" else "utf-8"
    # Javaのbyte単位lexerに合わせ、DBCSの末尾backslashも引用してから書き込む。
    quoted = [json.dumps(arg.encode(encoding).decode("latin-1"), ensure_ascii=False).encode("latin-1")
              for arg in arguments]
    argfile.write_bytes(b"\n".join(quoted) + b"\n")
    return [str(java), "@" + str(argfile)]

"""Release原本とタグで正式ライブラリの由来を固定する。"""

from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess

from .build_inputs import digest, regular_bytes
from .contracts import ContractError, validate_relative_path
from .database_adapter import _archive, _pom

GROUP = "io.github.sushiericworkspace"
MODULES = ("sushieric-datamodel", "sushieric-datamodel-host-adapter", "sushieric-combat-core")
SEMVER = (r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
          r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
          r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")


def require(condition, message):
    if not condition:
        raise ContractError("release: " + message)


def selection(model_version, core_version):
    """監査可能な正式版から3ライブラリの固定GAVを作る。"""
    require(all(isinstance(value, str) and re.fullmatch(SEMVER, value)
                and "SNAPSHOT" not in value.upper() for value in (model_version, core_version)),
            "正式SemVerを指定してください")
    require(model_version != "0.2.0" and core_version != "0.1.0", "旧版は正式監査の対象外です")
    return dict(mode="release", dataModelCoordinate=f"{GROUP}:{MODULES[0]}:{model_version}",
                hostAdapterCoordinate=f"{GROUP}:{MODULES[1]}:{model_version}",
                combatCoreCoordinate=f"{GROUP}:{MODULES[2]}:{core_version}")


def expected_files(selected):
    result = []
    for name, extensions in (("dataModelCoordinate", (("jar", ""), ("pom", ""))),
                             ("hostAdapterCoordinate", (("zip", "runtime"), ("pom", ""))),
                             ("combatCoreCoordinate", (("jar", ""), ("pom", "")))):
        coordinate = selected[name]
        _, module, version = coordinate.split(":")
        for extension, classifier in extensions:
            filename = f"{module}-{version}{'-' + classifier if classifier else ''}.{extension}"
            result.append(dict(coordinate=coordinate, extension=extension, classifier=classifier, filename=filename))
    return result


def _gh(*args):
    process = subprocess.run(["gh", *args], capture_output=True)
    require(process.returncode == 0, "GitHubの読取りに失敗しました")
    return process.stdout


def release_original(repository, version):
    """Release添付原本bytesと、軽量/注釈付きタグの最終commitを取得する。"""
    tag = "v" + version
    release = json.loads(_gh("api", f"repos/{repository}/releases/tags/{tag}"))
    require(not release["draft"] and release["tag_name"] == tag, "公開Releaseが必要です")
    assets = [entry for entry in release["assets"] if entry["name"] == "input-lock.json"]
    require(len(assets) == 1, "Release添付原本が一意ではありません")
    original = _gh("api", f"repos/{repository}/releases/assets/{assets[0]['id']}",
                   "-H", "Accept: application/octet-stream")
    target = json.loads(_gh("api", f"repos/{repository}/git/ref/tags/{tag}"))["object"]
    seen = set()
    while target["type"] == "tag":
        require(target["sha"] not in seen, "タグ参照が循環しています")
        seen.add(target["sha"])
        target = json.loads(_gh("api", f"repos/{repository}/git/tags/{target['sha']}"))["object"]
    require(target["type"] == "commit", "タグの参照先がcommitではありません")
    return original, target["sha"]


def _safe_evidence(value):
    if isinstance(value, dict):
        for key, child in value.items():
            require(not re.fullmatch(r"authorization|[a-z0-9_-]*(?:token|password|secret)[a-z0-9_-]*", key, re.I),
                    "発行証跡に認証情報を含められません")
            _safe_evidence(key)
            _safe_evidence(child)
    elif isinstance(value, list):
        for child in value:
            _safe_evidence(child)
    elif isinstance(value, str):
        require(not re.search(r'https?://[^\s"/]*@|[a-z]:[\\/]Users[\\/]|/(?:Users|home)/', value, re.I),
                "発行証跡に認証情報やユーザー固有パスを含められません")


def verify_libraries(lock, root=None, *, online=False):
    """6成果物・証跡・POM・Hostを照合する。online時はRelease原本とタグも照合する。"""
    require(lock.get("sourceLockVersion") == 2, "source lock v2が必要です")
    selected = lock["librarySelection"]
    require(selected == selection(selected["dataModelCoordinate"].split(":")[-1],
                                  selected["combatCoreCoordinate"].split(":")[-1]), "選択GAVが不正です")
    expected = expected_files(selected)
    key = lambda entry: (entry["coordinate"], entry["extension"], entry["classifier"])
    publications = lock["publications"]
    path_field = "path" if root is None else "file"
    require(all(entry.keys() == {"coordinate", path_field, "sha256", "extension", "classifier", "origin"}
                for entry in publications), "成果物に未定義の欄があります")
    require(all(entry.keys() == {path_field, "sha256"} for entry in lock["publicationEvidence"]),
            "発行証跡に未定義の欄があります")
    require(len(publications) == 6 and {key(entry) for entry in publications} == {key(entry) for entry in expected},
            "選択した6成果物が必要です")

    def read(entry):
        name = entry["path" if root is None else "file"]
        if root is not None:
            validate_relative_path(name)
        else:
            require(Path(name).is_absolute(), "入力には絶対パスが必要です")
        data = regular_bytes(Path(name) if root is None else root / name)
        require(digest(data) == entry["sha256"], "固定入力のhashが一致しません")
        return data

    evidence = {}
    require(len(lock["publicationEvidence"]) == 2, "発行証跡原本2件が必要です")
    for entry in lock["publicationEvidence"]:
        data = read(entry)
        record = json.loads(data)
        _safe_evidence(record)
        require(entry["sha256"] not in evidence, "発行証跡が重複しています")
        require(re.fullmatch(r"[0-9a-f]{40}", record.get("commit", "")) is not None
                and record.get("workingTreeClean") is True
                and isinstance(record.get("remoteVerification", {}).get("result"), str)
                and bool(record["remoteVerification"]["result"]), "発行・取得後照合の記録が必要です")
        if online:
            require(record["repository"] in ("SushiEricWorkspace/SushiEricDataModel", "SushiEricWorkspace/SushiEricCombatCore"),
                    "発行元repositoryが不正です")
            original, commit = release_original(record["repository"], record["version"])
            require(data == original, "Release添付原本のbytesと一致しません")
            require(record["commit"] == commit, "発行元commitとタグ参照先が一致しません")
        evidence[entry["sha256"]] = record
    contents = {}
    for entry in publications:
        item = next(item for item in expected if key(item) == key(entry))
        data = read(entry)
        require(Path(entry["path" if root is None else "file"]).name == item["filename"], "成果物名が一致しません")
        origin = entry["origin"]
        repo = "SushiEricCombatCore" if entry["coordinate"] == selected["combatCoreCoordinate"] else "SushiEricDataModel"
        record = evidence.get(origin.get("evidenceSha256"))
        destination = f"https://maven.pkg.github.com/sushiericworkspace/{repo}"
        require(record is not None and origin == dict(mode="release", repository=destination,
                sourceRepository=f"SushiEricWorkspace/{repo}", sourceCommit=record["commit"],
                evidenceSha256=origin["evidenceSha256"]) and record["repository"] == origin["sourceRepository"]
                and record["destination"] == destination and record["version"] == entry["coordinate"].split(":")[2],
                "発行元・配布先・版が一致しません")
        notation = entry["coordinate"] + (f":{entry['classifier']}@{entry['extension']}" if entry["classifier"] else "")
        matches = [artifact for artifact in record["artifacts"] if artifact ==
                   dict(coordinate=notation, file=item["filename"], sha256=entry["sha256"])]
        require(len(matches) == 1, "発行原本のGAV/hashと一致しません")
        if entry["extension"] == "pom":
            _pom(data, entry["coordinate"])
        contents[key(entry)] = data
    from xml.etree import ElementTree as ET
    core = ET.fromstring(contents[(selected["combatCoreCoordinate"], "pom", "")])
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    dependencies = [entry for entry in core.findall("m:dependencies/m:dependency", ns)
                    if entry.findtext("m:groupId", namespaces=ns) == GROUP
                    and entry.findtext("m:artifactId", default="", namespaces=ns).startswith("sushieric-datamodel")]
    require(len(dependencies) == 1 and ":".join(dependencies[0].findtext("m:" + part, default="", namespaces=ns)
            for part in ("groupId", "artifactId", "version")) == selected["dataModelCoordinate"], "Core POMのDataModelが一致しません")
    archive = _archive(contents[(selected["hostAdapterCoordinate"], "zip", "runtime")])
    model = contents[(selected["dataModelCoordinate"], "jar", "")]
    require(sum(data == model for name, data in archive.items() if name.startswith("lib/")) == 1,
            "HostとModのDataModel JARが一致しません")
    origins = [entry["origin"] for entry in publications if entry["coordinate"] != selected["combatCoreCoordinate"]]
    require(all(origin == origins[0] for origin in origins), "DataModel/Hostの発行元が異なります")


def portable_publication(entry):
    result = {"sha256": entry["sha256"], "file": f"files/{entry['sha256']}/{Path(entry['path']).name}"}
    if "coordinate" in entry:
        result.update({key: entry[key] for key in ("coordinate", "extension", "classifier")})
        result["origin"] = {key: entry["origin"][key] for key in
                            ("mode", "repository", "sourceRepository", "sourceCommit", "evidenceSha256")}
    return result


def capture_release_inputs(lock, input_path, repository):
    """指定監査入力をRelease原本へ照合し、taskのUUID配下へ固定して返す。"""
    supplied = json.loads(regular_bytes(input_path))
    selected = supplied["librarySelection"]
    candidate = dict(lock, sourceLockVersion=2, librarySelection=selected,
                     publicationEvidence=supplied["publicationEvidence"], publications=supplied["publications"])
    verify_libraries(candidate, online=True)
    result = deepcopy(candidate)
    for index, entry in enumerate(result["publicationEvidence"]):
        target = repository / "evidence" / str(index) / "input-lock.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(regular_bytes(Path(entry["path"])))
        entry["path"] = str(target.absolute())
    for entry in result["publications"]:
        group, module, version = entry["coordinate"].split(":")
        target = repository / group.replace(".", "/") / module / version / Path(entry["path"]).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(regular_bytes(Path(entry["path"])))
        entry["path"] = str(target.absolute())
    verify_libraries(result, online=True)
    return result

"""承認済みOS別テンプレートを新しいビルドの初期データとして使う。"""
from contextlib import contextmanager
from pathlib import Path
import platform
import json

from .build_inputs import canonical, digest, regular_bytes
from .contracts import LAYOUT_ID
from .import_data import IMPORTED_TEMPLATES
from .registry import Registry, require
from .runtime_bundle import verify_bundle
from .template_control import TemplateControl


@contextmanager
def template_guard(registry, payload):
    """コピー元のGC・世代公開を待たずに排他する。同じrootでは呼出元の排他を使用する。"""
    from .supervisor import private_lock
    source = Path(payload["sourceRoot"])
    source_registry = registry if source == registry.root else Registry(source)
    require(source == registry.root or (not source.resolve().is_relative_to(registry.root.resolve())
            and not registry.root.resolve().is_relative_to(source.resolve())),
            "ROOT_MISMATCH", "管理rootを包含するコピー元は使用できません")
    if source == registry.root:
        yield
    else:
        with private_lock(source / "manager/locks/deployment.lock"):
            with source_registry.transaction(read_only=True) as connection:
                require(not connection.execute("SELECT 1 FROM runtime_operations WHERE command='gc-apply' AND status!='COMPLETED'").fetchone(),
                        "RECOVERY_REQUIRED", "コピー元のGCが未完了です")
            yield


def template_source(registry, payload):
    """昇格・検証・snapshot全hashを照合し、元artifactと不変参照を返す。"""
    require(IMPORTED_TEMPLATES.get(payload["templateId"]) == platform.system(),
            "TEMPLATE_MISMATCH", "このOSの登録済み初期ワールドを指定してください")
    source = Path(payload["sourceRoot"])
    source_registry = registry if source == registry.root else Registry(source)
    template = TemplateControl(source_registry).registered(payload["templateId"])
    manifest = source_registry.snapshot._registered(template["snapshotId"])
    runtime = verify_bundle(source / "artifacts" / template["artifactId"])
    return dict(manifest, templateHash=digest(canonical(template)), sourceRuntime=runtime)


def check_runtime(manifest, runtime, bundle):
    """同じMinecraft版・保存layoutだけを初期コピーする。Mod互換性の証明にはしない。"""
    descriptor = json.loads(regular_bytes(bundle / "launch/profiles.json"))
    require(descriptor["version"] == 1 and descriptor["platform"] == platform.system()
            and descriptor["machine"] == platform.machine(),
            "ARTIFACT_MISMATCH", "作成先artifactのOS・architectureが一致しません")
    source = manifest["sourceRuntime"]
    versions = lambda value: next(tool["version"] for tool in value["tools"] if tool["name"] == "minecraft")
    require(source["layoutId"] == runtime["layoutId"] == manifest["layout"]["layoutId"] == LAYOUT_ID
            and versions(source) == versions(runtime),
            "ARTIFACT_MISMATCH", "Minecraft版または保存layoutが異なる初期コピーは未対応です")

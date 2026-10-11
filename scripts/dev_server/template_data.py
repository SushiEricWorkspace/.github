"""私用snapshotを変更せず、共有候補のアカウントと可搬設定を正規化する。"""
import json

from .contracts import ROOT_FILES
from .registry import require
from .snapshot_data import portable_properties, numeric_yaml, CONFIG_KEYS


ACCOUNT_FILES = ROOT_FILES - {"server.properties"}


def shared_data(name: str, data: bytes) -> bytes:
    """共有候補では権限・許可・禁止リストを空にする。例外アカウントは初期版では扱わない。"""
    if name in ACCOUNT_FILES:
        require(type(json.loads(data)) is list, "SETTINGS_UNSUPPORTED", "アカウント一覧はJSON配列で指定してください")
        return b"[]\n"
    if name == "server.properties":
        return portable_properties(data)
    if name == "config/SushiEricServerMod/config.yml":
        return numeric_yaml(data, CONFIG_KEYS)
    if name == "config/SushiEricServerMod/management/config.yml":
        require(data == b"{}\n", "SETTINGS_UNSUPPORTED", "可搬management設定が正規化されていません")
    return data


def require_shared(manifest: dict, read) -> None:
    """候補の表示種別だけでなく、全設定とアカウント一覧が正規化済みであることを照合する。"""
    require(manifest["visibility"] == "shared-template", "TEMPLATE_MISMATCH", "共有候補snapshotが必要です")
    for file in manifest["files"]:
        data = read(file["path"])
        require(data == shared_data(file["path"], data), "SETTINGS_UNSUPPORTED", "共有候補の設定・アカウント一覧が正規化されていません")

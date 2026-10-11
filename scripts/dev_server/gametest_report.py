"""GameTestのJUnit XMLを検査する。保存・JVM終了の証明とは分離する。"""

from pathlib import Path
import xml.etree.ElementTree as ET

from .build_inputs import digest, regular_bytes
from .contracts import ContractError


def read_report(path: Path) -> dict:
    """欠損・壊れたXML・空結果・不一致の件数を合格にしない。実行ログは変更しない。"""
    try:
        body = regular_bytes(path)
        root = ET.fromstring(body)
    except (OSError, ET.ParseError) as error:
        raise ContractError("GameTest: JUnitレポートが欠損または不正です") from error
    if root.tag not in {"testsuite", "testsuites"}:
        raise ContractError("GameTest: JUnitレポートのルートが不正です")
    cases = list(root.iter("testcase"))
    counts = {"tests": len(cases),
              "failures": len(list(root.iter("failure"))),
              "errors": len(list(root.iter("error"))),
              "skipped": len(list(root.iter("skipped")))}
    for suite in root.iter():
        if suite.tag not in {"testsuite", "testsuites"}:
            continue
        for key, tag in (("tests", "testcase"), ("failures", "failure"), ("errors", "error"), ("skipped", "skipped")):
            if key not in suite.attrib:
                continue
            try:
                declared = int(suite.attrib[key])
            except ValueError as error:
                raise ContractError("GameTest: JUnitレポートの件数が不正です") from error
            if declared != len(list(suite.iter(tag))):
                raise ContractError("GameTest: JUnitレポートの宣言件数と結果が一致しません")
    return {**counts, "reportSha256": digest(body),
            "testsPassed": bool(cases) and not any(counts[key] for key in ("failures", "errors", "skipped"))}

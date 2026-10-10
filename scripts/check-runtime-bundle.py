"""#527のbundleを検査し、明示指定時だけヘッドレスGameTestを新しい検証領域で実行する。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import uuid
import xml.etree.ElementTree as ET

from dev_server.contracts import ContractError
from dev_server.runtime_bundle import _directory, launch_command, verify_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--game-test-root", type=Path)
    args = parser.parse_args()
    bundle = args.bundle.absolute()
    manifest = verify_bundle(bundle)
    if args.game_test_root is None:
        print(json.dumps({"artifactId": manifest["artifactId"], "status": "verified"}))
        return
    for key in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH"):
        if os.environ.get(key):
            raise ContractError(f"launch: {key}による暗黙の起動変更は禁止です")
    root = args.game_test_root.absolute()
    _directory(root)
    if bundle == root or bundle in root.parents or root in bundle.parents:
        raise ContractError("launch: artifactとテスト実行領域を分離してください")
    root.mkdir(parents=True, exist_ok=True)
    run = root / ("gametest-" + uuid.uuid4().hex)
    run.mkdir()
    command = launch_command(bundle, "gameTest", run)
    (run / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    with (run / "console.log").open("wb") as output:
        result = subprocess.run(command, cwd=run, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise ContractError(f"GameTest: JVMが失敗しました。log={run / 'console.log'}")
    report = ET.parse(run / "report.xml").getroot()
    cases = list(report.iter("testcase"))
    if not cases or list(report.iter("failure")) or list(report.iter("error")) or list(report.iter("skipped")):
        raise ContractError(f"GameTest: 未実施/失敗があります。report={run / 'report.xml'}")
    print(json.dumps({"artifactId": manifest["artifactId"], "run": str(run), "passed": len(cases)}))


if __name__ == "__main__":
    main()

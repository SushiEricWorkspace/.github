"""exportDevRuntimeから呼ぶ固定bundle出力adapter。通常サーバーは起動しない。"""

import argparse
import json
from pathlib import Path

from dev_server.contracts import ContractError
from dev_server.runtime_bundle import freeze
from dev_server.task_maven import write_receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--source-lock", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    args = parser.parse_args()
    receipt_path = args.source_lock.with_suffix(".receipt.json")
    if receipt_path.exists() or receipt_path.is_symlink():
        raise ContractError("export: 既存receiptは上書きしません")
    bundle = freeze(args.recipe, args.source_lock, args.store)
    receipt = write_receipt(args.source_lock, bundle)
    print(json.dumps({"artifactId": receipt["artifactId"], "bundle": receipt["bundle"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""exportDevRuntimeから呼ぶ固定bundle出力adapter。通常サーバーは起動しない。"""

import argparse
import json
from pathlib import Path

from dev_server.contracts import ContractError
from dev_server.runtime_bundle import _MANAGED_CANDIDATE, freeze
from dev_server.task_maven import write_receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--source-lock", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--managed-profile", type=Path, help="既exact Profileの明示候補。省略時は候補を生成しません")
    args = parser.parse_args()
    receipt_path = args.source_lock.with_suffix(".receipt.json")
    if receipt_path.exists() or receipt_path.is_symlink():
        raise ContractError("export: 既存receiptは上書きしません")
    bundle = freeze(args.recipe, args.source_lock, args.store, managed_profile_path=args.managed_profile)
    receipt = write_receipt(args.source_lock, bundle)
    print(json.dumps({"artifactId": receipt["artifactId"], "bundle": receipt["bundle"],
                      "managedCandidate": str(bundle / _MANAGED_CANDIDATE) if args.managed_profile else None,
                      "managedCandidateReason": "未承認候補" if args.managed_profile else "--managed-profile未指定"},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()

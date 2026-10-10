"""exportDevRuntimeから呼ぶ固定bundle出力adapter。通常サーバーは起動しない。"""

import argparse
import json
from pathlib import Path

from dev_server.runtime_bundle import freeze


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--source-lock", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    args = parser.parse_args()
    bundle = freeze(args.recipe, args.source_lock, args.store)
    print(json.dumps({"artifactId": bundle.name, "bundle": str(bundle)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

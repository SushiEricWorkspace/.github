"""完了taskの成功Maven UUIDだけを、dry-runの候補hashに基づき明示回収する。"""

import argparse
import json
from pathlib import Path
import sys

from dev_server.contracts import ContractError
from dev_server.task_maven import TaskMaven
from dev_server.task_worktrees import TaskError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--owner", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--build-id", help="回収するUUIDの完全hex")
    parser.add_argument("--candidate-hash", help="dry-runで返されたhash")
    args = parser.parse_args()
    try:
        manager = TaskMaven(args.workspace_root)
        if args.dry_run:
            if args.candidate_hash:
                raise ContractError("maven: dry-runへcandidate-hashは指定できません")
            result = manager.dry_run(args.task_id, args.owner)
        else:
            if not args.candidate_hash:
                raise ContractError("maven: 回収にはcandidate-hashが必要です")
            result = manager.collect(args.task_id, args.owner, args.build_id, args.candidate_hash)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ContractError, TaskError, OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

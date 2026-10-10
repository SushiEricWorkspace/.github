---
name: worktree
description: Issue・タスク単位のgit worktreeを作成・確認・担当引継ぎ・削除する。複数AIの作業分離、またはprimaryでAI作業を始めそうなときに使用する。既存の固定AI用worktreeも保全する。
---

# タスク単位worktree

作業前に対象repoのAGENTS.mdと、原本の[worktreeガイド](../../scripts/dev_server/worktrees-guide.md)を読む。
task-idは作業を一意にする小文字英数字とハイフン、ownerは同時書込担当のセッション識別子とする。
既に登録されたタスクへの参加では、同じworktreeのownerを引き継ぐ。
primary、人間、別ownerの作業ディレクトリを使わない。

## 手順

1. `scripts/setup-worktrees.py --list`で登録を確認する。既存固定worktreeは`--list-legacy`で読み取れる。
2. 新規作成ではIssue番号・repo・task-id・ownerを明示する。既定の起点はAGENTS.mdに対応するoriginの最新開発基準であり、前提Issueの依存commitや基準変更があるrepoだけ`--base REPO=REF`を追加する。
3. 出力されたパス `<workspace>/worktrees/<task-id>/<repo>/`で作業する。既存branch/worktreeの衝突を別パス作成やforceで回避しない。
4. 引継ぎは旧ownerの作業終了を確認してから`--handoff-from`を使う。未コミット変更があれば中断してユーザーへ相談する。
5. 削除の許可を確認し、`--remove --task-id ... --owner ...`で登録対象だけを削除する。branchは別途マージ済みであることを確認して削除する。

ownerは協調契約でありGit lockやアクセス制御ではない。削除時はdirtyと未知の追跡外データを拒否する。
`run/`・`.idea/`を複製しない。Minecraftの実行データはソースworktreeの寿命から切り離す。
途中失敗のRECOVERY_REQUIREDは実体と登録を確認し、自動修復・強制削除しない。

## 移行中の固定worktree

進行中の作業は自分に割り当てられた`<repo>-codex/`または`<repo>-claude/`で完了できる。
人間や別AIの固定worktreeを移動・削除しない。新規タスクはタスク単位で作成する。
分離テスト環境の管理CLIは総合導入Issue完了まで必須にしない。

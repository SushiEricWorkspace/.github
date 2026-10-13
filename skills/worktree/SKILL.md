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
5. 特定branch専用のworktreeは、branch削除を指示された場合、保持指示がなければ一緒に削除する。同じ削除許可を改めて求めず、Git一覧と管理記録を照合し、`--remove --task-id ... --owner ...`でworktreeを先に削除してからbranchを削除する。primary、共有・固定AI用、他branch用、指示対象外repoのworktreeを含めない。保全データや登録不一致、削除失敗があればworktreeとbranchを保全して報告する。worktree削除だけの指示からbranch削除の許可は推定しない。

ownerは協調契約でありGit lockやアクセス制御ではない。削除時はdirtyと未知の追跡外データを拒否する。
`run/`・`.idea/`を複製しない。Minecraftの実行データはソースworktreeの寿命から切り離す。
途中失敗のRECOVERY_REQUIREDは実体と登録を確認し、自動修復・強制削除しない。

## 削除失敗の診断と明示復旧

- Windowsの通常削除と削除前検査（status・ls-files）はGitコマンド単位で`core.longpaths=true`を指定する。local/global設定を書き換えず、forceを使用しない。
- エラー原文、対象パス、Git登録・実体・`.git`の有無、処理済みrepoと未処理/要確認repoを報告する。`Filename too long`と`Permission denied`を同じ原因と決めつけない。
- アクセス拒否では、対象をcwdにする端末・IDE、利用中プロセス、ファイル/親のアクセス権、sandbox実行権限を確認する。原因は確認するまで未確定とし、無関係なプロセス終了、自動kill、広範囲のpruneを行わない。
- 対象task・現在owner・全repo・保全内容と削除復旧の許可を確認する。許可済みの範囲では再確認せず、`--recover-remove --task-id ... --owner ...`で明示復旧する。一般の開発依頼やテストから既存実環境の修復許可を推定しない。
- registry形式2は既存task・repo・owner・state・created記録を保持して削除開始を記録する。形式1から記録を引き継ぎ、形式2未対応の旧スクリプトは使用しない。
- 復旧は削除開始記録がある`RECOVERY_REQUIRED`または中断した`REMOVING`だけを対象にする。開始を判定できない旧`RECOVERY_REQUIRED`は保全して拒否する。全repoを先に照合し、Git登録・実体が消失したrepoの記録整合、Git登録と`.git`がない空ディレクトリだけの除去、正常な未処理repoの通常検査付き削除を行う。
- Git登録のない非空ディレクトリ、残存`.git`、dirty、保全データ、他owner、異なるbranch・Git管理先、リンク・特殊ファイル、作成未完了や未知の状態は拒否して保全する。通常`--remove`から不一致を自動修復しない。
- 復旧後の`REMOVED`・各repoの`created=0`とGit一覧・パスの消失を照合する。branchと登録履歴、primary・他task・固定AI用worktree、instance・snapshot・artifactは削除対象に含めない。

## 移行中の固定worktree

進行中の作業は自分に割り当てられた`<repo>-codex/`または`<repo>-claude/`で完了できる。
人間や別AIの固定worktreeを移動・削除しない。新規タスクはタスク単位で作成する。
分離テスト環境の管理CLIは総合導入Issue完了まで必須にしない。

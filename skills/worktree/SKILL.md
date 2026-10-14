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
4. 引継ぎは旧ownerの書込み停止を確認してから`--handoff-from`を使う。未コミット変更を保持した同じworktreeのowner登録だけを更新し、HEAD・branch・index・追跡済み/未追跡/ignoredファイルは変更しない。commit・push・PR作成等の実行許可は別に確認する。
5. 特定branch専用のworktreeは、branch削除を指示された場合、保持指示がなければ一緒に削除する。同じ削除許可を改めて求めず、Git一覧と管理記録を照合し、`--remove --task-id ... --owner ...`でworktreeを先に削除してからbranchを削除する。primary、共有・固定AI用、他branch用、指示対象外repoのworktreeを含めない。保全データや登録不一致、削除失敗があればworktreeとbranchを保全して報告する。worktree削除だけの指示からbranch削除の許可は推定しない。

ownerは協調契約でありGit lockやアクセス制御ではない。削除時はdirtyと未知の追跡外データを拒否する。
`run/`・`.idea/`を複製しない。Minecraftの実行データはソースworktreeの寿命から切り離す。
途中失敗のRECOVERY_REQUIREDは実体と登録を確認し、自動修復・強制削除しない。

## 公開担当へのアクセス情報

repo・worktreeの絶対パス、owner、許可操作、基準commit・差分と確認時点、sandbox権限、worktreeとGit共通管理領域へのアクセス可否、主担当で成功した実行方法を渡す。
owner登録の引継ぎはOSやsandboxのアクセス権を変えない。
アクセス拒否時は書込みを止め、明示したworkdirと絶対パスの`git -C`で読み取り確認する。
通常実行が拒否され、操作が許可済みなら、対応環境では対象と理由を限定した`require_escalated`で読み取りを再試行する。
成功したら差分・stage・追跡先・既存PRを確認して許可済みの公開を続ける。通常実行のエラーだけでworktree破損や利用不可と断定しない。
承認付き実行の利用不可・拒否・再試行失敗では、実行方法・エラー原文・登録状態・未実行操作を報告して保全する。
ACL変更、sandbox全体の無効化、owner奪取、別worktreeへの複製、Git管理情報の修復を回避策にしない。Git操作許可と実行時の承認レビューを区別する。

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

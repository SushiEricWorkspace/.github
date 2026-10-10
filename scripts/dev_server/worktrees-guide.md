# タスクworktreeの管理

Issueごとのソース・Git index・build出力を分離するときに使用します。
Python 3.12とGitを使用し、primary repositoryは同じworkspaceに配置します。
サーバー実行環境の管理CLIとは別のツールです。確定契約は[README](README.md)を参照してください。

## 作成・担当引継ぎ・一覧

`.github` repositoryから実行します。ownerにはAI名だけでなく同時書込担当のセッションを識別できる値を指定します。

```bash
python3.12 -B scripts/setup-worktrees.py --task-id issue-123-a --issue 123 --owner codex-session-a --repos Common,SushiEricServerMod
python3.12 -B scripts/setup-worktrees.py --list
python3.12 -B scripts/setup-worktrees.py --list --task-id issue-123-a
python3.12 -B scripts/setup-worktrees.py --task-id issue-123-a --handoff-from codex-session-a --owner claude-session-b
```

作成先は`<workspace>/worktrees/issue-123-a/<repo>/`、branchは`feature/issue-123`です。
`--workspace-root PATH`でworkspaceを明示できます。primaryのbranch・index・作業ファイルは変更しません。
originをfetchし、repoごとの開発基準commitを記録します。Common・ServerManager・CombatCoreはdevelop、Mod・.githubはmainです。
origin/HEADがリリースbranchを指していても、開発基準から起点を変えません。
依存commitを使うrepoでは`--base Common=<commit>`のように指定します。複数repoの起点を個別に指定できます。
既存のlocal/remote branchやworktreeは取り込んだり上書きしたりせず拒否します。

登録は`worktrees/registry.sqlite`（形式1）で管理します。同一taskへの同じ入力の再実行は実体を確認して結果を返します。
異なるowner、repo、起点commit、Issueで同一taskを再利用しません。
ownerの更新はSQLiteトランザクションで排他します。Gitのref操作はGit自身のlockを使用します。
ownerは同一ユーザーの協調契約であり、OSのアクセス制御ではありません。手作業のGit操作も同じownerが行います。

## 削除と途中失敗

```bash
python3.12 -B scripts/setup-worktrees.py --remove --task-id issue-123-a --owner claude-session-b
```

全repoの事前検査を通してから、記録されたworktreeだけを`git worktree remove`します。
未コミット変更、登録branchの不一致、symlink/reparse point、特殊ファイルは拒否します。
ignoredデータは再生成可能な`build/`・`.gradle/`内の通常ファイルだけ削除対象にできます。
`run/`などその他のignoredデータがあれば止まります。必要なデータを別途保全してから再実行してください。
サーバー実行データの`minecraft-dev/`、artifact、snapshot、instanceは操作しません。
branchや登録履歴は残ります。マージ後のbranch削除は別操作です。

途中失敗は`RECOVERY_REQUIRED`となり、部分的に作成したworktreeも保全します。
登録済みの実体が検証できる場合のみ、ownerを明示した削除を再実行できます。
Git操作の直後に中断して登録と実体が食い違った場合は、自動修復せず手動確認で停止します。
`CREATING`・`REMOVING`のまま中断したtaskも、別ownerで奪ったりtask-idを再利用したりしません。
worktreeのディレクトリを直接消す、forceを付ける、広範囲のpruneを行う、という回避は禁止です。

## 既存環境と同期

既存の固定AI worktreeは`--list-legacy`で読めます。作成・削除・移動・runコピーを自動で行いません。
進行中の固定worktreeの作業はそのまま完了し、新規タスクだけをタスク配置へ切り替えます。
旧`--agents`・`--no-copy`・`--install-skill`は使用しません。スキル原本は`skills/worktree/SKILL.md`です。
ローカルに配置する場合は原本を使い、配置先をコミットしません。

共通ルールはprimaryではなく担当worktreeを明示して同期します。指定したrepoだけが対象になります。

```bash
python3.12 -B scripts/sync-agent-rules.py --repo-root Common=worktrees/issue-123-a/Common --repo-root SushiEricServerMod=worktrees/issue-123-a/SushiEricServerMod
python3.12 -B scripts/sync-agent-rules.py --check --repo-root Common=worktrees/issue-123-a/Common
```

## 検証範囲

```bash
python3.12 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

一時Git repositoryで二タスクのsource/index/build分離、owner競合、引継ぎ、dirty拒否、実行領域の保全を確認します。
build分離試験は独立ディレクトリへの合成出力であり、Gradle実行の試験ではありません。
Windowsでの単体テストをmacOS 15の運用適合や停電耐久性の実証として扱いません。

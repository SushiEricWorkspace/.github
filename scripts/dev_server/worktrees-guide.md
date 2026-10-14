# タスクworktreeの管理

Issueごとのソース・Git index・build出力を分離するときに使用します。
Python 3.12とGitを使用し、primary repositoryは同じworkspaceに配置します。
サーバー実行環境の管理CLIとは別のツールです。確定契約は[README](README.md)を参照してください。

## 作成・担当引継ぎ・一覧

`.github` repositoryから実行します。ownerにはAI名だけでなく同時書込担当のセッションを識別できる値を指定します。

```bash
python3.12 -B scripts/setup-worktrees.py --task-id issue-123-a --issue 123 --owner codex-session-a --repos SushiEricServerMod
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

Issueの変更範囲はそのrepo内だけです。他repoも変更する場合は、そちらに別Issueを作り、`owner/repo#番号`で相互参照します。
例えばModのIssueでCommonのAPI変更も必要になったら、Common側の別IssueでAPI・完成条件・検証を定め、その確定commitをModの前提にします。
既存Commonソースの読み取りや確定成果物の利用だけなら、Commonの変更Issueは不要です。
異なるrepoの同番号Issueは別の作業として扱い、依存repoへ元Issue番号の変更branchを作りません。
現在のCLIは列挙した全repoへ同じIssue番号のbranchを作るため、変更しない依存repoをその目的で`--repos`へ含めないでください。確定依存worktreeの専用登録は[#40](https://github.com/SushiEricWorkspace/.github/issues/40)で対応します。

登録は`worktrees/registry.sqlite`（形式2）で管理します。同一taskへの同じ入力の再実行は実体を確認して結果を返します。
異なるowner、repo、起点commit、Issueで同一taskを再利用しません。
形式1の既存task・repo・owner・state・created記録を保持して、形式2の削除開始記録を追加します。
形式2に未対応の旧スクリプトは使用せず、現在のスクリプトを使ってください。未知のregistry形式は拒否します。
ownerの更新はSQLiteトランザクションで排他します。Gitのref操作はGit自身のlockを使用します。
ownerは同一ユーザーの協調契約であり、OSのアクセス制御ではありません。手作業のGit操作も同じownerが行います。

担当引継ぎは旧ownerの書込み停止を確認してから、同じworktreeを`--handoff-from`で新ownerへ渡します。
実装担当からGit公開担当への引継ぎでは、ステージ済み・未ステージ・未追跡・ignoredファイルを保持したままowner登録だけを更新します。
HEAD、branch、index、ファイルは変更せず、自動commit・stash・reset・checkoutや別worktreeへの複製は行いません。
現在owner・ACTIVE状態・全repoの実体とGit登録・branch・管理パスを確認し、ビルド中や不一致があればownerを更新しません。
新規作成・既存登録の作成再実行・削除・削除復旧は従来の保全条件を維持します。
引継ぎはGit公開操作の許可を増やしません。commit・push・PR作成・merge・削除にはそれぞれ実行許可が必要です。

### 公開担当へのアクセス情報の引継ぎ

公開担当にはrepo・worktreeの絶対パス、owner、許可操作、基準commit・確認時点と差分を渡します。
併せて現在のsandbox権限、対象worktreeとGit共通管理領域がアクセス可能な範囲にあるか、主担当で成功した実行方法を明示します。
管理スクリプトのowner更新は協調上の担当変更であり、OSのACLやsandboxの権限を変更しません。

アクセス拒否時は書込みを止め、明示したworkdirと絶対パスの`git -C <worktree> status --short`で確認します。
通常実行が拒否されたら、既存の操作許可と権限境界を照合します。対象操作が許可済みで対応する環境では、対象を限定した理由を付けて`require_escalated`で読み取りを再試行します。
通常実行での`Permission denied`や`this operation must be run in a work tree`だけを、Git登録破損の証拠にしません。
正常にアクセスできたら差分・stage・追跡先・既存PRを再確認し、許可済みの公開工程を続けます。実行時の承認レビューとGit公開許可は別です。

承認付き実行が利用不可、承認レビューで拒否、または再試行後も失敗した場合は状態を保全し、実行方法、エラー原文、管理登録、未実行操作を報告します。
ACL変更、sandbox全体の無効化、owner奪取、別worktreeへの複製、Git管理情報の修復を回避策にしません。

## 削除と途中失敗

```bash
python3.12 -B scripts/setup-worktrees.py --remove --task-id issue-123-a --owner claude-session-b
```

全repoの事前検査を通してから、記録されたworktreeだけを`git worktree remove`します。
未コミット変更、登録branchの不一致、symlink/reparse point、特殊ファイルは拒否します。
ignoredデータは再生成可能な`build/`・`.gradle/`内の通常ファイルだけ削除対象にできます。
`run/`などその他のignoredデータがあれば止まります。必要なデータを別途保全してから再実行してください。
サーバー実行データの`minecraft-dev/`、artifact、snapshot、instanceは操作しません。
worktree削除だけの指示ではbranchや登録履歴は残ります。
特定branch専用のworktreeは、branch削除を指示された場合、保持指示がなければ一緒に削除します。
Git一覧と管理記録で対象を照合し、worktreeを先に管理手順で削除してからbranchを削除してください。
primary、共有・固定AI用、他branch用のworktreeは対象外です。
`--remove`はtask内の全repoを対象にするため、一部repoだけのbranch削除指示で他repoを巻き込まないでください。
未コミット変更、保全データ、他ownerの作業継続、登録不一致、削除失敗があればworktreeとbranchを保全し、原因と残り作業を報告します。

Windowsの削除前検査（`status`・`ls-files`）とGit削除はコマンド単位で`-c core.longpaths=true`を指定します。
local/global設定は変更せず、dirty検査を無効化する`--force`も使用しません。
全repoの事前検査から削除完了までbuild lockを保持します。

途中失敗は`RECOVERY_REQUIRED`となり、失敗時のエラー原文、対象パス、Git登録・実体・`.git`の有無、処理済みrepoと未処理/要確認repoを表示します。
Git削除前の`REMOVING`遷移と同じトランザクションで削除開始を記録し、一覧の`removal_started`で確認できます。
Git削除成功後も実体と登録の消失を確認してから`created=0`を記録します。
失敗や中断を削除完了として扱いません。`--list --task-id ...`で現在の管理記録を確認してください。

### エラー別の確認

- `Filename too long`: 出力された対象パスとGit登録・`.git`の有無を照合します。通常の削除では検査時もWindowsの長いパス設定を付け、保存データの列挙漏れを防ぎますが、失敗後の非空ディレクトリを再帰削除して回避しません。
- `Permission denied` / `Access is denied`: 対象worktreeをcwdにする端末・IDE、ファイルを利用中のプロセス、ファイルと親ディレクトリのアクセス権、sandboxの実行権限を確認します。エラーだけで利用中プロセスや権限不足を断定しません。必要な許可を得て利用者が原因を解消してから再開します。無関係なプロセス終了、自動killは行いません。
- 登録branch不一致、未知のファイル、リンク、特殊ファイル、照合不能: 全repoを保全して停止し、対象・owner・データの保全先と個別対応を確認します。force、広範囲のprune、DBの手編集で回避しません。

### 中断した削除の明示復旧

復旧の対象task、現在owner、全repo、保全すべきデータを確認し、削除復旧が許可された範囲内で実行します。
既存実環境の不整合修復を、開発・テスト依頼だけから実行しません。

```bash
python3.12 -B scripts/setup-worktrees.py --list --task-id issue-123-a
python3.12 -B scripts/setup-worktrees.py --recover-remove --task-id issue-123-a --owner claude-session-b
```

`--recover-remove`は`RECOVERY_REQUIRED`または中断した`REMOVING`の同一task・現在ownerだけを対象とします。
`RECOVERY_REQUIRED`には削除開始記録が必要です。旧記録に開始記録がなければ作成失敗と区別できないため保全して拒否します。
旧形式でも`REMOVING`は削除開始が確定しているため、全repoの照合後に復旧できます。
開始記録のない旧`RECOVERY_REQUIRED`でも、全repoの登録・実体が正常なら許可範囲内で通常`--remove`の検査を通して削除できます。
登録不一致を通常削除で回避せず、判定不能な旧記録は個別に保全と対応を確認してください。
全repoの管理記録、管理範囲内のパス、branch・基準commit、Git登録、リンクと実体を先に検査してから、次の処理を行います。

| repoの状態 | 処理 |
|---|---|
| Git登録と実体がない | 削除済みとして`created=0`を記録 |
| Git登録と`.git`がなく空ディレクトリだけ残る | 削除直前にも再検査し、空ディレクトリだけ除去して記録を整合 |
| 正常なGit登録・実体が残る | 通常のdirty・ignored・リンク検査を通してGit削除を再開 |
| Git登録のない非空ディレクトリ、残存`.git`、異なるbranch・Git管理先、リンク・特殊ファイル、判定不能 | 変更せず拒否。個別に保全と対応が必要 |

作成未完了のrepo、branch消失、未知の状態・registry形式も拒否します。作成途中のtaskを推測で修復しません。
通常`--remove`では登録不一致を整合させません。`CREATING`の中断や他ownerのtaskを奪取せず、task-idを再利用しません。
復旧後は`state=REMOVED`、各repoの`created=0`、Git一覧と対象パスの消失を確認します。
復旧もtask内の全repoが対象で、branchと登録履歴、primary・固定AI用worktree、instance・snapshot・artifactは残します。

## 既存環境と同期

既存の固定AI worktreeは`--list-legacy`で読めます。作成・削除・移動・runコピーを自動で行いません。
進行中の固定worktreeの作業はそのまま完了し、新規タスクだけをタスク配置へ切り替えます。
旧`--agents`・`--no-copy`・`--install-skill`は使用しません。スキル原本は`skills/worktree/SKILL.md`です。
ローカルに配置する場合は原本を使い、配置先をコミットしません。

共通ルールの書き込みでは担当worktreeを`--repo-root REPO=PATH`で明示します。省略時は同期を拒否し、`--workspace-root`だけでは同期先になりません。指定したrepoだけが対象になります。`--check`は`--repo-root`を省略するとworkspace全体を読み取り確認します。

```bash
python3.12 -B scripts/sync-agent-rules.py --repo-root Common=worktrees/issue-123-a/Common --repo-root SushiEricServerMod=worktrees/issue-123-a/SushiEricServerMod
python3.12 -B scripts/sync-agent-rules.py --check --repo-root Common=worktrees/issue-123-a/Common
```

## 検証範囲

```bash
python3.12 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

一時Git repositoryで二タスクのsource/index/build分離、owner競合、未コミット変更を保持した引継ぎ、作成再実行・削除・削除復旧のdirty拒否、実行領域の保全を確認します。
部分削除、Git削除直後の中断、複数repoの途中失敗、アクセス拒否は一時repositoryへの故障注入で確認します。
Windows実Gitで300文字を超えるignored build/.gradle出力の削除を確認する試験は、Windows以外ではskipします。
skipを実行成功として扱いません。macOSの一時ディレクトリにsymlinkの祖先がある場合は`TMPDIR=/private/tmp`で実行します。
build分離試験は独立ディレクトリへの合成出力であり、Gradle実行の試験ではありません。
Windowsでの単体テストをmacOS 15の運用適合や停電耐久性の実証として扱いません。

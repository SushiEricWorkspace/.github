# 管理接続プロファイルと貸出引継ぎ（形式1）

Managerと人間用起動入口の共通契約です。貸出とwriterの正本は
[管理CLI](supervisor-guide.md)・[writer契約](writer-guide.md)です。
別PCから同じ契約を使う場合は[SSH仲介ガイド](ssh-guide.md)を参照してください。
固定設定を一度登録し、実行時はprofile ID・枠候補・借用または引継ぎを選びます。
Manager画面への組込みはServerManager #196/#197、人間用ローカル一括起動は[一括操作ガイド](human-server-guide.md)、OS別wrapper/SSH入口は[操作ガイド](human-entry-guide.md)、SSH仲介は #15で扱います。
このIssueでは接続画面や起動停止の自動化を追加しません。

## 保存先と登録

Python 3.12で `scripts/connection-profile.py` を使用します。保存先はGit worktree外の
明示した絶対パスで、親ディレクトリは先に用意してください。
Windowsでは所有者限定DACL、macOSでは所有者のdirectory=0700/file=0600を要求します。
リンク・reparse point・hardlink記録を拒否します。記録をGitへコピーしないでください。

次は架空のmacOSホストをSSH設定のHost名で参照する非秘密プロファイルです。
Host設定、認証鍵、パスワード、passphrase、RCON password、API認証情報をこのJSONへ追加できません。

```json
{
  "version": 1,
  "profileId": "dev-mac",
  "displayName": "開発用 Mac",
  "transport": "ssh",
  "sshUser": "approved-user",
  "targetOs": "macos",
  "host": "approved-dev-mac",
  "managementRoot": "/Users/example/開発 環境/minecraft-dev",
  "python": "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12",
  "cli": "/Users/example/tools/scripts/dev-server.py",
  "instances": [{"instanceId": "persistent-01", "displayName": "永続枠 1"}],
  "approval": {"operator": "example-operator", "reference": "対象ホスト・rootの承認記録への参照"}
}
```

SSHの場合は`sshUser`で承認済みのログインユーザーを固定します。既存のSSH profileはこの項目を追加して明示編集してください。編集前の引継ぎはhash不一致で拒否されます。
localの場合はtransportを`local`、hostを実行ホスト名にし、`sshUser`を指定しません。
WindowsではtargetOsを`windows`にし、三つのパスを対象Windowsホストの絶対パスにします。
例えば `C:\\開発 環境\\minecraft-dev` です。クライアントOSで遠隔パスをresolveしません。
profile version=1の項目は全て必須・未知項目禁止で、枠候補のIDは重複できません。
`approval`は実在する既承認のホスト/rootを参照する宣言であり、このJSONの登録は運用承認を発行しません。
接続する側は承認記録と対象ホスト実体、Python 3.12、CLI、inventory/root/枠を再照合します。
自動探索でrootを補完したり承認範囲を拡張したりしません。

```bash
# profile.jsonには上の非秘密JSONだけを記載する（保存場所はGit外）。
python3.12 -B scripts/connection-profile.py register --directory /Users/example/connection-private < profile.json
python3.12 -B scripts/connection-profile.py check --directory /Users/example/connection-private --profile-id dev-mac
# 固定設定の編集は明示する。編集前に発行した引継ぎはprofile hash不一致で拒否する。
python3.12 -B scripts/connection-profile.py edit --directory /Users/example/connection-private < profile.json
```

PowerShellでは、パイプの日本語をUTF-8で渡すため `$OutputEncoding = [Text.UTF8Encoding]::new($false)` を設定してから `Get-Content -Raw -Encoding utf8 'C:\設定\profile.json' | & 'C:\Python312\python.exe' -B scripts/connection-profile.py register --directory 'C:\設定\connection-private'` を使用できます。
`check`は保存記録の形式・権限検査だけで、ホストへの接続・Pythonの実バージョン・運用適合は確認しません。
profileは`profiles/<profileId>.json`、短命情報は`handoffs/<handoffId>.json`に分離します。
保存・編集途中の失敗ファイルは保全し、自動で消したり上書きして成功扱いにしたりしません。

## 借りて接続する場合

利用側は明示された枠に対して `acquire` を一度行い、結果をprivateに保存します。
owner、ttlSeconds、operationIdを呼出元が保持します。結果不明なら同じ要求の照合を行い、
別IDのacquireを再送しません。他ownerの貸出tokenを一覧から取得しません。
acquire直後の `generationBound=false` や空世代のままwriterを開けません。
prepare/restore/resetとstartは既存CLIの前提に従い、返却後のデータを無断で再起動しません。
借用したclientがrenew・正常停止・返却の責任を負い、アプリ終了だけでreleaseしません。

## 既存貸出への明示的な引継ぎ

引継ぎは貸出ownerの変更ではなく、同じownerに属するclient間の接続権の一回限りの受け渡しです。
発行元は貸出取得結果のtokenを保持し、CLI `lease-check`で現在の貸出を検査します。
payloadは `instanceId/leaseId/epoch/expectedGeneration/owner` の全項目です。
`lease-check`は読み取り専用で、leaseIdを返さず、期限更新・貸出・返却を行いません。
instance lock下でRegistryのtokenとownerを検査し、RUNNINGの実体検査も既存runtimeへ委譲します。
dry-runの証明は拒否します。

`ConnectionStore.offer(profile, token, checked, owner=..., source_client=..., target_client=...)`
はlease-check結果を使い、profile hash、token、owner、発行元・受取client、期限、runId/nonce、
新規sessionId、引継ぎIDをprivateに記録し、IDだけを返します。
固定profileにleaseId/epoch/generation/run/nonce/sessionを埋め込みません。
引継ぎIDは明示された相手へだけ渡し、通常の表示・診断にはtokenや記録全文を出しません。

受取側は明示された一つのIDへ
`ConnectionStore.claim(profile, handoff_id, owner=..., target_client=..., lease_check=...)`
を呼びます。callbackはそのprofileと同じホスト/rootの正規CLIを呼び、
`lease-check`の成功したresultだけを返します。保存JSONやstatusを成功応答に偽装してはいけません。
local用callbackでは `local_request(profile, "lease-check", payload, operation_id=...)` を使用できます。
SSH用callbackは #15の既存認証・host key検証付き仲介を使用し、localへfallbackしません。
秘密payloadはstdin/private IPCへ送り、コマンドライン・通常ログへ出しません。

状態遷移は `OFFERED → CLAIMING → CLAIMED` です。CLAIMINGを検査前に耐久化し、
再照合の失敗・応答喪失はUNKNOWNとして残します。プロセス中断でCLAIMINGが残った場合も
再取得できません。他owner/client、期限切れ、旧epoch・旧世代・旧run/nonce、profile変更、
未完了操作・UNKNOWN writerを拒否します。OS lockは競合を待たず拒否し、二重claimを防ぎます。
CLAIMEDはwriter-open成功を意味しません。受取側は記録の新規sessionIdでwriter-openを一度行い、
実PID/startMillis/cwdを登録します。SSHではサーバーホスト上の仲介自身を登録します。
引継ぎ時はacquireを行わず、同じ枠を二重に借りません。

| 操作責任 | 借用client | 引継ぎ発行元 | 引継ぎ受取client |
| --- | --- | --- | --- |
| renewと期限の監視 | 担当 | 継続して担当 | 発行元の継続を確認。失敗時は編集・保存を保全して停止 |
| writer-open/保存/close | 接続時に担当 | 自身のwriterだけ担当 | 新規sessionを担当 |
| stop/snapshot/release | 明示操作で担当 | 明示操作で担当 | 自動実行しない |
| Managerだけ正常切断 | writer drain/Closeを照合 | renewとサーバーの責任を保持 | 自身のwriterだけ終了。サーバー・貸出を残す |

受取側はrenew・release責任を暗黙に奪いません。発行元は引継ぎ記録の期限より前にrenewし、
受取側の接続中も継続します。記録の期限はclaim期限であり、CLAIMED後は正規CLIの現在期限が正本です。
発行元の終了・renew失敗は自動解放の根拠ではありません。
writer-closeは全受理要求の結果、upload/save完了、監視socket Closeの成功を揃えた後だけ行います。
切断・timeout・不明なwriterは既存UNKNOWN/保留を残し、期限切れだけで解放・kill・再貸出しません。
本形式に自動返却、owner変更、UNKNOWNをOFFEREDへ戻す操作はありません。

## 検証

```bash
python3.12 -B -m unittest discover -s scripts -p test_connection_profiles.py -v
python3.12 -B -m unittest discover -s scripts -p test_supervisor.py -v
```

macOSの一時領域に`/var`などのリンクを含む場合は、テストプロセスだけに
`TMPDIR=/private/tmp`を指定します。通常パスの検査は維持し、ホスト設定は変更しません。

```bash
TMPDIR=/private/tmp python3.12 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

Issue #14の全313件の検証結果はWindows/Python 3.12.14で305件成功・8件skip、
macOS/Python 3.12.4で300件成功・13件skipです。両OSで追加19件は全て成功しました。
macOSはWindows専用12件とCommon入力未指定の実Gradle1件をskipしています。
Windowsの成功をmacOSの検証として代用せず、それぞれのOSで実行しています。

両OS上で同じテストを実行します。架空のprofile、使い捨てprivate記録、合成Registryを使用し、
本番接続・実SSH・実Minecraft・Manager実画面の検証とは区別します。
実SSHは対象ホスト・絶対worktree・操作内容の別途承認後に行います。

# 開発用SSH仲介

承認済みSSH profileを使い、サーバーホスト上の一接続・一枠の仲介を介して既存supervisorを操作します。
Python 3.12とOpenSSHクライアントを使用します。IPCは同一ユーザー専用、Management APIはloopbackのままです。
Manager画面への組込みはServerManager #196/#197、人間用入口は[操作ガイド](human-entry-guide.md)を参照してください。

## 接続条件

[接続プロファイル](connection-profile-guide.md)の`transport: ssh`に承認済みの`sshUser`を指定します。
host/root/Python/CLI/ユーザーは明示編集し、以前のhashに固定した引継ぎを流用しません。
既存SSH profileには`sshUser`を追加してください。移行・自動補完はありません。
別途確認したホスト鍵の既存known_hostsを絶対パスで渡します。未確認・変更済みの鍵や対話認証は拒否します。
鍵の自動登録、agent/X11/port forwarding、multiplexing、ProxyCommand/ProxyJumpは使用しません。
認証は既存OpenSSH環境を使い、秘密鍵・passwordをprofileやprotocolに追加しません。
SSHサーバー・Firewall・認証設定を変更・自動有効化しません。

macOSでは固定CLIの親ディレクトリへ移動し、引用済み引数でPythonをexecします。
Windowsでは標準cmd/PowerShellから`powershell.exe -EncodedCommand`を起動し、Pythonの標準streamをbyteのまま中継します。
空白・Unicode・引用符を含むパスを扱います。本文・tokenはshell引数に載せません。独自SSH shellは検証対象外です。

## 呼出し契約

`scripts`をPythonのimport pathへ置き、`SshConnection`を使用します。
`journal_parent`にはGit外の`ConnectionStore.directory`を渡します。
profileは承認済みの値、token/run/nonceは同じホストCLIで検査した値を使用してください。

```python
from pathlib import Path
import uuid
from dev_server.connection_profiles import ConnectionStore
from dev_server.ssh_connection import SshConnection

store = ConnectionStore(private_directory)  # Git外の絶対Path
connection = SshConnection(profile, "persistent-01", "manager-01",
                           known_hosts=Path(approved_known_hosts),
                           journal_parent=store.directory, timeout=15)
checked = connection.request("lease-check", dict(token, owner=owner),
                             operation_id=uuid.uuid4().hex, timeout=40)
if not checked["ok"]:
    raise RuntimeError("貸出検査に失敗しました")
run = checked["result"]
opened = connection.request("open", dict(token, owner=owner, sessionId=uuid.uuid4().hex,
                                         runId=run["runId"], nonce=run["nonce"]),
                            operation_id=uuid.uuid4().hex, timeout=40)
```

呼出しはCLI envelope（`ok`、`operationId`、`result`または`error`）を返します。
APIの`success: false`を変更しません。接続・protocol・timeoutの結果不明は`RegistryError`になります。
各要求は直列実行し、operationIdは一度だけ使用します。呼出し側は`ok`を確認して次へ進みます。

| command | payloadと用途 |
| --- | --- |
| status/acquire/renew/lease-check/release | 通常CLIのpayload。接続に固定したinstanceIdだけを許可 |
| prepare/restore/reset/start/stop/snapshot/recover | 通常CLIのpayloadと同じ貸出・世代検査を使用 |
| human | command/owner/options/actionId/resume/targetClient。確認済みホストの共通HumanServerへ委譲。writer接続とは混在不可 |
| open | token、owner、sessionId、runId、nonce。PID/cwdは受け取らず仲介自身を登録 |
| io | `{"request": ...}`。[writer-I/O](writer-guide.md)の許可対象だけを委譲 |
| monitor-open | `{"intervalTicks": 20}`。1〜1200tick、接続ごとに監視socketは一つ |
| monitor-read | `{"timeoutSeconds": 3}`。1〜30秒、`events`配列を返す。通知なしは空配列 |
| monitor-close | `{}`。unsubscribe応答とpeer Closeを確認 |
| close | `{}`。操作完了・監視peer Close・writer-close後に仲介を終了 |

`open`は同じ貸出・起動に固定します。監視も`lease-check`と読み取り専用`writer-check`で
自身のOPEN登録・nativeプロセス・現在のrunを再照合し、監視socketから管理コマンドを送りません。
単発APIは別socketで応答nonceとpeer Closeまで確認します。writer接続中のreleaseは拒否します。
正常切断でも貸出を返却せず、更新・返却責任は取得元に残ります。

引継ぎは取得元の`lease-check`結果で`ConnectionStore.offer`を作り、
受取側の`ConnectionStore.claim(..., lease_check=...)`からこのSSH接続の`lease-check`を呼びます。
claimは取得済み貸出を一度だけ受け取り、新sessionIdで`open`します。acquireやrenew責任の移譲へ変換しません。

要求・応答は4byte長さ付きJSON objectで、それぞれ最大64KiBです。stdoutはprotocol専用、診断はstderrです。
クライアントはstderr本文を保存・転載しません。既存IPCへの待機は35秒、監視購読/Closeは5秒です。
クライアントの要求待機は既定5秒なので、必要に応じて明示したtimeoutを使います。
上限を超えた長い処理は、完了を推測せず既存CLI証跡で照合してください。

## 正常切断と保留

`close`成功にはSSH子プロセスのexit=0も必要です。受理済み操作の結果を待つ前に切断しません。
peer Close欠落、SSH EOF、仲介クラッシュ、保存中timeout、応答ID不一致はUNKNOWN/保留として扱い、
自動再接続・再送・再保存・writer-close・貸出返却・killを行いません。
client証跡はprivate storeの`ssh-clients/<接続ID>/`、server証跡は`<root>/manager/ssh-connections/<接続ID>/`です。
要求ID・指紋・状態・応答hashを保存し、本文・tokenは保存しません。
クラッシュではPENDINGやOPEN記録が残る場合があります。これを完了・終了証明にしません。

## 検証範囲

```bash
python3.12 -B -m unittest discover -s scripts -p test_ssh_broker.py -q
python3.12 -B -m unittest discover -s scripts -p 'test_*.py' -q
```

macOSのテストプロセスには`TMPDIR=/private/tmp`を使用します。
両OSで合成Registry、実native子プロセス/private IPC、実WebSocketの応答・peer Closeを検査します。
Windowsのbinary標準stream・EOF前の二往復はWindows上で検証し、macOSではWindows native試験をskipします。
別途許可したWindows→macOSの実SSHで、合成runtimeの貸出・prepare/start/renew、Unicode保存・読込、
APIのsuccess保持、監視購読解除・peer Close、正常SSH終了、仲介自身のPID登録、切断後の貸出保持を確認しました。
この記録はIssue #15の検証範囲です。人間用入口による両OSの実Mod・実SSHの結果は、
[Issue #17の検証記録](human-entry-guide.md#issue-17の検証記録)に記載します。Manager画面と本番運用は未検証です。

## 人間用一括操作の応答

humanの外側okはRPCの応答確定、result内のokは一括操作の成功を表します。内側の未完了はphase/actionId/operationId/evidenceを返し、貸出を自動解除しません。
操作の再送は新しいSSH接続から同じclientId/actionIdと選択でresumeを明示します。接続journalのoperationIdは一接続内で再利用しません。
一括操作のtokenとjournalはホスト側human-clientsへ、client側は固定選択と応答確定だけをprivateへ保存します。handoff材料はframe内だけで渡し、一回限りofferの正本はclientのConnectionStoreに一つだけ作ります。

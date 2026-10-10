# 監督プロセスと貸出Registry

分離テスト環境の宣言設定、supervisor、貸出を扱います。
ソースの作成・削除は[worktreeガイド](worktrees-guide.md)、固定artifactは[runtime出力ガイド](runtime-export-guide.md)、
文書の形は[契約ガイド](README.md)を参照してください。
Python 3.12を使用し、macOSとWindowsで同じCLI・Registryを使います。

## 検証境界

この段階ではMinecraft・Managerを起動しません。start/stop/prepare/snapshot/restore/recover/gcは
`NOT_IMPLEMENTED`です。doctorはIPC到達とRegistry読取を確認するだけで、実ポートのbind、
保存完了、外部writer終了、FSの停電耐久性を証明しません。
実データを持つ枠のreleaseも、後続の保存・終了証跡を接続するまで拒否します。
総合受入と運用者の設定が揃うまで既存環境を置き換えず、運用を有効化しません。

## 初期化

inventoryは`contracts-v1.json`のinventory契約に従うJSONを運用者が明示します。
管理root、operator、quotaBytes、reserveBytes、別媒体backupの確認者、二枠と各portを省略できません。
backup先へは書き込みません。同一rootと同じディスクを別媒体と推測しません。
macOSはPOSIX絶対パス、Windowsはドライブ付き絶対パスを指定します。
UNC、親参照、リンク/reparse point、Windowsの予約名・ADSは拒否します。
形式検査は実媒体・FS種別の検証ではなく、#537の運用者確認が別途必要です。

管理rootの親だけを事前に用意し、存在しないrootまたは空のrootを指定してください。
CLIへ渡すJSONやファイルに認証token/RCON passwordを含めません。

```bash
python3.12 -B scripts/dev-server.py init --root /absolute/minecraft-dev --inventory /absolute/inventory.json --operation-id init-01 --dry-run
python3.12 -B scripts/dev-server.py init --root /absolute/minecraft-dev --inventory /absolute/inventory.json --operation-id init-01
```

Windowsでは`python3.12`を`py -3.12`へ置き換え、`--root`を`C:/...`形式にします。
初期化を中断してrootへ部分データが残った場合は自動再初期化・削除せず止まります。
完了済みinitは同じinventoryと同じoperationIdだけが再送可能です。
schemaVersionは1です。開発中の形式変更に旧データ移行処理は追加しません。

## supervisorと要求

一つのターミナルで、初期化済みrootを明示して常駐プロセスを実行します。
CLIは接続失敗時にsupervisorを自動起動しません。

```bash
python3.12 -B scripts/dev-server.py supervise --root /absolute/minecraft-dev
python3.12 -B scripts/dev-server.py doctor --root /absolute/minecraft-dev
python3.12 -B scripts/dev-server.py list --root /absolute/minecraft-dev
python3.12 -B scripts/dev-server.py acquire --root /absolute/minecraft-dev --operation-id acquire-01 --payload '{"instanceId":"persistent-01","owner":"codex-session-a","ttlSeconds":3600}'
```

`--payload`はJSON objectです。上の引用符はPOSIX shell向けです。
Windowsでの引用符処理はshellごとに異なるため、JSONファイルを用意して`--payload-file C:/.../request.json`を使えます。
Pythonから引数配列を渡す場合は次の形を使えます。

```python
import json
import subprocess
import sys

subprocess.run([sys.executable, "-B", "scripts/dev-server.py", "acquire",
                "--root", "C:/minecraft-dev", "--operation-id", "acquire-01",
                "--payload", json.dumps({"instanceId": "persistent-01", "owner": "codex-session-a", "ttlSeconds": 3600})], check=True)
```

成功JSONには`ok`、`operationId`、`result`、失敗には`error.code`と`error.message`を返します。
acquireの結果にあるleaseId、epoch、generationを保存し、renew/releaseへ次のpayloadを渡します。

```json
{"instanceId":"persistent-01","leaseId":"取得結果のleaseId","epoch":1,"expectedGeneration":null}
```

renewでは`ttlSeconds`を追加します。新ownerの`generationBound`はfalseであり、前世代の起動許可ではありません。
変更のdry-runは条件を検査し、lease、epoch、operation記録、参照予約を変更しません。
実行時には条件を再検査するためdry-runの成功は予約ではありません。

## 冪等性・復旧待ち

同じoperationId・同じpayloadの再送は確定した同じ結果を返します。
別payloadは`OPERATION_CONFLICT`です。解放後の旧acquire、次の貸出後の旧releaseなど、
現在のlease/epoch/世代と矛盾する再送は拒否します。結果不明時に新IDで再試行しません。

CLI終了・期限切れでleaseを自動回収しません。期限切れのrenew/releaseも拒否し、後続recoverの確認が必要です。
supervisor再起動では、空いていた枠も含め全枠を`RECOVERY_REQUIRED`へ移します。
現段階のrecoverは未実装なので、DBを手編集して貸出を強行しません。
`shutdown`は監督だけを終了して全枠を復旧待ちにする補助操作です。leaseは解放されません。
Minecraftの停止コマンドではなく、同一rootの監督停止にだけ使います。
shutdownもoperationIdを記録し、他の変更とのID衝突を拒否します。
前回の監督を停止した要求の再送は`STALE_SUPERVISOR`となり、再起動した監督を停止しません。

AVAILABLEのacquireのみ、停止した空枠のreleaseのみを許可します。
writer/child/pending/参照予約が残る場合もrelease不可です。
参照予約は`Registry.reserve_references`/`finish_reservation`を後続エンジンから呼び、
参照集合を整列して短いtransactionで記録します。コピー中にDB transactionを保持しません。
このAPIはまだGCや保存器へ接続されていません。

## OS adapter

- macOS: owner限定dir 0700、socket 0600、接続の両側でpeer UID、flock。
- Windows: owner SIDだけの保護DACL、`PIPE_REJECT_REMOTE_CLIENTS`、byte-range lock。
- 両方: singleton OS lockを監督期間中保持し、instance OS lockを取得した後に短いSQLite transactionを開始。
- IPCは64KiB以下のJSON bytesだけ。pickleの復元、秘密を含むpayloadのログ出力は行いません。
  JSONの解析に失敗する深さの入力は、その接続だけを拒否して監督を継続します。

Windowsのoverlapped I/OはPython 3.12の`_winapi`/`PipeConnection`を使用します。
Pythonのメジャー/マイナーバージョンを変更するときはnativeテストを再実行してください。
同一OSユーザーの非協調的な直接編集や、管理者権限による強制アクセスを完全に防ぐ契約ではありません。
private root外のファイルの権限を、このCLIから変更しません。

実装根拠は[Windows named pipe](https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-createnamedpipew)、
[Apple getpeereid](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man3/getpeereid.3.html)、
[Python 3.12のConnection](https://docs.python.org/3.12/library/multiprocessing.html#connection-objects)です。

## native検証

`.github`リポジトリで各OS別に実行します。

```bash
python3.12 -B -m unittest discover -s scripts -p test_supervisor.py -v
python3.12 -B -m unittest discover -s scripts -p test_contracts.py -v
```

OS自身のIPC・DACL/UID・lock、CLI終了後の監督継続、競合、異常終了後の復旧待ちを確認します。
使い捨てrootと、このテストだけのPython childを使います。既存Minecraftを起動・停止しません。
Windowsで成功してもmacOSの合格とは扱いません。別ユーザー/リモートの実接続、FSの耐久化、
停電相当、実JVM/Managerとのwriter連携は別途確認が必要です。

# データ世代の構築・復元・復旧

固定runtimeとworld/configを同じ世代として切り替える場合に使用します。
[監督・貸出](supervisor-guide.md)、[runtime制御](runtime-control-guide.md)、[snapshot](snapshot-guide.md)を先に確認してください。
復元は起動とは別操作です。旧世代・途中データ・証跡は削除しません。

## 構築と参照切替

すべての操作に`--operation-id`を指定します。tokenはinstanceId、leaseId、epoch、expectedGenerationです。

| 操作 | tokenに追加する必須項目 |
|---|---|
| prepare | artifactId、generationId、acceptEula=true、seed |
| import | artifactId、generationId、acceptEula=true、sourceDirectory、sourceHash、sourceStopped=true、approvedBy、taskId、purpose |
| restore | artifactId、generationId、acceptEula=true、snapshotId |
| reset | artifactId、generationId、acceptEula=true、templateId |
| recover | action（resume/rollback/observe-stop）、targetOperationId、ttlSeconds。observe-stopはexpectedRunId・expectedProofHashも必須 |

prepareは初回の空枠だけです。importは空枠または正常停止済みの貸出枠へ新しい世代を構築し、旧世代を保全します。importは停止確認済みの人間用runを通常コピーします。元runの移動・削除は行いません。restoreは登録済みsnapshot全体を復元します。
resetは`templates/<templateId>.json`が指す不変snapshotへ戻します。空worldの再生成ではありません。
テンプレートの登録・昇格は[テンプレートガイド](template-guide.md)に従います。未登録templateや異なる参照hashは拒否します。
参照契約はschemaVersion=1、templateId、snapshotId、manifestHash、artifactId、validationId、operationIdです。
現在の互換性契約は同一artifact・同一layoutだけです。別artifactは複製枠での互換性validationが接続されるまで拒否します。
acceptEulaは運用者が同意した場合だけtrueにします。

```bash
python3.12 -B scripts/dev-server.py restore --root /absolute/minecraft-dev --operation-id restore-01 --payload-file /absolute/restore.json --timeout-seconds 900
```

正常停止、writer/child不在、lease/epoch/期待世代、runtime/snapshotのhash、容量を検査します。
同じFSの`instances/<id>/staging/<operationId>/`へ全体を通常コピーし、インスタンス固有のport/bind、新しいRCON秘密、EULAを合成します。
ファイルと親directoryを同期し、完成記録と全hashを照合した後だけ、次の順で公開します。

1. `manager/operations/generation-<operationId>/prepared.json`
2. 完成世代を`generations/<generationId>/`へ移動
3. `active.json`を原子的に置換
4. `switched.json`
5. Registryの世代と操作結果を同じtransactionで確定
6. `committed.json`

旧世代は残ります。startはactive/Registry/完成記録/COMMITTEDの一致を要求します。
world/configが実行中に変わるため、完成時の全hashを毎tick検証しません。
runtimeは起動時にも全hashを検査します。
Windowsは同一volumeのMoveFileExW/WRITE_THROUGH、macOSはrename、file fsync/F_FULLFSYNCと親directory fsyncを使用します。
共有違反・同期失敗は保留し、自動kill・削除・容量整理で回避しません。停電耐久性はこの実装やプロセス障害注入だけでは保証しません。

## 復旧

supervisor再起動時は全枠をRECOVERY_REQUIREDへ保留します。
復旧はまず`--dry-run`で確認し、同じ要求とoperationIdで実行します。
targetOperationIdには途中のprepare/import/restore/reset/snapshot操作IDを指定します。
途中操作がない停止枠の再起動照合にはnullを指定します。
未貸出枠はleaseId=nullと現epoch/期待世代を指定し、resumeでAVAILABLEへ戻します。新しいleaseは勝手に発行しません。
期限切れleaseでも同じlease/epochの明示recoverだけがttlSecondsを使って期限を更新できます。

```bash
python3.12 -B scripts/dev-server.py recover --root /absolute/minecraft-dev --operation-id recover-01 --payload-file /absolute/recover.json --dry-run
python3.12 -B scripts/dev-server.py recover --root /absolute/minecraft-dev --operation-id recover-01 --payload-file /absolute/recover.json
```

importのresumeは元runのworldロックを保持し、承認したsourceHashを再照合します。
resumeは完成済み世代の全hashを再照合して切替を完了します。未完成コピーを推測して完成扱いにしません。
rollbackはDB確定前の切替を旧世代へ戻します。初回の部分構築はinstance全体をquarantineへ同一FSで保全移動し、空枠を再利用できます。
DB確定後に別の状態へ戻す場合は、新規restoreを使用します。
公開済みsnapshotのDB登録前障害は、停止元世代と変換後の全ファイル集合を比較して登録を完了できます。
snapshotの要求journal公開前の障害は、DBに保存した要求とlease/epochを照合してrollbackできます。
一致する完成済み`.writing`だけ再公開でき、部分bytesは残したまま別の一時記録へ書き直します。

PID、開始識別、boot、nonce、cwdの正常停止記録を照合し、記録されたJob/process groupの全child不在を確認します。
Java生成後の識別記録欠落、未終了・再利用PID、結果不明writer、待受予約、壊れたDBは解除しません。
起動・停止操作の結果が不明な場合も、正常exitCodeや保存完了を推測せず保留します。
必要な証跡が欠ける状態は自動復旧の対象外で、人間の診断を必要とします。

正常停止・writer終了照合後のreleaseはデータを残して世代をleaseから切り離します。
次のownerはそのままstartできません。明示restore/resetで自身の世代を構築します。

## 検証境界と既存開発データ

関連テストは`test_generation_control.py`、`test_storage.py`、`test_runtime_control.py`、`test_snapshot_control.py`です。
実FSのコピー/参照切替と合成保存証跡の試験であり、実Minecraft保存往復とは区別します。
native親子試験はOS別に実行します。片方の成功は他方の成功ではありません。

実runtime往復は許可済みの使い捨てroot・固定bundleだけで実行します。
`verify_generation.py`は既存root/evidenceを上書きせず、26065/26075/26080（2枠目は各+1）を宣言します。
仮バックアップ設定を使う試験なので、正式バックアップ検証には数えません。

```bash
python3.12 -B scripts/dev_server/verify_generation.py --repo /absolute/task/.github --root /absolute/minecraft-dev/533-test --bundle /absolute/fixed-artifact --evidence /absolute/task/verification/533-test
```

世代完成記録にoperationIdと全ファイル集合、COMMITTED journalの照合を追加しています。
この記録がない旧開発用管理rootの世代はstartできません。互換処理は追加していません。
既存rootを削除・再初期化せず保全し、新しい使い捨てrootで生成し直してください。
GC・実運用導入は本機能の完了とは別です。


## 新しいビルド・管理rootでの初期ワールド作成

実機確認にはOS別初期データを使用し、別root・artifactではtemplate-initを選びます。
新規worldが明示目的の場合だけprepareを使用します。payload・承認・hash照合・実行検証の手順は
[テンプレート運用](template-guide.md#新しいビルド管理rootでの初期ワールド作成)を参照してください。


## 観測した正常終了の復旧

管理stopの記録なしで正常終了したrunは、`recover`の`action=observe-stop`で明示的に照合できます。
Windowsの終了process objectとnamed Jobが読み取れる場合だけ対応し、取得不能・PID再利用・非正常exit・子残存は拒否します。
receiptの成功booleanを根拠にせず、記録PID・開始FILETIME・boot、Job全子不在、既存Mod STOPPED proofの保存/drain/hashを監督自身が検証します。
終了後にcwdや起動commandを含むフルidentityを再取得したとは扱いません。

payloadは既存recoverのlease token・`ttlSeconds`・`targetOperationId=null`に、`expectedRunId`と`expectedProofHash`を加えます。
`expectedProofHash`は対象runの診断`proof.json`のSHA-256です。未完了操作・参照予約・writerがある場合は解除しません。
待受予約はinventoryと起動記録に一致する当該instance/runの全件だけを解除し、別runの予約を消しません。

```json
{
  "instanceId": "persistent-01",
  "leaseId": "取得済みleaseId",
  "epoch": 1,
  "expectedGeneration": "generation-01",
  "action": "observe-stop",
  "targetOperationId": null,
  "ttlSeconds": 120,
  "expectedRunId": "run-取得済みrun識別子",
  "expectedProofHash": "対象proofの64桁SHA-256"
}
```

```bash
python3.12 -B scripts/dev-server.py recover --root /absolute/minecraft-dev --operation-id observe-stop-01 --payload-file /absolute/observe-stop.json --dry-run
python3.12 -B scripts/dev-server.py recover --root /absolute/minecraft-dev --operation-id observe-stop-01 --payload-file /absolute/observe-stop.json
```

全照合後、runの観測由来の終了・保存証跡、LEASED_STOPPED、同leaseのTTL更新、予約解除、監査と冪等結果を1 transactionで確定します。
元のRCON stopRequested/stopAcknowledgedは変更しません。`observedStop`は通常のRCON要求・応答とは別の由来です。
検証失敗やtransaction確定前の失敗ではDBの成功情報や予約解除を残さず、同じoperationId/payloadで再照合できます。
確定後の応答欠落では記録済み結果を返し、完了済み要求の再送はTTLを延長しません。
world・snapshot・artifact・診断を削除せず、`snapshotClean`をtrueにしません。snapshotの全検証は別途必要です。

稼働中の旧監督へCLI更新だけで適用はできません。旧監督と別Registryで並行書込みを行わず、次の順で切り替えます。

1. Git外の読取専用helperで対象の終了processとnamed Jobのhandleを保持し、開始識別・exit0・Job空を確認します。
2. 元DBと停止証跡を保全し、旧監督を正規shutdownしてから新コードの監督を起動します。新規起動で全枠は復旧待ちになります。
3. 新監督へ上記dry-runと同一要求を送り、LEASED_STOPPED・観測証跡・予約解除を確認してreleaseします。必要な空枠も既存recoverでAVAILABLEへ戻します。
4. 全枠AVAILABLE、lease・予約・pending・writer・child不在を確認するまでhelperのhandleを保持します。

`shutdown-idle`も全枠をRECOVERY_REQUIREDへ変更します。AVAILABLE状態を保持したい場合は、Minecraftを停止したまま管理監督をidle待機で維持します。
helperはprocess/Jobの読取handleだけを保持し、終了・引継ぎ・Registry更新を実行しません。handleを失った場合は成功を推測せず停止して照合します。

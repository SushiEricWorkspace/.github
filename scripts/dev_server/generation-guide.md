# データ世代の構築・復元・復旧

固定runtimeとworld/configを同じ世代として切り替える場合に使用します。
[監督・貸出](supervisor-guide.md)、[runtime制御](runtime-control-guide.md)、[snapshot](snapshot-guide.md)を先に確認してください。
復元は起動とは別操作です。旧世代・途中データ・証跡は削除しません。

## 構築と参照切替

すべての操作に`--operation-id`を指定します。tokenはinstanceId、leaseId、epoch、expectedGenerationです。

| 操作 | tokenに追加する必須項目 |
|---|---|
| prepare | artifactId、generationId、acceptEula=true、seed |
| restore | artifactId、generationId、acceptEula=true、snapshotId |
| reset | artifactId、generationId、acceptEula=true、templateId |
| recover | action（resume/rollback）、targetOperationId、ttlSeconds |

prepareは初回の空枠だけです。restoreは登録済みsnapshot全体を復元します。
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
targetOperationIdには途中のprepare/restore/reset/snapshot操作IDを指定します。
途中操作がない停止枠の再起動照合にはnullを指定します。
未貸出枠はleaseId=nullと現epoch/期待世代を指定し、resumeでAVAILABLEへ戻します。新しいleaseは勝手に発行しません。
期限切れleaseでも同じlease/epochの明示recoverだけがttlSecondsを使って期限を更新できます。

```bash
python3.12 -B scripts/dev-server.py recover --root /absolute/minecraft-dev --operation-id recover-01 --payload-file /absolute/recover.json --dry-run
python3.12 -B scripts/dev-server.py recover --root /absolute/minecraft-dev --operation-id recover-01 --payload-file /absolute/recover.json
```

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

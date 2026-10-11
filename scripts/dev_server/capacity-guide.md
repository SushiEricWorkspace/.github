# Windows受入試験の容量・メモリ計測

別媒体復元試験後の停止rootで、persistent二枠とGameTest一枠の初期設定を検討するときに使います。
[バックアップ手順](backup-guide.md)と[総合受入条件](acceptance-guide.md)を先に確認してください。
これは運用初期化・自動調整ではなく、承認済みの使い捨てデータによる測定です。

## 実行条件

Windows、64bit Python 3.12、既存のprivate snapshot、同じ固定artifactを使用します。
元の試験supervisorは正常終了済みで、二枠とも未貸出・RECOVERY_REQUIREDである必要があります。
正常終了後に監督を再起動するための保留を、管理CLIのrecoverで照合します。保留を手で解除しません。
既存の運用rootや結果不明の試験rootには使用しないでください。
二枠の起動、snapshotからの新世代作成、GameTestの起動について事前許可が必要です。

```powershell
python3.12 -B scripts/dev_server/verify_capacity.py `
  --repo C:/absolute/task/.github `
  --root C:/absolute/minecraft-dev/acceptance-trial `
  --evidence C:/absolute/task/verification/capacity `
  --operator operator-name --snapshot save-roundtrip-v1-private
```

証跡先は管理rootと独立した新規directoryにします。既存の世代を上書きせず、固定の計測用世代を各枠へ作成します。
同じrootでの再実行を再試行手段として使わず、途中失敗の証跡・予約を残して復旧判断を求めます。
正常系ではGameTestの結果と所有終了を確認し、両永続枠を正常stop・releaseしてsupervisor終了まで待ちます。
失敗時の自動kill・強制解除・削除はありません。

## 測定値の意味

- `logicalBytes`は独立した通常ファイルの長さの合計です。
- `fileAllocatedBytes`は[FILE_STANDARD_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_standard_info)のAllocationSizeの合計です。[GetFileInformationByHandleEx](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex)で取得し、FS metadataは含めません。
- メモリは3 JVMのPIDだけでなくnative所有識別を取得前後に照合し、working setとprivate commitを別々に集計します。取得不能・終了と重なった標本は除外し、その件数と理由を記録します。
- `peakSumWorkingSetBytes`と`peakSumPrivateCommittedBytes`は採用標本の最大値です。最悪負荷・将来の増加を保証しません。working setの合計には共有ページの重複があり得ます。
- restore/start時間には完全性・所有識別などの照合を含みます。復元・起動だけの理論値ではありません。

report.jsonとsupervisor.log、root内の実行・停止・GameTest証跡を保全します。
小さなfixtureの実測から大きなワールドの必要量を断定しません。保護された旧世代・snapshot・失敗データ・固定artifactの保持、
二枠の配備とstaging、起動後増加余裕を見積もり、物理空き容量も確認してquota/reserveを運用者へ提案します。
試験の32GiB/1GiBを正式設定へ自動転用せず、運用root・ポート・バックアップ先と一緒に承認を得ます。

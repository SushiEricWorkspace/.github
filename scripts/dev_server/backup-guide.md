# 別媒体バックアップの復元訓練

正常停止した分離テスト環境を別媒体へコピーし、保存値を復元確認するときに使用します。
Python 3.12と、[固定runtime](runtime-export-guide.md)に含まれる管理fixtureを使用します。
これは受入試験用ハーネスであり、自動バックアップや運用の自動有効化ではありません。

## 媒体と許可

- 管理rootと異なる物理ディスクを使います。内蔵SSDでも別の物理ディスクなら対象です。同じディスク上の別partitionは別媒体ではありません。
- 運用者、管理root、保存先の絶対パス、起動する二枠のportを明示して許可を得ます。Windowsでは`Get-Partition`と`Get-Disk`でdriveと物理ディスクの対応を確認します。
- ハーネスの別volume検査だけでは別物理媒体を証明しません。媒体・所有権・容量は運用者が確認します。
- 同じPC内の別ディスクはPC全体の故障、盗難、両媒体の同時障害を防ぎません。停電耐性や媒体故障の実験とも区別します。
- バックアップには認証設定や非公開データが含まれます。新規保存先のrootと子要素は所有者限定にし、共有・公開しません。

## 新規rootでの手順

入力bundle、試験root、証跡、バックアップ保存先は独立した絶対パスを指定します。
root・証跡・保存先が存在する場合は拒否します。保存先の親だけを事前に用意します。

```bash
python3.12 -B scripts/dev_server/verify_templates.py \
  --repo /absolute/task/.github \
  --root /absolute/minecraft-dev/acceptance-trial \
  --bundle /absolute/fixed-artifact \
  --evidence /absolute/task/verification/backup \
  --approved-by operator-name --task-id acceptance-trial \
  --fixture save-roundtrip-v1 \
  --minecraft-base-port 26105 --rcon-base-port 26115 --management-base-port 26120 \
  --backup-destination /absolute/separate-volume/acceptance-trial
```

Windowsでは各パスをdrive付き絶対パスにし、Python 3.12の実行ファイルを指定します。
二枠目のportは各baseの+1です。既存portを止めたり別portへ自動変更したりしません。
`--backup-destination`は`--restart-only`と併用せず、templateの昇格も行いません。
EULAへの同意と、fixture生成のためのサーバー起動許可が前提です。

1. 空rootを試験用inventoryで初期化し、固定bundleをコピーします。32GiBのquotaと1GiBのreserveはハーネスの試験値であり、正式運用の推奨値ではありません。
2. 最初の枠を起動し、現在のMod/Common APIでfixtureを生成します。正常停止証跡とwriter不在を照合してprivate snapshotを作成します。
3. supervisorを正常終了させ、その終了と宣言portの非使用を確認します。管理root全体を保存先へ通常コピーし、ファイル集合・size・SHA-256・空directory集合を再照合します。
4. 元rootを同じFSの`<root名>-before-backup-restore`へ保全移動し、別媒体のコピーから元と同じ絶対パスへ復元します。元データを削除しません。
5. 復元したrootでsupervisorを起動し、両枠を明示recoverします。正常停止証跡内の絶対パスを維持するため、rootの移設やDBのパス書換えは行いません。
6. snapshotをverifyし、二枠目の新しい世代へrestoreします。実APIのfixture再読込を確認し、正常停止・release・supervisor終了まで記録します。

`copy_verified_tree`は停止を判定しません。ハーネスの正常停止とsupervisor終了の手順を省略して呼び出してはいけません。
元root・バックアップ・復元rootは独立した通常ファイルです。hardlink・リンク・reparse point・特殊ファイルは拒否します。

## 証跡と失敗時

証跡directoryの`report.json`、各supervisorのlog、root内の停止・snapshot・世代journalを保全します。
reportにはコピーのtreeHash、論理bytes、コピー・復元・起動時間、実API結果と正常停止結果を記録します。
この保存往復だけで総合受入、同時メモリ、quotaの妥当性、Managerの実操作まで合格とは扱いません。

途中失敗時は元root、保全root、バックアップ途中物、復元途中物、予約をそのまま残します。
新しいoperationIdでの自動再送、kill、自動削除、DB手編集での解除を行いません。
同じrootでのハーネス再実行は既存データとして拒否されます。
残る自身のJVM・supervisorは証跡とOS所有識別を照合し、[復旧手順](generation-guide.md)と運用者の指示に従います。
保全rootは起動用ではありません。バックアップと保全物の削除は別の許可が必要です。

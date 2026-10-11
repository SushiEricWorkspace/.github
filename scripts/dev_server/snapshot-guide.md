# 正常停止したデータのスナップショット

固定runtimeの正常停止後に、全ゲームデータをprivate snapshotへ保存する手順です。
事前の貸出は[監督ガイド](supervisor-guide.md)、起動・正常停止は[runtime制御ガイド](runtime-control-guide.md)、
Managerの終了は[writerガイド](writer-guide.md)を参照してください。Python 3.12を使用します。

## 作成と検証

正常停止済みの同じlease/epoch/generationを指定します。`LEASED_STOPPED`、停止要求・応答・保存成功、
JVMと全childの終了、writerが全てCLOSED、writer操作・待受予約不在を再照合します。
期限切れ、結果不明writer、監督再起動後の保留状態を正常停止へ変換しません。

作成要求をJSONファイルへ用意します。取得済みleaseIdとepochを使い、operationIdとsnapshotIdは新規の値にします。

```json
{
  "action": "create",
  "instanceId": "persistent-01",
  "leaseId": "<取得済みleaseId>",
  "epoch": 1,
  "expectedGeneration": "generation-01",
  "snapshotId": "snapshot-01"
}
```

```bash
python3.12 -B scripts/dev-server.py snapshot --root /absolute/minecraft-dev --operation-id snapshot-create-01 --payload-file /absolute/create.json --dry-run
python3.12 -B scripts/dev-server.py snapshot --root /absolute/minecraft-dev --operation-id snapshot-create-01 --payload-file /absolute/create.json --timeout-seconds 300
python3.12 -B scripts/dev-server.py snapshot --root /absolute/minecraft-dev --operation-id snapshot-verify-01 --payload '{"action":"verify","snapshotId":"snapshot-01"}' --timeout-seconds 300
```

WindowsではPython 3.12の実行パスとWindows絶対パスを指定し、shellの引用符処理を避けるためverifyにも`--payload-file`を使用できます。
dry-runは参照・停止証跡・容量を検査しますが、作業ディレクトリ・予約・DB記録を作りません。
作成の再送は同じoperationIdとpayloadだけに限定し、登録済みsnapshotも再検証します。
別run・別世代の停止結果へ古い作成応答を適用しません。

## 保存範囲と変換

snapshotには`data/`、`manifest.json`、`COMPLETE`を置きます。実効world全体を再帰収集し、
ネザー・エンド・カスタムdimension・未知通常ファイルを含めます。`world/session.lock`、logs、crash-reportsは含めません。
アカウントのops/whitelist/banはprivateデータとして全て保存します。
バニラのusercache.jsonは再生成される認証キャッシュとして除外し、Fabricが生成する空mods directoryは許容します。
bundle外のModがmods directoryに存在する場合は未対応構成として拒否します。

configは現行のSushiEricServerMod保存契約に限定し、player_data・item_data・ore_data・shop_data・recipe_data・shop_history、
placed_ores.yml・placed_special_blocks.yml・manager-compat.jsonを保存します。各データdirectory内の通常ファイルを個別の既知ファイル名へ限定しません。
Mod config.ymlはmining.ore_revival_secondとmoney.history_save_interval_secondの数値を保存します。
Management config.ymlはportを除き`{}`に変換します。
server.propertiesは明示されたバニラのゲーム設定を保存し、固定port・bind・RCON秘密・管理サービス設定を除きます。
未知項目・継続行・未知YAML形式・未分類config・外部root・DBは推測でコピーせず拒否します。
リンク/reparse point・特殊ファイル・hardlink・Windowsで解釈が曖昧になる名前を拒否します。

全ファイル集合・size・SHA-256、data-layout、artifact参照、変換版、nonce付き正常停止結果をmanifestへ記録します。
runtimeをsnapshotの可変データへコピーせず、完成artifactへの参照を保持します。
容量不足でも自動GCしません。sourceとsnapshotは通常コピーによって分離されます。

## 公開と障害保全

instance/deployment lockと参照予約を保持し、DB transaction外でコピーします。
コピー中の起動・停止・writer書込みは同じinstance lockで排除し、各コピーと公開前に停止・writer・leaseを再照合します。
元のファイル集合・hashを最後に再収集して変化を拒否します。同一OSユーザーの非協調的な直接編集の完全防止は保証しません。

新規ファイルをflushし、macOSではF_FULLFSYNCと親directoryのfsync、WindowsではファイルfsyncとMoveFileExWのWRITE_THROUGHで公開します。
全データとmanifestを検証してからCOMPLETEを最後に書き、同一FSのsnapshot名へ公開し、Registryへ登録します。
これは停電耐性の試験結果ではありません。対象OS/FSの適合と運用導入は#537で別途確認します。
Windowsの公開APIは[MicrosoftのMoveFileExW仕様](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw)に従います。

`manager/operations/snapshot-<operationId>/`にrequested/prepared/published/committedの新規JSON証跡を順番に残します。
公開とDB登録の間で失敗しても、未登録snapshotをverifyの成功対象として扱いません。
欠損・余分なファイル・hash改変・COMPLETE不一致を検出します。
失敗時は元世代、staging、公開済みデータ、journal、参照予約を保全してRECOVERY_REQUIREDにします。
復旧・restore・resetは[世代ガイド](generation-guide.md)に従います。promote・GCは後続Issueの範囲です。
DB手編集や自動削除で進めません。

Registryは形式4です。旧開発用rootを移行・削除せず拒否するため、検証には新しい空rootが必要です。

## 検証

```bash
python3.12 -B -m unittest discover -s scripts -p test_snapshot_control.py -v
python3.12 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

合成runtime/停止証跡と使い捨て実FSによる競合・コピー失敗・秘密除去・hash検証を実施します。
実Minecraft全dimensionの保存、macOS/Windowsの個別試験、別媒体backup、停電耐性はそれぞれ実行結果を分けて記録します。

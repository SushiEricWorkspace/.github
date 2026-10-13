# 不変テンプレートの作成と正式登録

`smoke-v1`、`save-roundtrip-v1`を新規生成・別枠検証して、運用者の明示承認後に登録する手順です。
貸出と停止は[監督ガイド](supervisor-guide.md)、private保存は[snapshotガイド](snapshot-guide.md)、
復元とresetは[世代ガイド](generation-guide.md)を参照してください。Python 3.12を使用します。

## fixtureの新規生成

現行Common/Minecraft APIで生成する`se managedfixture`を含む固定bundleを用意します。
このコマンドは管理bundle起動時のコンソール専用です。通常のrunServerでは登録しません。
生成元instanceを固定seedでprepareし、start後にRCONで次のどちらかを実行します。

```text
se managedfixture create smoke-v1
se managedfixture create save-roundtrip-v1
```

seedはそれぞれ52452501、52452502です。fixtureの既存ファイルがある場合は上書きしません。
実NBT、Modプロフィール、Commonのアイテム定義、dimension配下のマーカー、未知通常ファイルを
生成・再読込し、instance/generation/start nonceとデータhashをdiagnosticsへ記録します。
マーカーの存在だけではカスタムdimensionのゲーム動作が確認できたとは扱いません。

正常stop後、private snapshotをcreateします。既存runのworldを自動取り込みしません。

## 共有候補の作成

snapshotコマンドに次のpayloadを指定します。全操作でoperationIdは新規の値を使用します。
`--payload-file`の使い方はsnapshotガイドと同じです。

```json
{
  "action": "candidate",
  "instanceId": "persistent-01",
  "leaseId": "<生成元のleaseId>",
  "epoch": 1,
  "expectedGeneration": "fixture-source",
  "sourceSnapshotId": "fixture-private",
  "snapshotId": "fixture-candidate"
}
```

生成元の同じ正常停止runとleaseを再照合します。候補は別ファイルへ通常コピーし、private保存を変更しません。
候補のops、whitelist、banは空リストにします。固定port・RCON秘密・管理接続設定は持ち越しません。
未分類設定、リンク、DB、秘密設定を推測してコピーしません。visibilityはshared-templateです。

## 別instanceへの復元と検証

候補を別instanceへ同じartifactでrestoreし、startします。RCONで同じfixtureのverifyを実行します。

```text
se managedfixture verify smoke-v1
```

正常stop後、snapshotのvalidateを実行します。

```json
{
  "action": "validate",
  "instanceId": "persistent-02",
  "leaseId": "<復元先のleaseId>",
  "epoch": 1,
  "expectedGeneration": "fixture-restored",
  "snapshotId": "fixture-candidate",
  "fixture": "smoke-v1",
  "validationId": "fixture-validation"
}
```

新規prepareの固定seed、restoreの登録journal、全コピーhash、現行APIの期待値・hash、
別枠のnonce、保存成功、JVM/child終了、writer不在を照合します。
passedChecksを自己申告する入力はありません。合成テストのJSONを実サーバー証跡として使いません。
候補manifestへ検証結果を追記せず、別の不変監査ファイルへ記録します。

## 運用者承認と昇格

人間の承認を得た後だけ、snapshotのpromoteを実行します。

```json
{
  "action": "promote",
  "instanceId": "persistent-02",
  "leaseId": "<復元先のleaseId>",
  "epoch": 1,
  "expectedGeneration": "fixture-restored",
  "snapshotId": "fixture-candidate",
  "templateId": "smoke-v1",
  "validationId": "fixture-validation",
  "approvedBy": "<inventoryのoperator>",
  "taskId": "<実際のtask-id>",
  "purpose": "<使用目的と承認範囲>"
}
```

approvedByは人間の承認の代替ではありません。同一OSユーザーの非協調編集に対する認証境界ではなく、
協調する運用者の監査契約です。journalにはRegistryで照合したownerも固定して残します。
テンプレート名は標準2種類とOS別持込2種類の名前・版に限定し、同名上書き、latest別名、自動昇格は拒否します。
resetは完成した昇格登録・検証hash・候補hashを照合します。異なるartifactへ自動互換としません。
旧開発用の手書き参照ファイルは登録済みとして使用できず、新規検証・昇格が必要です。

## 途中障害

候補、private保存、監査、journal、予約を保全します。強制停止や保存失敗を正常停止へ変換しません。
validate/promoteの公開後障害は、同じlease/epoch/generation/ownerと元の承認・証跡・hashが一致する場合だけ、
recoverのresumeで完成できます。targetOperationIdは失敗した操作IDを指定します。
dry-runで照合してから実行してください。公開物のrollback削除、未公開証跡の推測生成はしません。
一致しない場合はそのまま保全し、診断を依頼します。保留解除や別名での再試験を独断で行いません。

## 人間用runを正式テンプレートへ持ち込む

Windowsは`initial-world-windows-v1`、Macは`initial-world-macos-v1`を使用します。
それぞれのOSにあるrunを独立して取り込みます。標準fixtureの固定seed/API検証とは別の検証契約です。
テンプレートは各OSの私有管理rootに登録し、リポジトリへworld・プロフィール・秘密を公開しません。

持込元を正常停止し、JVM・子プロセス・外部writerの終了を確認してください。
`sourceStopped=true`はこの運用者確認を表し、worldロックだけで正常保存したと推測しません。
元runは管理rootと重ならない絶対パスが必要です。空instanceまたは正常停止済みの貸出instanceへ次のpayloadでimportします。既存世代がある場合はexpectedGenerationにそのIDを指定します。

```json
{
  "instanceId": "persistent-01",
  "leaseId": "<持込先のleaseId>",
  "epoch": 1,
  "expectedGeneration": null,
  "artifactId": "<固定artifactのhash>",
  "generationId": "initial-world-source",
  "acceptEula": true,
  "sourceDirectory": "C:/absolute/SushiEricServerMod/run",
  "sourceHash": null,
  "sourceStopped": true,
  "approvedBy": "<inventoryのoperator>",
  "taskId": "<実際のtask-id>",
  "purpose": "reset時の初期world"
}
```

MacではsourceDirectoryをMacの絶対パスにしてください。
まず`dev-server.py import --dry-run`へ送信し、返ったsourceHashとplatform・fileCountを確認します。
payloadのsourceHashを返った値へ置き換え、同じoperationIdでimportを実行します。
実行時も既存session.lockを非ブロッキングで保持し、コピー前後・参照切替前の元データhashを照合します。
元runへのファイル作成、設定変更、移動、削除はありません。

worldの全dimension、プレイヤー進捗、Mod保存データをコピーします。
ops/whitelist/banは空にし、固定port・RCON秘密を置き換えます。
session.lock、logs、crash-reports、usercache、通常ファイルの.DS_Storeは除外します。
空resourcesだけを許可し、未知root・設定・非空resources・外部DB・リンクは拒否します。

取り込んだ管理世代をstartして正常stopし、private snapshotと共有candidateを作成します。
同じartifactで別instanceへrestoreし、startして正常stopしてください。
上記validateのfixtureとpromoteのtemplateIdにOS別IDを指定します。managedfixtureコマンドは不要です。
IMPORT_SOURCEは登録済みimport journal・元hash・OS・初期コピーhash、IMPORT_RESTOREは独立復元journal・候補全hash、
IMPORT_SAVEは両管理世代の別nonce・保存成功・JVM/child終了の照合を表します。
ゲーム内プロフィールやアイテムの個別API値を検証したという意味ではありません。

昇格後は既存resetでそのIDを選択できます。同名の上書きは拒否します。
途中障害は元runとstagingを保全します。完成コピーは元hashの一致したresume、未完成コピーは明示rollbackで扱います。
この検証は実運用バックアップ、別媒体、停電耐性、非協調writerの排除を証明しません。

## 取り込み経路の検証

`test_import_control.py`は元run保全、秘密除外、途中変更の拒否、障害復旧、独立復元・昇格・resetを検証します。
`test_import_lock.py`は実JDK 21のFileChannelとPythonの排他を双方向で確認します。
実JDKのjava絶対パスを`SUSHIERIC_TEST_JAVA21`へ指定して実行してください。未指定時はskipです。
この合成データの試験を実worldのロード・保存成功として扱いません。

## 実データの確認（2026-10-13、Issue #31）

Windowsのwindows-managedとMacのhuman-16-macosへ、各自の既存runから独立して登録しました。
Windowsは119ファイル・15,626,822 bytes、Macは30ファイル・2,291,295 bytesを取り込みました。
両OSで管理起動・正常停止、private snapshot、共有候補、別instanceへのrestore・起動・正常停止、
validate・promote、登録テンプレートからのreset・再起動・正常停止まで成功しました。
元runのsourceHashは取り込み前後で一致し、旧世代・snapshot・診断を保全しています。
最後に貸出を返却し、監督を正常終了しました。証跡は各rootのmanager/audit/issue-31-registration/report.jsonです。

Windowsの関連74件（1skip）と104件（2skip）、最終import13件、実Java排他2件は失敗なしです。
Macは同じ関連範囲の184件中183成功・1skipでした。Java排他の双方向試験を含みます。
標準fixtureの個別ゲームAPI検証、実運用バックアップ、別媒体の確認としては数えません。
登録テンプレートは、Windowsが4051b6a4c2a12cb4f36166f69246cfeb05a65f818f49ae5f006a0fcdaa49b305、
Macがbe516b52b40ae02ccf70fa69c0cb970c81443a1cfa30510c7aa67febb4b9a72aの同一固定artifactに限りresetできます。


## 新しいビルド・管理rootでの初期ワールド作成

実機確認サーバーの初期データにはOS別の登録テンプレートを使います。同じroot・artifactではreset、
新しいrootまたはartifactでは`template-init`を使い、空worldが明示目的の場合だけprepareを使います。
テンプレート不足・不一致時にprepareへ切り替えません。

WindowsのsourceRootは`C:/Users/ryuma/IdeaProjects/SushiEricWorkspace/minecraft-dev/windows-managed`、
templateIdは`initial-world-windows-v1`です。Macは`/Users/ryuman/IdeaProjects/SushiEricWorkspace/minecraft-dev/human-16-macos`、
`initial-world-macos-v1`です。元rootのCLI/監督も本版へ更新し、GCはdeployment lockを共有する入口から実行してください。

template-initは空の貸出枠だけを対象とし、TOKEN、artifactId、generationId、acceptEula=trueと、
sourceRoot、templateId、templateHash、approvedBy、taskId、purposeを指定します。
approvedByは作成先inventoryのoperator、purposeは新ビルドへの初期データ使用の承認範囲です。
`templateHash=null`でdry-runを行い、返ったtemplateHash・sourceArtifactId・snapshotIdを確認して、
templateHashを設定した同じoperationIdで実行します。CLIは`dev-server.py template-init --root TARGET --payload-file PAYLOAD --operation-id ID`です。

承認済み昇格と検証、snapshot全hash、同じOS・Minecraft版・data-layoutを確認して通常コピーします。
元のworld・プレイヤー進捗・Mod保存データを引き継ぎ、port・秘密・管理設定・EULAを対象枠へ再生成します。
コピー元のsnapshotとテンプレートは変更せず、途中障害は既存recoverのresume/rollbackで扱います。
別Modビルドとの保存互換性を保証する操作ではありません。起動後に対象機能・保存データを確認し、正常stopの成功まで検証してください。
新ビルドのtemplateを自動昇格せず、reset/restoreの同一artifact制約は維持します。


## template-initの実データ確認（2026-10-13、Issue #35）

Windowsはwindows-managedの初期テンプレート（artifact 4051b6a4…）から、専用rootへ別Modビルド
921fd7e9…で作成しました。Macはhuman-16-macosの初期テンプレート（be516b52…）から
minecraft-dev/35-macへ、別固定artifact 799962dd…で作成しました。
作成直後のworld・Mod保存データはWindows113ファイル、Mac25ファイルでコピー元snapshotと一致しました。
両OSで実起動、Mod保存成功と正常終了、snapshot、GC plan、返却、監督終了まで確認しました。
元templateHash・snapshot manifestHashは維持されています。証跡は検証rootのmanager/audit/issue35-verification/report.jsonです。

新しい経路10件とGC23件はWindows31成功・2skip、Mac33成功です。
関連する世代・template・監督65件も失敗なし（両OSとも1skip）で、修正後の重点レビューに追加指摘はありません。
Macは既存の使い捨て検証rootのbackup設定・operatorを継承しており、別媒体バックアップの運用受入には数えません。

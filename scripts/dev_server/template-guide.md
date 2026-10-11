# 固定fixtureから不変テンプレートを作る

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
テンプレート名は初期2種類の名前と版に限定し、同名上書き、latest別名、自動昇格は拒否します。
resetは完成した昇格登録・検証hash・候補hashを照合します。異なるartifactへ自動互換としません。
旧開発用の手書き参照ファイルは登録済みとして使用できず、新規検証・昇格が必要です。

## 途中障害

候補、private保存、監査、journal、予約を保全します。強制停止や保存失敗を正常停止へ変換しません。
validate/promoteの公開後障害は、同じlease/epoch/generation/ownerと元の承認・証跡・hashが一致する場合だけ、
recoverのresumeで完成できます。targetOperationIdは失敗した操作IDを指定します。
dry-runで照合してから実行してください。公開物のrollback削除、未公開証跡の推測生成はしません。
一致しない場合はそのまま保全し、診断を依頼します。保留解除や別名での再試験を独断で行いません。

## 人間用worldの手動持込

人間用runは移動・削除しません。持込元を正常停止し、全writerの完了とプロセス終了を確認して、
元の保存データと設定・使用artifactを保全してください。稼働中コピーや強制停止後のコピーをclean扱いしません。
承認した新規使い捨て領域へ通常コピーし、秘密・外部root・accountlistsを分類してから別枠で検証します。
標準2テンプレートは固定seedの新規prepareが必須なので、この持込worldを標準fixtureとして昇格できません。
持込world用の期待値・用途・検証方式を別途確定し、既存worldへの自動取り込みを追加しないでください。

この検証は実運用バックアップ、別媒体、停電耐性、非協調writerの排除を証明しません。

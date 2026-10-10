# 分離テスト環境の契約

タスクのソースworktreeを作成・引き継ぎ・削除する場合は[worktreeガイド](worktrees-guide.md)を参照してください。

[ServerMod #525](https://github.com/SushiEricWorkspace/SushiEricServerMod/issues/525)の契約・fixtureを管理します。
確定設計は[#524](https://github.com/SushiEricWorkspace/SushiEricServerMod/issues/524)、
保存root/writerの接続表・検証境界はServerModの`docs/core/test-environment-guide.md`にあります。

## 検証

`.github`リポジトリ直下で、Python 3.12を使用します。外部依存はありません。

```bash
python3.12 -B -m unittest discover -s scripts -p test_contracts.py -v
```

`contracts-v1.json`は専用のcontractFormat=1であり汎用JSON Schemaではありません。
definitions/contractsをrefで参照し、objectのpropertiesは全て必須・未知項目禁止です。
型、enum、minimum/maximum、minLength、pattern、minItems/maxItems、items、パスformatを
`contracts.py`で検査し、項目間制約も同じモジュールへ集約します。
instanceは静的割当の契約です。lease/state/epoch等のRegistry契約はsupervisor実装時に追加します。
runtime/manifest/validationのhash・nonceは形式検査だけでは証跡になりません。

`fixtures-v1.json`は固定seed/UUIDと必要な最小定義・期待値です。
`fixtures.py`は架空hash・nonce・ファイル内容から検証文書を作るだけで、
Minecraftワールド、NBT、独自定義YAML、JARを実生成しません。
`settings_expectations`も入力・可搬結果・復元先注入の固定期待値であり、実際の変換器ではありません。
実fixtureは後続harnessが現在のCommon/Minecraft APIで生成します。
合成文書をruntime登録・snapshot公開・昇格の実証跡に使用しないでください。

契約検証はコピー/削除/設定変換/ポート予約/JVM起動を実行しません。
管理CLI `scripts/dev-server.py`も未実装です。既存worktreeやrun/への操作は行いません。

## テスト結果の扱い

契約テストで確認するのはA08/A09/A20/A23/A24に対する入力/期待値・拒否条件です。
実NBTの保存往復、設定解析・秘密除去/注入、実FSのリンクや差替え、別媒体、ポート占有、
macOS 15の耐久化・停電耐性は後続試験に残します。
運用の初期対象はmacOS 15/Python 3.12/JDK 21。Windowsで単体検証しても運用適合とは扱いません。

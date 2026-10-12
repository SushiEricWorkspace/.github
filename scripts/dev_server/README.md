# 分離テスト環境の契約

タスクのソースworktreeを作成・引き継ぎ・削除する場合は[worktreeガイド](worktrees-guide.md)を参照してください。
固定した開発runtimeを出力する場合は[runtime出力ガイド](runtime-export-guide.md)を参照してください。
宣言設定・貸出Registry・supervisorを扱う場合は[監督・貸出ガイド](supervisor-guide.md)を参照してください。
固定bundleの初回prepare・起動停止は[runtime制御ガイド](runtime-control-guide.md)を参照してください。
管理接続の固定設定・短命の貸出引継ぎは[接続プロファイルガイド](connection-profile-guide.md)を参照してください。
Managerの管理接続と終了証明は[管理writerガイド](writer-guide.md)を参照してください。
正常停止した全データの保存・完全性検証は[snapshotガイド](snapshot-guide.md)を参照してください。
世代の構築・restore/reset・障害復旧は[世代ガイド](generation-guide.md)を参照してください。
固定fixtureの生成・独立検証・明示昇格は[テンプレートガイド](template-guide.md)を参照してください。
固定bundleのGameTestと同世代保存往復は[GameTestガイド](gametest-guide.md)を参照してください。
参照保護・容量検査・明示回収は[GCガイド](gc-guide.md)を参照してください。
正常停止後の別媒体コピーと実保存値の復元訓練は[バックアップガイド](backup-guide.md)を参照してください。
導入前の条件・OS別証跡の整理は[総合受入ガイド](acceptance-guide.md)を参照してください。
Windowsの停止試験rootによる容量・三枠同時メモリ計測は[計測ガイド](capacity-guide.md)を参照してください。

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
instanceは静的割当の契約です。lease/state/epoch等はRegistryとsupervisorが管理します。
runtime/manifest/validationのhash・nonceは形式検査だけでは証跡になりません。

`fixtures-v1.json`は固定seed/UUIDと必要な最小定義・期待値です。
`fixtures.py`は架空hash・nonce・ファイル内容から検証文書を作るだけで、
Minecraftワールド、NBT、独自定義YAML、JARを実生成しません。
`settings_expectations`も入力・可搬結果・復元先注入の固定期待値であり、実際の変換器ではありません。
実fixtureは管理bundle専用の`se managedfixture`が現在のCommon/Minecraft APIで生成・再読込します。
合成文書をruntime登録・snapshot公開・昇格の実証跡に使用しないでください。

契約検証はコピー/削除/設定変換/ポート予約/JVM起動を実行しません。
管理CLI `scripts/dev-server.py`の初期化・監督・貸出は実装済みです。
固定bundleの初回prepare・JVM起動停止・Mod保存/終了証跡照合を実装しています。
協調Managerの世代固定writerを登録し、正常停止・writer不在を固定したprivate snapshotの作成・検証を行います。
prepare/restore/resetの世代構築、固定fixtureの検証・明示昇格、recover、参照照合付きGCを実装しています。
管理外の非協調writerや停電耐性を証明せず、総合受入前の運用導入は行いません。
既存worktreeやrun/への操作は行いません。

## テスト結果の扱い

契約テストで確認するのはA08/A09/A20/A23/A24に対する入力/期待値・拒否条件です。
実NBTの保存往復、設定変換、実FS、別媒体、ポート占有の結果は[受入記録](acceptance-results.md)と各検証証跡で区別します。
契約テストの成功だけでこれらの実試験や停電耐性を証明しません。
運用の初期対象はmacOS 15とWindows、Python 3.12/JDK 21です。
各OSのnative IPC・lock試験と運用適合試験を分け、片方の成功をもう片方の証明にしません。

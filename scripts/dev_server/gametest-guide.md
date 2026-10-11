# 固定bundleの分離GameTest

初期化済みの管理rootとsupervisorで、GameTestの合否・終了時保存・所有JVM終了を照合する手順です。
[監督・貸出](supervisor-guide.md)、[runtime出力](runtime-export-guide.md)を先に確認してください。
Python 3.12を使用します。実サーバー試験とSSH接続は実行前の人間の許可が必要です。

## 実行

完成したbundleを管理rootの`artifacts/<artifactId>/`へ配置します。
`dev-server.py gametest`へ、次のJSONを`--payload-file`で渡します。

```json
{
  "runId": "gametest-example-01",
  "owner": "codex-session-example",
  "artifactId": "<64桁SHA-256>",
  "timeoutSeconds": 240,
  "acceptEula": true
}
```

```bash
python3.12 -B scripts/dev-server.py gametest --root /absolute/minecraft-dev --operation-id gametest-example-01 --payload-file /absolute/request.json --timeout-seconds 300
```

EULAへの同意は運用者の許可後だけ指定します。`--dry-run`は条件を検査し、実行領域やJVMを作りません。
同じoperationIdと同じpayloadの再送は記録済み結果を返します。失敗した実行の再送でもJVMを再生成しません。
再試験では新しいrunIdとoperationIdを明示し、元の失敗証跡を残します。結果不明・所有確認未完了の実行を自動再試行しません。

`temporary/<runId>/`にworld/config/launchを、`diagnostics/`に要求・プロセス識別・標準出力・Mod証跡・結果を保存します。
JUnit XMLはrun直下の`report.xml`です。実効seed、artifact、template未使用（null）、所有者、終了コードを結果・Registryへ記録します。
永続枠を使わず、inventoryの`gameTestConcurrency`（通常1、負荷試験時2）をDBで予約します。
未確定・復旧待ちの実行も枠を占有します。監督再起動で未確定の実行を再生成しません。
終了したJVMだけでなく所有Job/groupの子の終了まで確認します。期限超過・未予約待受・識別不一致では保全し、自動killしません。

この入口は待受なし・Management API無効のprofile専用です。通信試験用の待受を追加する入口はありません。
その試験には既存のinventory範囲とendpoint予約を使用する実装を先に追加し、宣言外の待受を黙認しないでください。

## 合否と保存証跡

`testsPassed`はJVMの終了コード0、空でないJUnit XML、失敗・error・skipなし、宣言件数との一致が必要です。
XML欠損や壊れたXMLを合格にしません。`runtimeStopped`は別に、Modの終了時保存、world閉鎖、writer drain、
同じnonceのJVMと子の終了を照合します。どちらかが失敗した場合、CLIは失敗し、結果とworldを保全します。

既存GameTestは保存失敗を意図的に起こす負の試験を含みます。その保存記録はMod証跡の`executionSaves`に残し、
終了時保存は`saves`と`gameTestShutdownSaveCompleted`へ分離します。GameTestの`modSaveCompleted`はfalseです。
GameTestの正常終了を、永続データの正常保存、snapshot clean、template昇格の根拠へ流用しません。

成功worldは結果保全後のGC候補、失敗worldは7日、ログと結果は30日の保持期限をRegistryへ記録します。
GCの候補提示・参照再照合・実削除は#536で接続し、ここではworldを自動削除しません。
未確定の実行は期限だけで回収しません。

完了した実行を同じownerでpinする場合は、gametestへ次のpayloadを指定します。解除はpinned=falseです。
pinの正本はRegistryであり、固定した過去のresult.jsonは変更しません。

```json
{"action":"pin","runId":"gametest-example-01","owner":"codex-session-example","pinned":true}
```

## 保存往復と併走の外部ハーネス

`verify_templates.py --restart-only`は既存のfixture生成・RCON・停止照合を再利用します。
新しい使い捨てrootでfixtureを変更し、正常停止、同じgenerationで再起動、現行APIで照合、再度正常停止まで実行します。
両runの異なるnonce、UUID対応のバニラ/Mod値・定義・設定・ファイル、正常停止証跡を照合します。
snapshot作成・template昇格はこのモードでは行いません。

```bash
python3.12 -B scripts/dev_server/verify_templates.py --repo /absolute/task/.github --root /absolute/minecraft-dev/new-test-root --bundle /absolute/artifacts/hash --evidence /absolute/evidence/new-test --approved-by operator --task-id issue-535 --fixture save-roundtrip-v1 --restart-only --game-test-runs 2 --minecraft-base-port 26105 --rcon-base-port 26115 --management-base-port 26120
```

GameTest2実行の設定は負荷試験限定です。既存root・既存証跡を上書きしません。
バックアップ設定は使い捨て試験の仮値であり、正式バックアップ・別媒体・停電耐性の確認とは扱いません。
GUIの見た目、実音、クライアント描画やJavaFXはこの試験の対象外です。

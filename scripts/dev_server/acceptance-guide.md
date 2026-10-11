# 総合受入の確認範囲

分離テスト環境をホストごとに運用へ移す前に確認します。
確定条件は[ServerMod #524](https://github.com/SushiEricWorkspace/SushiEricServerMod/issues/524)、総合導入は[#537](https://github.com/SushiEricWorkspace/SushiEricServerMod/issues/537)で追跡します。
下表は確認先と検証の区別を示す対応表です。ファイルが存在するだけでは合格になりません。
実行済み・未完了の区別は[受入記録](acceptance-results.md)に残します。

## 条件と確認先

確認先のPythonファイルはこのdirectoryを基準にしています。
合成runtimeの試験、実OSのPython child・socketの試験、実Minecraftの保存試験を区別して記録します。

| 条件 | 自動テスト・実試験の確認先 | 証跡に含める区別 |
|---|---|---|
| A01 同時acquire | `test_supervisor.py`のnative同時acquire | 有効leaseが一つ、実IPC |
| A02 古いlease/epoch | `test_runtime_control.py`、`test_writer_control.py` | stop/restore/遅延writeの拒否 |
| A03 外部port占有 | `test_runtime_ports.py`、`test_runtime_control.py` | 外部socket保持、外部プロセス無干渉 |
| A04 bind前後の競合 | `test_runtime_control.py` | API bind失敗・bind raceが偽RUNNINGにならない |
| A05 予約と実socket | `test_runtime_process.py`、`test_runtime_ports.py` | OS別IPv4/IPv6/TCP/UDP、実PID所有 |
| A06 稼働/停止途中のコピー | `test_snapshot_control.py`、`test_generation_control.py` | 合成の状態拒否と実停止証跡 |
| A07 外部writer | `test_writer_control.py`、`test_snapshot_control.py` | writer CLOSEDと実Managerの切断 |
| A08 バニラ/Mod状態 | `verify_templates.py`の`save-roundtrip-v1` | 現行APIで生成・再読込、UUIDと保存値 |
| A09 dimension/未知ファイル | `test_snapshot_control.py`、`verify_generation.py` | 合成集合と実3dimensionのregion・ブロック |
| A10 中断/欠損/改変 | `test_snapshot_control.py`、`test_generation_control.py` | 部分bytesと元世代保全 |
| A11 journal境界 | `test_generation_control.py`、`test_storage.py` | プロセス/失敗注入。停電耐性とは別 |
| A12 active/DB不一致 | `test_generation_control.py`、`test_runtime_control.py` | 起動禁止と照合付きrecover |
| A13 監督死亡/残存JVM | `test_runtime_control.py`、`test_runtime_process.py` | 合成JVM状態とnative child生存を区別 |
| A14 PID再利用/期限 | `test_runtime_control.py`、`test_generation_control.py` | 自動kill・再貸出しなし |
| A15 保存失敗/非正常exit | `test_runtime_control.py`、`test_gametest_control.py` | Boolean=false/例外/drain/終了コード/nonce |
| A16 容量/権限/別FS | `test_gc_control.py`、`test_storage.py`、`test_generation_control.py` | 失敗注入・native共有違反・旧データ保全 |
| A17 独立コピー | `test_snapshot_control.py`、`test_backup_verification.py` | 元の変更がコピーへ伝播しない |
| A18 永続枠/GameTest2 | `verify_templates.py --restart-only --game-test-runs 2` | 3JVMの実OS開始/終了時刻の重なり、結果の分離 |
| A19 GC/restore競合 | `test_gc_control.py` | 予約追加・排他・内容変更時拒否 |
| A20 互換性不明 | `test_contracts.py`、`test_runtime_bundle.py`、`test_generation_control.py` | 未知layout/artifactを推測で許可しない |
| A21 冪等性 | `test_supervisor.py`、各制御テスト | 同ID同payloadの再送と異なるpayload拒否 |
| A22 sourceからの独立 | `test_runtime_bundle.py`、`test_task_worktrees.py` | 合成削除保全と削除済みtaskの固定bundleによる実起動 |
| A23 秘密/port変換 | `test_snapshot_control.py`、`test_template_control.py`、`verify_generation.py` | private保持・共有正規化・復元先注入 |
| A24 root境界/リンク | `test_contracts.py`、`test_task_worktrees.py`、`test_backup_verification.py` | OSごとのlink/reparse/長いパス試験のskipを記録 |
| A25 別枠再現 | `verify_templates.py`、別媒体backupモード | artifact固定・seed・実APIの初期値照合 |

停止nonceの不一致・保存Boolean失敗は`test_runtime_control.py`と`test_gametest_control.py`、
切替後のManager遅延保存は`test_writer_control.py`とManager側のmanaged接続テストを確認します。
同じworktreeのGradle二重生成は`test_task_worktrees.py`のbuild中の排他とMod側のruntime出力経路、
稼働bundleのGC保護と正常停止前の回収拒否は`test_gc_control.py`と`test_gametest_control.py`を確認します。
合成build出力だけで実Gradle二重生成の合格とは扱いません。

## OS別の記録と有効化条件

1. 対象commitとdirty入力、Python・JDK・OS/FSを記録します。全テストの成功数・skip名・未実行をOSごとに残します。
2. 個別Issueで検証済みの結果を使う場合は対象HEAD・OS・検証範囲を明記し、後続変更で影響するものだけ再実行します。
3. 実保存往復、Managerの接続/保存/切断、障害訓練、[別媒体の復元](backup-guide.md)の証跡を揃えます。
4. snapshot/restore/start時間、論理/物理容量、persistent2/GameTest1の同時メモリを測定します。Windowsの停止試験rootは[計測ガイド](capacity-guide.md)を参照します。試験用quotaを運用推奨値へ転用しません。
5. 運用者が管理root・port・quota/reserve・バックアップ先を承認した後、そのホストの運用規約を有効化します。

一方のOSの合格を他方へ転用しません。GUI・音・クライアントの見た目、JavaFX画面、停電・実媒体破損への耐性は別項目です。
途中試験の保留や元データを消して合格にしません。既存primary・固定worktree・runの移動や削除は総合導入の暗黙作業に含めません。
総合受入が揃うまでは管理CLIを用いた試験のみを行い、並列運用の完成や既存環境の移行完了とは報告しません。

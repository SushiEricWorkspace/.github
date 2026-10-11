# 総合受入の検証記録（#537・進行中）

2026-10-11時点の実行結果です。運用者設定は承認されましたが、[条件と確認先](acceptance-guide.md)に対する最終照合は進行中です。
この記録だけで並列運用を有効にしません。

## 対象入力と自動テスト

管理スクリプトは`.github`の`4a514e0698d099563d084dd284cdccec54450abd`を基準に、#537の未コミット差分を適用しました。
WindowsとMacの全テストを実行した3 PythonファイルのSHA-256は一致しています。

| 入力 | SHA-256 |
|---|---|
| `backup_verification.py` | `623300cbb7d52b462178720b480ed224d0a15847e631973b11654a75c4753b7a` |
| `test_backup_verification.py` | `ff1507d53797fec4e56d8b6c6b8c0d91a431fa90684dc48d22efbc04ce33ce9b` |
| `verify_templates.py` | `d8c2490724c1aad13fc2fd0e4507285eaee5418d6a9ba76f5716dd85d1ac3962` |

| 実行 | 結果 | 対象外・スキップ |
|---|---|---|
| Windows・Python 3.12.14・全285件 | 277件成功、8件スキップ、失敗なし | Unix権限関連3件、Mac socket関連1件、リンク権限不足3件、Common入力未指定のGradle1件 |
| macOS 15.7.7・Python 3.12.4・全285件 | 281件成功、4件スキップ、失敗なし | Windows専用3件、Common入力未指定のGradle1件 |
| Windows追加計測テスト5件 | 5件成功 | 上記285件の実行後に追加。Macへ転送・実行していないWindows専用モジュール |
| Windows IPC修正テスト4件・native supervisor30件 | 全件成功 | 上記285件の実行後の修正。修正後の全回帰は別途記録する |
| Windows最終全294件 | 286件成功、8件スキップ、失敗なし（642.026秒） | Unix/Mac専用4件、symlink作成権限不足3件、Common入力未指定のGradle1件 |
| Mac最終全294件 | 281件成功、13件スキップ、失敗なし（211.120秒） | Windows専用12件、Common入力未指定のGradle1件 |

最終追加4ファイルは両OSでSHA-256を照合しました。

| 入力 | SHA-256 |
|---|---|
| `local_ipc.py` | `14509e59fe2b0deb416e0f428db2737acc66d5f91b3ce7f29a2a774120982659` |
| `test_local_ipc.py` | `de1645ef98d8dbce99e7b593402e5152d65cef226051319d001b1b7464c4bd0f` |
| `verify_capacity.py` | `6082efa49434413e31ebbdc50f21eddb4ef669ccf4ef223f74396939ccbfddeb` |
| `test_verify_capacity.py` | `56ee3c46680a3ca763a3a0cbd81b6288bc6e49ce4eb6117c6f80e29c4ac6a7e5` |

最終ログSHA-256はWindowsが`4be6857f932886230742b8d4fd55ab0844609fa1ca4121920ac827af52ae4f0d`、
Macが`42110f9875e2e0abc2a24fdefb9095893a00389d47bfc0815544fd58364653f7`です。

同一taskの実Gradle生成中の排他もWindowsで確認しました。#537専用Commonの`publishDevelopment`をtask専用Mavenへ実行し、
同じtaskに対する管理ビルド要求はGradle開始前の`BuildLockError`で拒否されました。
拒否完了時も先行Gradleは稼働し、その後終了コード0で発行しました。共有Mavenは更新していません。
これは生成側の排他試験であり、固定bundleのGameTest2run併走とは別に記録します。

Windowsでは実NTFS junctionを新規試験データへ作成し、IPC/GCのパス検査、worktreeパス検査、
bundle/snapshotの共通ファイル走査が拒否することを確認しました。参照先は保全し、自作junction本体だけ除去しました。
これはreparse pointの補足試験であり、権限不足でスキップした3件のsymlink試験を実行済みへ置き換えません。

Macは分離したdetached検証worktreeで自動テストのみ実行しました。#537でMac JVM・GUIは起動していません。
新規worktreeの作成先誤りは運用者の許可後に`git worktree move`で修正し、元の新規空親directory一つだけ除去しました。
既存primary・他worktreeを変更せず、由来を確定できなかった外側の空directoryは残しています。

## 受入IDとの照合

「回帰」は最終294件で成功した該当制御テスト、「native」はそのOSの自己生成Pythonプロセス・socket・IPC試験です。
合成JVMや失敗注入を実Minecraft・停電試験と同一視しません。対応テストは[確認範囲](acceptance-guide.md)にあります。

| ID | 確認結果・証跡の区別 |
|---|---|
| A01 | 両OS native IPCの同時acquireで有効lease一つ |
| A02 | 両OS回帰で古いlease/epochのstop・restore・writer保存を拒否 |
| A03 | 両OS native socket占有と回帰で拒否・外部socketを保持 |
| A04 | 両OS回帰でbind前後の競合・API失敗をRUNNINGにしない |
| A05 | 両OS native IPv4/IPv6・TCP/UDP所有表と回帰の予約照合 |
| A06 | 両OS回帰で稼働/停止途中のsnapshot・restore拒否。実正常停止後のコピーは#537で確認 |
| A07 | 両OS回帰でwriter残存拒否。実Managerのwriter登録・CloseはManager PR#193で確認 |
| A08 | 両OSの実往復は.github PR#9/#11、Windows別媒体往復は#537で確認 |
| A09 | 両OS回帰で未知通常ファイル集合、実3dimension・58ファイルは.github PR#9で確認 |
| A10 | 両OS回帰の中断・欠損・改変注入で旧世代保全 |
| A11 | 両OS回帰のjournal境界失敗注入・native公開/共有違反。停電試験ではない |
| A12 | 両OS回帰でactive/DB不一致の起動禁止・照合付きrecover |
| A13 | 両OS回帰で監督再起動/残存プロセスの拒否、native親終了後のchild生存を別に確認 |
| A14 | 両OS回帰でPID/開始識別/boot/期限不一致の拒否、native識別照合。自動killなし |
| A15 | 両OS回帰で保存Boolean/例外・nonce・非正常exitの昇格拒否 |
| A16 | 両OS回帰の容量/権限/別FS障害注入。Windows native共有違反、両OS試験rootのNO_SPACE拒否も確認 |
| A17 | 両OS回帰と.github PR#9実コピーで独立性確認。#537別媒体コピーも全hash照合 |
| A18 | 両OSの永続枠+GameTest2run実併走を.github PR#11で確認。#537の計測はpersistent2/GameTest1 |
| A19 | 両OS回帰でGCと復元の予約・排他・適用時再照合 |
| A20 | 両OS回帰で未知schema/layout/artifact拒否。異なるartifactへの移行は未対応 |
| A21 | 両OS native/回帰で同ID・同payload再送、別payload拒否。#537 EOF後も同IDで手動照合 |
| A22 | 回帰のworktree削除保全と、#535のソース削除後に同じ固定bundleを用いた#537実起動で確認 |
| A23 | 両OS回帰の秘密・port正規化/注入と、.github PR#9/#534系実復元で確認 |
| A24 | 両OS回帰の境界・特殊ファイル検査、Mac実symlink拒否、Windows実junction拒否。Windows symlink3件は権限不足のまま |
| A25 | 両OS別枠実往復は.github PR#9とテンプレート検証、Windows別媒体からの実再読込は#537で確認 |

Managerの両OS自動・GUI検証は[Manager PR#193](https://github.com/SushiEricWorkspace/SushiEricServerManager/pull/193)
（merge `f1c9b814d5777b0c17f0e61933c45b7f6770ae59`）を再利用します。#537のManager差分は規約だけで、GUI・保存実装を変更していません。
実世代往復は[.github PR#9](https://github.com/SushiEricWorkspace/.github/pull/9)（merge `b24d29dcf2893ffeeb94e22825ef4c91c8632ae1`）、
併走は[.github PR#11](https://github.com/SushiEricWorkspace/.github/pull/11)（merge `b514eead6da04e6e060726240442bd733aaf3f9c`）を参照します。
停止nonce/Boolean失敗・切替後のManager遅延save・稼働bundleのGC保護は最終回帰で確認しました。
実Gradle生成排他と固定bundleのGameTest分離は別の試験として上記に記録しています。

## Windows実サーバーと別媒体

固定artifactは`4051b6a4c2a12cb4f36166f69246cfeb05a65f818f49ae5f006a0fcdaa49b305`、JDK 21.0.9です。
Commonは`io.github.sushiericworkspace:sushieric-common-mod-dev:0.1.0-dev.20261010120343808`を使用しました。
bundleから実行し、共有Commonのpublish、primaryの切替、既存のrunコピーは行っていません。

- 運用者：ryuma。運用対象として選択されたWindows PCの使い捨てroot `minecraft-dev/537-windows`。
- 分離port：Minecraft 26105/26106、RCON 26115/26116、Management 26120/26121。GameTestは待受なし。
- C:とH:はNTFSの別物理SSD（Disk2/Disk3）であることを確認し、許可済みの`H:/SushiEricBackup/issue-537-windows`へ通常コピーしました。
- 正常停止後のfullrootは6,394ファイル、1,163directory、論理1,486,019,204 bytes。ファイル・空directory集合とhashをコピー・復元時に再照合しました。
- treeHashは`1fc6f3a786420e3518bdbfd8156fc5ea881434395d23c7a12ff227eddc32c9b8`。C→Hは124.609秒、H→Cは136.469秒。
- 元rootは`537-windows-before-backup-restore`へ保全。別媒体から同じ絶対パスへ復元し、snapshotから別枠への復元は116.328秒、起動・照合は43.860秒でした。
- fixtureの再読込は、所持金1234、token2個、内部ID末尾526、seed52452502、鉱石復活12秒、UUID対応のNBT・Modファイル8件のhashが一致しました。別nonceでの正常停止も確認しています。
- バックアップ・保全元・診断データを削除せず、試験用supervisorとJVMは正常終了しました。

## 二永続枠・一GameTestの計測

同じ停止試験rootへ新世代を明示restoreし、二枠を起動して固定bundleのGameTestを併走しました。
GameTestは16件成功、失敗・error・skipなし、終了コード0、所有JVMとchild終了確認済みです。

| 測定 | 結果 |
|---|---|
| 新世代restore（枠1 / 枠2） | 84.953秒 / 83.609秒 |
| start・所有照合（枠1 / 枠2） | 35.656秒 / 35.672秒 |
| 正常停止後のprivate snapshot作成・検証 | 17.203秒 |
| 3 JVM合計working setの標本ピーク | 3,199,528,960 bytes（約2.98 GiB） |
| 3 JVM合計private commitの標本ピーク | 3,836,231,680 bytes（約3.57 GiB） |
| 所有識別照合に成功した標本 | 21件 |
| 除外した標本 | 1件（GameTest終了と重なりPROCESS_MISMATCH） |
| 試験後rootの論理容量 / ファイル割当容量 | 3,725,363,247 / 3,752,770,856 bytes |

割当容量にFS metadataは含みません。メモリは標本ピークであり、最悪負荷や将来の大きなworldの保証ではありません。
両永続枠は保存成功・RCON応答・所有JVMとchild終了を確認してreleaseし、supervisorも終了コード0でした。
runtime停止成功と外部writer照合を含むsnapshot cleanは別の証明です。

## 正式Windows rootの導入

運用者ryumaは `C:/Users/ryuma/IdeaProjects/SushiEricWorkspace/minecraft-dev/windows-managed` のみを正式な管理CLI運用対象として承認しました。
persistent2/GameTest1、quota64GiB/reserve8GiB、Minecraft 26105/26106、RCON 26115/26116、Management 26120/26121を使用します。
承認条件の保存往復・初回バックアップ・最終照合がすべて成功したため、このrootに限って運用を有効化しました。
Mac・既存root/runへの運用許可は拡張せず、自動起動・常駐化・既存データ移行は行いません。

- `smoke-v1`と`save-roundtrip-v1`を新規生成し、別枠で復元・実API照合・正常停止後、人間の明示承認に基づいて昇格しました。
- 昇格した各テンプレートからresetし、再起動・所定データの実再読込・RCON正常停止を確認しました。
- templateHash：smoke `2d8ea5093d91b004e5800a29b6f3b3e00998d54cc0cb7c59cbc3d473b2e99319`、save-roundtrip `3f28622f5825ff273d4632fdb1f7ffb5c837d801e89b4dabb01ee8860855cc18`。
- 初回停止バックアップ：`H:/SushiEricBackup/windows-managed/initial-537`。22,519ファイル、4,128directory、論理5,210,425,659 bytes。
- 元rootとコピーの全ファイル・空directory集合、サイズ・SHA-256が一致しました。treeHashは`51e0188661760ae820df4faf0759ac0288e2f03f3a354a5d4b45659efc8ba697`です。
- 所有JVM・child・supervisorの不在、全待受portのbind可能、lease解放、writer/pending/予約不在、承認済みテンプレートとsnapshotの登録・hash一致を最終照合しました。
- 通常shutdownは枠を`RECOVERY_REQUIRED`にします。停止・貸出解放済みですが、次回supervisor起動時は照合付きrecoverが必要です。状態を手編集でAVAILABLEへ変更していません。

## 保全する証跡と対象外

Windowsの証跡はタスク直下`verification/windows-backup/`、`verification/windows-capacity/`、試験rootの診断・journalにあります。
正式rootの証跡は`verification/windows-managed/report.json`と`operational-acceptance.json`です。
Macは検証worktree内`.verification-537/python-tests.log`、`.verification-537/final/python-tests.log`と転送archiveを保全しています。
秘密を含み得る原本をGitへ追加・公開しません。

- snapshot作成時間の追加計測は新世代のrestore・正常起動停止後に成功しました。release後の新規leaseから旧世代のsnapshotを作ろうとした初回要求は、世代未紐付けの安全条件で拒否され、失敗記録を保全しました。
- 追加計測のstop要求でIPCのEOFが一度発生しました。監督・JVMは存続し、停止操作のDB登録とstopRequestedがないことを確認しました。同じoperationId・同じpayloadを手動再送し、正常停止・snapshot・release・監督shutdownを確認しています。新IDでの自動再送・killは行っていません。
- Windows named pipeの待受期限と取消が競合すると、成功済みの接続を閉じるコード経路を修正しました。追加4テストとnative supervisor30件が成功し、独立反証でも阻害事項はありません。今回のEOFとの因果関係は断定していません。
- 共通規約は#537のCommon・CombatCore・Mod・Managerの担当worktreeだけへ同期しました。A01〜A25と追加条件の証跡照合、正式rootのテンプレート明示昇格、Windows root限定の有効化まで完了しました。
- 停電、実媒体破損、PC全体の故障、未知Mod・外部DB、非協調writerの完全防止は検証済みと扱いません。
- JavaFX画面・クライアントの見た目は自動テストの成功へ含めず、変更の影響と既存の実機確認を別に照合します。
- 既存worktree/runの移行・削除は本受入の暗黙操作には含めません。

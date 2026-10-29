# 参照保護・容量管理・明示GC

管理rootと固定GameTest専用領域の保存物を回収するときに使用します。Python 3.12と起動済みsupervisorを使用します。
正常停止・保存証跡は[snapshotガイド](snapshot-guide.md)、世代の復元は[世代ガイド](generation-guide.md)を参照してください。

## 候補の確認と適用

GCは自動実行しません。root内の未知データ、途中操作、保留・予約を期限切れだけで削除しません。
まず候補を保存し、表示された参照の中から削除するものだけを明示します。

```bash
python3.12 -B scripts/dev-server.py gc --root /absolute/dev-root --operation-id gc-plan-01 --payload '{"action":"plan"}'
python3.12 -B scripts/dev-server.py gc --root /absolute/dev-root --operation-id gc-apply-01 --payload '{"action":"apply","planId":"gc-plan-01","references":["snapshot:old-snapshot"],"approvedBy":"inventoryのoperator"}' --dry-run
```

同じapply要求から`--dry-run`を外すと回収します。dry-runでは計画・操作記録を作成しません。
候補には参照、rootからの相対パス、通常ファイルの合計bytes、ファイル集合のtreeHashを表示します。
適用直前に参照・期限・全ファイル集合とhashを再照合し、変化があれば`GC_CHANGED`で拒否します。
他のコピー・昇格・参照操作とGCはsupervisor内で排他し、稼働JVMと記録された子プロセスの不在も確認します。
管理rootは所有者限定で、Windowsでは子要素のDACLも確認します。macOSの子要素は所有者一致を確認し、所有者限定root内に通常権限で生成されたworldを扱います。
リンク・reparse point・共有hardlink・特殊ファイルは権限にかかわらず拒否します。

## 保護対象と保持期限

- active世代、直前世代、そのartifact、稼働run、参照予約は常に保護します。lease期限切れでは解除しません。
- 使用可能なtemplate、保持snapshotとそれらの復元・検証に必要な世代・証跡を保護します。
- 未昇格snapshotは最新3件と作成後14日以内のものを保護します。
- GameTest成功worldは結果・終了証跡・レポートを照合して回収できます。失敗worldは7日、ログ・結果は30日保持します。
- 未完了runと結果不明の操作は期限にかかわらず回収しません。未知directoryやquarantineも候補にしません。

再現用のrunなどはpinを設定します。解除には新しいoperationIdで`pinned:false`を指定します。

```json
{"action":"pin","reference":"run:reproduction-run","pinned":true}
{"action":"policy","policy":{"snapshotCount":3,"snapshotSeconds":1209600,"failureWorldSeconds":604800,"resultsSeconds":2592000}}
{"action":"retire-template","templateId":"obsolete-template","approvedBy":"inventoryのoperator"}
```

pinは`artifact:<hash>`、`snapshot:<id>`、`generation:<instance>/<generation>`、`run:<id>`、`gametest:<id>`に対応します。GTのrunまたはgametestをpinすると診断anchorと実行cwdを両方保護します。
保持設定は全項目を正の整数で指定します。active・直前世代などの必須保護は変更できません。
失敗worldの期限をログ・結果より長く設定した場合、worldを含むrun全体と復元用artifactもworldの期限まで保護します。
template退役は使用を禁止し、参照保護から外します。templateファイルは削除せず記録を残します。
世代から参照されるartifactは、その世代を回収した後の新しい計画で候補になります。
保持期限を過ぎたGameTestのrunと専用artifactは、他の保護参照がなければ同じ計画に出ます。回収する参照は個別に明示選択します。新layoutの`gametest:<id>`は専用cwd、`run:<id>`は診断anchorです。専用cwdを残して診断anchorだけを回収することはできません。同時選択では要求の並び順によらず専用cwdを先に回収し、その失敗時は診断anchorを保持します。

## 容量不足と途中失敗

`quotaBytes`は管理rootと固定siblingの`<root名>-gametest-private`の通常ファイルを合算します。物理空きも両領域で検査します。候補コピーと記録の増分に`reserveBytes`を加え、quotaと物理空きを事前検査します。
コピー中も各ファイルの書込前に物理空きを確認します。必要容量が不足した場合は`NO_SPACE`で拒否し、自動GCしません。
snapshot・世代は通常コピーです。hardlink・CoWによる共有を前提にしません。

回収対象を同一FSのquarantine（GT実行cwd/worldは同じ親の`gc-<operationId>-<参照hash>`）へ移動した後、事前に記録した通常ファイルだけを照合して削除します。
権限エラーや内容変化などで失敗すると`RECOVERY_REQUIRED`にし、残存物と`manager/operations/gc-<operationId>/`のjournalを保全します。
途中GCの自動再試行・強制削除・保留解除は実装していません。人間が元の承認、選択対象、残存物、DB、証跡を照合するまで新しい参照操作を開始できません。
この状態でも診断、既存leaseの更新、writerのclose、稼働サーバーの正常停止は可能です。それぞれ既存のlease・writer照合を省略しません。
worktree削除はGCから独立しており、実行データを削除しません。

## バックアップ復元訓練の境界

正常停止してwriter不在を照合したsnapshot、必要artifact、inventory・Registry・操作証跡を保全し、通常コピーで別の空保存先へ複製します。
コピー後に全ファイルの集合・サイズ・SHA-256一致を確認し、snapshotをverifyして、試験専用の新しいinstanceへrestoreします。
元rootや失敗証跡を上書きせず、復元後の値と正常停止を確認して結果を記録します。
媒体を分けない使い捨てコピー・復元試験は、正式な別媒体バックアップ確認ではありません。
実際の別媒体、運用設定、媒体障害からの復元確認はServerMod #537の受入項目として残します。
[バックアップガイド](backup-guide.md)に従い、媒体の実確認と新規rootでの実保存値の復元結果を記録します。

## 検証

`test_gc_control.py`は合成runtime・停止証跡と使い捨て実FSで、参照保護、保持期限、容量減少、途中回収、結果の改変を確認します。
実Minecraftの起動試験や別媒体の運用確認とは区別します。OS別の成功・スキップ・未実行を個別に記録してください。

`verify_gc.py`は既存bundleを読取コピーし、新規rootでartifactのpin・容量不足注入・明示回収・通常コピー復元を実行します。
JVMは起動せず、実Minecraftのsnapshot復元試験には数えません。rootが存在すれば拒否し、成功・失敗後とも証跡と試験データを残します。

```bash
python3.12 -B scripts/dev_server/verify_gc.py --root /absolute/disposable/gc-test --bundle /absolute/fixed-artifact
```

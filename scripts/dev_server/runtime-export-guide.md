# 固定開発runtimeの出力

登録済みのタスクworktreeをビルドし、ソースworktreeの寿命とMinecraftの実行環境を分離するときに使います。
配置・担当の管理は[worktreeガイド](worktrees-guide.md)、データ契約は[README](README.md)を参照してください。
Python 3.12、JDK 21、対象のSushiEricDataModelとMod、同じタスクの`.github`が必要です。
`build-dev-runtime.py`は登録済みtaskの`.github/scripts`から実行してください。primaryや別taskのhelperコピーは、ビルド生成物を作る前に拒否します。

## ビルド

```bash
python3.12 -B scripts/build-dev-runtime.py --task-root /workspace/worktrees/issue-123-a --owner codex-session-a --store /workspace/minecraft-dev/artifacts
```

`--store`には運用者が初期設定したartifact領域を指定します。未設定の管理rootや他人のrunをコピー・初期化しません。
同一taskのGradle実行はOS lockで直列化し、登録ownerの引継ぎとworktree削除もビルド中は拒否します。
SushiEricDataModelをtask専用Mavenへ発行し、取得した完全座標でModをビルドします。SushiEricDataModel・Mod・`.github`が同じownerのtaskへ登録されていることが必要です。
新規taskではIssueが属する単一の変更repoを`--repos`、残るrepoを固定commitの`--dependency`として[worktreeガイド](worktrees-guide.md)の手順で登録します。
例えばSushiEricDataModelのIssueではMod・`.github`を依存にし、ManagerのIssueではSushiEricDataModel・Mod・`.github`を依存にします。`.github`のIssueではSushiEricDataModel・Modを依存にします。
未登録の必要repoは自動追加しません。依存のdetached HEAD・固定commit・cleanとownerを照合し、変更repoのdirty入力は従来どおりhashへ記録します。
登録全repoを入力集合に含め、exporterもtask `.github`の入力として固定します。
登録にManagerがあればManagerもビルドし、CombatCoreがあれば検証・発行します。変更repoとして登録した場合も同じです。
`--manager`・`--with-core`で追加検証を要求する場合も、対応するrepoの登録が必要です。
Coreが登録されていれば、発行した完全座標と同じbuild UUID内のMaven/coreを、
Modの`combatCoreDevelopmentCoordinate`と`combatCoreDevelopmentRepository`へ渡します。
Core未登録時は両propertyを渡しません。Modが実際にCombatCoreへ依存していない状態では、
helperからModの依存宣言を追加しません。
通常の開発ビルドは共有開発SushiEricDataModelの動的参照を維持します。正式版を開発bundleへ使用はできません。

## Gradleとの境界

- SushiEricDataModel/Coreの`publishDevelopment`はtimestampとUUIDで座標を作り、atomic claimで同座標の再発行を拒否します。成功した完全座標は`build/development-publication.properties`へ出力します。
- consumerには`-PdataModelDevelopmentRepository=PATH -PdataModelDevelopmentCoordinate=GROUP:MODULE:VERSION`を渡します。SushiEricDataModelのMod用・Editor用成果物を取り違える指定と正式版指定の併用は拒否します。
- Modの`describeDevRuntime`はLoomの実際のJavaExecからmain class、全classpath、JVM/プログラム引数、起動設定を取得します。配布jarから起動方法を推測しません。
- Modの`exportDevRuntime`は取得したレシピをbundleへ変換します。手動呼出にはビルド前に取得した`runtimeSourceLock`、`runtimeArtifactStore`、`runtimePython`を明示します。標準のexporterは兄弟の`.github/scripts/export-dev-runtime.py`です。
- exporterはJVMを起動しません。GameTest/実機実行は固定bundleを使う管理CLIの責務です。[runtime制御](runtime-control-guide.md)と[GameTest](gametest-guide.md)を参照してください。

## 固定する入力と出力

commit、dirty binary patch、全tracked入力hash、未追跡入力hashとその実体を記録します。
ignoreされた実行データ・build出力・秘密情報を入力へ混入させないでください。外部のinit script等による暗黙のビルド変更は使用しません。
公開前にGit入力・コンパイル前の依存・コピー元の全ファイルを再照合します。変更中の入力では公開しません。
SushiEricDataModel/CoreのPOM/JAR、処理済みmain/gametestクラス・resource、Minecraft/mapping/依存Mod、
解決されたLoom本体とplugin依存、Gradleのlib、JDK全体をコピーします。
JDK等にリンク・特殊ファイルがある場合も自動でたどらず拒否します。

`runtime.json`にはschemaVersion 2・dataModelCoordinateの完全座標・ツールの実バージョンとSHA-256を保存します。
LoomがSNAPSHOT指定でも解決された実体を固定し、次回起動時にネットワークから解決し直しません。
起動プロファイルはserver/GameTestを分け、GameTestクラスを通常serverのclasspathへ追加しません。
サーバーで読まれないclientのassets設定は除外します。起動設定の外部パスは明示adapterでbundle/runへの相対参照へ変換し、未知の外部パスや環境変数は拒否します。
作成したOS/アーキテクチャ専用のbundleです。別OSへの転送は想定しません。

artifact IDはmanifestから計算するSHA-256です。COMPLETEと全ファイルのサイズ/hashが一致するbundleだけを起動可能とします。
既存artifactの上書きはしません。失敗した`.staging-*`は診断用に保全し、成功扱いしません。
bundleのJDKを含めてartifact領域から実行でき、元のworktree、build、Gradleキャッシュを実行時に参照しません。
Javaの長いclasspathはrun側のargfileへ展開し、OSのコマンド長上限に依存しません。
`launch_command`は起動設定だけを展開し、JVMコマンドを返します。
既定はrun/.launchを新規作成し、`auxiliary`を渡すとrun-id専用の別pathへ出力します。
cwd/world/configを変えず同世代を再起動できます。既存の補助path・bundleは上書きしません。
bundleの内部へ実行データを置けませんが、generation/runtimeの配備は許可します。
管理propertyは全5項目または未指定です。プロセス開始・正常停止・保存証跡は
[runtime制御](runtime-control-guide.md)の責務です。

## runtimeのversionと切替

新規freezeは`recipeVersion: 2`と`dataModelCoordinate`だけを受け付け、`schemaVersion: 2`を出力します。
DataModelのMod用JARと隣接POMは双方必須で、コンパイル前の`dependencyInputs`へSHA-256を記録します。
`sources`には`SushiEricDataModel`と`SushiEricServerMod`を含め、旧提供元名やキーを混在させません。

保存済み`schemaVersion: 1`のbundleは、旧`commonCoordinate`・Common入力記録をそのまま検証・起動できます。
旧recipeの新規freeze、旧propertyへのfallback、bundleの再書込やartifact IDの付替えは行いません。
snapshotは同じartifact ID・layout IDのbundleへだけ復元でき、v1 snapshotを別artifactのv2へ変換しません。
切替時は対応済みrepoの確定commitで新bundleを作り、新bundle用の世代を初期化して起動・保存を検証します。
既存bundleと対応snapshotは保全し、新bundleへ旧snapshotを直接復元しないでください。

## ビルド記録とMavenの明示回収

helperはビルドUUIDごとに`worktrees/<task>/maven/<UUID>`へ発行し、task外の`worktrees/.build-records/<task>/<UUID>.json`へ開始・成功・失敗を記録します。
成功は完成bundleの全hash、manifest hash・artifact ID、発行コピーと入力の再照合後に確定します。MavenのPOM/JAR以外のmetadata・checksum・claim・空dirもinventoryへ記録します。
exporterはfreeze成功後にsource lockの拡張子を`.receipt.json`へ置き換えたreceiptを新規作成します。既存receiptは上書きせず拒否するため、直接exporterを呼ぶ場合も新しいsource lock名を使ってください。
receiptだけでは成功記録になりません。失敗・開始途中・記録のない古い出力は推測で回収しません。

worktree削除後、現在の登録ownerで候補を確認し、返されたUUIDと候補hashを明示して一つずつ回収します。成功記録のownerは開始時の監査情報であり、正規の引継ぎ後は現在ownerを指定します。

```bash
python3.12 -B scripts/collect-task-maven.py --workspace-root /workspace --task-id issue-123-a --owner codex-session-a --dry-run
python3.12 -B scripts/collect-task-maven.py --workspace-root /workspace --task-id issue-123-a --owner codex-session-a --build-id <32桁UUID> --candidate-hash <dry-runの64桁hash>
```

dry-runはUUIDごとの候補hashまたは保持理由を返します。実回収は`REMOVED`のtaskで、成功記録・完成bundle・コピーpublication・全inventoryが一致するUUIDだけが対象です。
同じbuild lockを保持し、owner/state・記録・bundle・inventoryを削除直前にも再照合します。リンク・特殊file・未知の追加file/dir・改変は拒否し、bundleとMaven候補の包含関係も両方向で拒否します。
削除するのは選択UUID配下の記録済み通常fileと空dirだけです。task/Maven親、`.build.lock`、registry、Git、artifact、snapshotは残し、自動GCは行いません。
Windowsは祖先dirを保持し、通常fileの固定パスとhashを同じhandleで確認して、そのhandleへ削除を指定します。POSIXはdir FDを固定し、leafをprivate quarantineへ移してinode/hashが一致したものだけを削除します。
途中失敗は`COLLECTING`のまま保全し、自動復旧しません。POSIXで差替えを検出したfileはUUID内の`.maven-collect-*`へ残ります。個別に保全状況を確認してください。
この手順は登録ownerとbuild lockに従う協調作業を前提にします。

## 検証境界

```bash
python3.12 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

合成runtimeの単体試験はhash・パス除去・入力変更・欠損/改変の拒否を確認します。
build helperの試験は、変更repoと固定依存の検査、未登録依存・owner・dirtyの拒否、登録Manager/Coreを含む入力固定、helperコピーの事前拒否とexporter入力の一致を確認します。
一時Gitと合成Gradle出力の成功を実Gradleの結合ビルド成功として扱いません。実結合は対応repoの確定commitが揃ってから別途実行します。
実際のLoom bundleからのGameTestは別途実行し、起動と開発専用定義の登録を確認します。
`scripts/check-runtime-bundle.py --bundle PATH`で再照合し、`--game-test-root PATH`も指定した場合だけ
新しいrun-idでGameTestを実行します。通常サーバーは起動しません。結果XMLとconsole.logはこの実行領域へ残します。
実SushiEricDataModelの並列発行試験は、`SUSHIERIC_DATAMODEL_GRADLE_SOURCE`に対象SushiEricDataModelのパスを明示して
`test_gradle_publication.py`を実行します。二つの独立コピーへ異なるresourceを追加し、並列発行後に両consumerの完全座標と内容を確認します。
通常の単体試験ではこの重いGradle試験をスキップします。
Windowsでの結果はmacOS 15の運用適合・停電耐久性の証明ではありません。

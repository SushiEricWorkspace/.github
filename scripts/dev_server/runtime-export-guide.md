# 固定開発runtimeの出力

登録済みのタスクworktreeをビルドし、ソースworktreeの寿命とMinecraftの実行環境を分離するときに使います。
配置・担当の管理は[worktreeガイド](worktrees-guide.md)、データ契約は[README](README.md)を参照してください。
Python 3.12、JDK 21、対象のCommonとMod、同じタスクの`.github`が必要です。

## ビルド

```bash
python3.12 -B scripts/build-dev-runtime.py --task-root /workspace/worktrees/issue-123-a --owner codex-session-a --store /workspace/minecraft-dev/artifacts
```

`--store`には運用者が初期設定したartifact領域を指定します。未設定の管理rootや他人のrunをコピー・初期化しません。
同一taskのGradle実行はOS lockで直列化し、登録ownerの引継ぎとworktree削除もビルド中は拒否します。
Commonをtask専用Mavenへ発行し、取得した完全座標でModをビルドします。
Managerも検証する場合は`--manager`、CombatCoreを検証・発行する場合は`--with-core`を指定します。
Modが実際にCombatCoreへ依存していない状態ではCoreをModの依存へ追加しません。
通常の開発ビルドは共有開発Commonの動的参照を維持します。正式版を開発bundleへ使用はできません。

## Gradleとの境界

- Common/Coreの`publishDevelopment`はtimestampとUUIDで座標を作り、atomic claimで同座標の再発行を拒否します。成功した完全座標は`build/development-publication.properties`へ出力します。
- consumerには`-PcommonDevelopmentRepository=PATH -PcommonDevelopmentCoordinate=GROUP:MODULE:VERSION`を渡します。CommonのMod用・Editor用成果物を取り違える指定と正式版指定の併用は拒否します。
- Modの`describeDevRuntime`はLoomの実際のJavaExecからmain class、全classpath、JVM/プログラム引数、起動設定を取得します。配布jarから起動方法を推測しません。
- Modの`exportDevRuntime`は取得したレシピをbundleへ変換します。手動呼出にはビルド前に取得した`runtimeSourceLock`、`runtimeArtifactStore`、`runtimePython`を明示します。標準のexporterは兄弟の`.github/scripts/export-dev-runtime.py`です。
- JVMやManagement APIを起動する通常サーバーの管理は行いません。GameTest/実機実行の管理は後続ハーネスの責務です。

## 固定する入力と出力

commit、dirty binary patch、全tracked入力hash、未追跡入力hashとその実体を記録します。
ignoreされた実行データ・build出力・秘密情報を入力へ混入させないでください。外部のinit script等による暗黙のビルド変更は使用しません。
公開前にGit入力・コンパイル前の依存・コピー元の全ファイルを再照合します。変更中の入力では公開しません。
Common/CoreのPOM/JAR、処理済みmain/gametestクラス・resource、Minecraft/mapping/依存Mod、
解決されたLoom本体とplugin依存、Gradleのlib、JDK全体をコピーします。
JDK等にリンク・特殊ファイルがある場合も自動でたどらず拒否します。

`runtime.json`にはmanifest・完全Common座標・ツールの実バージョンとSHA-256を保存します。
LoomがSNAPSHOT指定でも解決された実体を固定し、次回起動時にネットワークから解決し直しません。
起動プロファイルはserver/GameTestを分け、GameTestクラスを通常serverのclasspathへ追加しません。
サーバーで読まれないclientのassets設定は除外します。起動設定の外部パスは明示adapterでbundle/runへの相対参照へ変換し、未知の外部パスや環境変数は拒否します。
作成したOS/アーキテクチャ専用のbundleです。別OSへの転送は想定しません。

artifact IDはmanifestから計算するSHA-256です。COMPLETEと全ファイルのサイズ/hashが一致するbundleだけを起動可能とします。
既存artifactの上書きはしません。失敗した`.staging-*`は診断用に保全し、成功扱いしません。
bundleのJDKを含めてartifact領域から実行でき、元のworktree、build、Gradleキャッシュを実行時に参照しません。
Javaの長いclasspathはrun側のargfileへ展開し、OSのコマンド長上限に依存しません。
`launch_command`は新しいrun-id用ディレクトリへ起動設定だけを展開し、JVMコマンドを返します。
既存の`.launch`がある実行領域は上書きしません。プロセス開始・正常停止・保存証跡は呼出側が管理します。

## 検証境界

```bash
python3.12 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

合成runtimeの単体試験はhash・パス除去・入力変更・欠損/改変の拒否を確認します。
実際のLoom bundleからのGameTestは別途実行し、起動と開発専用定義の登録を確認します。
`scripts/check-runtime-bundle.py --bundle PATH`で再照合し、`--game-test-root PATH`も指定した場合だけ
新しいrun-idでGameTestを実行します。通常サーバーは起動しません。結果XMLとconsole.logはこの実行領域へ残します。
実Commonの並列発行試験は、`SUSHIERIC_COMMON_GRADLE_SOURCE`に対象Commonのパスを明示して
`test_gradle_publication.py`を実行します。二つの独立コピーへ異なるresourceを追加し、並列発行後に両consumerの完全座標と内容を確認します。
通常の単体試験ではこの重いGradle試験をスキップします。
Windowsでの結果はmacOS 15の運用適合・停電耐久性の証明ではありません。

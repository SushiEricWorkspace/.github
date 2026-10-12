# 人間用の起動・正常停止入口

Python 3.12で scripts/human-server.py を実行します。貸出・世代・保存の正本は[既存管理CLI](supervisor-guide.md)です。
Windows/macOSのローカルprofileに対応し、OS別wrapper・SSH入口はIssue #17で追加します。

## 初回設定と選択

[共通profile契約](connection-profile-guide.md)でGit外のprivateディレクトリへ登録します。
local profileのhostは対象ホストの socket.gethostname() と一致させます。
初期化済みroot・inventoryの運用者・承認済み枠・実Python 3.12/64bitを照合します。
approvalは既存承認への参照で、登録によって運用許可は発行しません。

~~~bash
python3.12 -B scripts/human-server.py choices --directory /absolute/connection-private
python3.12 -B scripts/human-server.py choices --directory /absolute/connection-private --profile "開発 Mac"
~~~

profile・instanceはIDまたは表示名で指定します。同名候補が複数ある場合はIDを使います。
templateは登録済みtemplate IDを名前として表示します。候補へ貸出token・RCON passwordを含めません。
WindowsではPython 3.12の絶対実行パスとドライブ付き絶対パスを指定してください。

## 起動、復元、保持

以下のARTIFACTは管理rootのartifactsへ配備済みの64桁SHA-256です。
新規ビルドは[runtime出力](runtime-export-guide.md)の別操作です。
COMPLETE・全ファイルhash・作成OS/アーキテクチャを照合し、別artifactへの互換性を推測しません。
初回の空枠は明示借用とprepareを選びます。EULAは運用者の同意後だけ指定します。

~~~bash
python3.12 -B scripts/human-server.py start --directory /absolute/connection-private --profile "開発 Mac" --instance "永続枠 1" --owner human-session --acquire --mode prepare --artifact ARTIFACT --seed 52452502 --accept-eula
~~~

既存世代のある返却済み枠は明示restore/resetを要求します。resetは登録templateへ戻し、
restoreは指定snapshotを復元します。旧世代は保全されます。起動の副作用で既存データを捨てません。

~~~bash
python3.12 -B scripts/human-server.py start --directory /absolute/connection-private --profile dev-mac --instance persistent-01 --owner human-session --acquire --mode reset --template accepted-default --artifact ARTIFACT --accept-eula
python3.12 -B scripts/human-server.py start --directory /absolute/connection-private --profile dev-mac --instance persistent-01 --owner human-session --acquire --mode restore --snapshot saved-work --artifact ARTIFACT --accept-eula
~~~

同じ有効貸出・世代の停止枠はmode=keepで保持して起動できます。この場合はacquireを付けません。
pauseは正常保存停止までで、貸出を保持します。restartは同じ稼働中貸出を正常停止し同じ世代を起動します。
返却後の再借用はrestartに含めません。

~~~bash
python3.12 -B scripts/human-server.py pause --directory /absolute/connection-private --profile dev-mac --instance persistent-01 --owner human-session
python3.12 -B scripts/human-server.py start --directory /absolute/connection-private --profile dev-mac --instance persistent-01 --owner human-session --mode keep --artifact ARTIFACT
python3.12 -B scripts/human-server.py restart --directory /absolute/connection-private --profile dev-mac --instance persistent-01 --owner human-session
~~~

接続できる監督を再利用します。応答喪失でも既存監督がOS lockを所有する場合は別監督を生成しません。
不在を確認した場合だけ常駐監督を生成し、生成段階・PID・native所有識別・bootId・ログをprivateへ保存します。
生成途中・終了未確認の記録が残る場合は新規生成せず保全します。既存rootの再initは行いません。
macOSのPythonランチャー遷移は設定Python本体と起動commandを照合し、開始識別・boot・cwd・ownerを保持します。

## 状態、ログ、renew、Manager接続

~~~bash
python3.12 -B scripts/human-server.py status --directory /absolute/connection-private --profile dev-mac --instance persistent-01 --owner human-session
python3.12 -B scripts/human-server.py logs --directory /absolute/connection-private --profile dev-mac --instance persistent-01 --owner human-session
python3.12 -B scripts/human-server.py renew --directory /absolute/connection-private --profile dev-mac --instance persistent-01 --owner human-session
python3.12 -B scripts/human-server.py handoff --directory /absolute/connection-private --profile dev-mac --instance persistent-01 --owner human-session --target-client manager-session
~~~

statusは接続アドレス・固定port・枠状態・writer・貸出期限・担当・途中要求・証跡先を表示します。
logsは現在runのconsole.logの末尾最大64KiBを表示し、保持する貸出token・その世代のRCON秘密を伏せます。
renew担当は明示したownerです。期限は既定3600秒でttl-secondsにより変更できます。
自動renewサービスはありません。ownerがstatusのexpiresAtより前にrenewを実行し、
Managerへの引継ぎ後も更新・正常停止・返却の責任を持ちます。端末の終了は返却を意味しません。
期限切れは自動回収せず、同じtokenの明示recoverによる照合が必要です。

handoffはprivate記録先と一回限りのhandoffIdを、明示したManager clientへ渡すために返します。
Managerは[共通引継ぎ契約](connection-profile-guide.md)のclaim/lease-checkを使い、二重acquireしません。
受取側はwriter登録・正常切断を担当し、発行元ownerがrenewを続けます。
この成功はManager画面の接続完了を意味しません。

## 保存停止と返却

Managerを先に正常切断します。未終了・UNKNOWN writerや未完了処理が残る場合はstopを拒否します。
管理CLIのRCON保存正常停止、Mod/バニラ保存・JVM/child終了照合を通し、
必要ならsnapshotを作成してからreleaseします。snapshotの有無は必ず選びます。

~~~bash
python3.12 -B scripts/human-server.py stop --directory /absolute/connection-private --profile dev-mac --instance persistent-01 --owner human-session --snapshot saved-work
python3.12 -B scripts/human-server.py stop --directory /absolute/connection-private --profile dev-mac --instance persistent-01 --owner human-session --without-snapshot
~~~

snapshotなしでも世代データは削除しません。保存失敗・writer残存・応答不明ではreleaseへ進みません。
timeout-secondsは監督の起動停止待機期限で、CLI応答期限はコピー・停止照合を含む900秒です。
期限切れでも監督・JVM・CLI childを自動killしません。

supervisor-stopは監督だけを止める別操作です。この入口が生成した同じnative識別・bootIdを要求します。
再利用しただけの監督は停止しません。shutdown-idleがRegistry内で全枠AVAILABLE・貸出/writer/child/pending不在、
進行中操作不在を確認し、以後の新規操作を拒否して停止します。他枠が復旧待ちの場合も停止できません。
既存Registryは監督終了・再起動で全枠を復旧待ちにします。再生成後は既存CLIの明示recoverで
未貸出枠の世代・停止証跡を照合します。新規借用では選択枠だけを同じ計画内で照合し、他枠は変更しません。

## 途中失敗と再送

sessionsへ貸出token・期待世代・期限、actionsへ入力と段階ごとのoperationId・秘密payload・応答、
supervisorsへ生成監督の所有情報を保存します。全てGit外で所有者限定です。profile・通常出力へ混ぜません。
送信前にSENDINGを耐久化し、失敗時に段階・actionId・operationId・証跡先を表示します。
途中要求が残る枠の新規操作を拒否し、同IDへ異なる選択を指定するとOPERATION_CONFLICTです。

応答喪失後の再送は、同じ引数と表示されたactionIdにresumeを追加して明示します。
完了段階は保存応答を使い、未確認段階は同じoperationId・同じpayloadだけを再送します。
新IDの自動再送・自動rollback・DB手編集での解除は行いません。
prepare/restore/reset/snapshotの途中状態は、同じ私有貸出と表示されたoperationIdを指定した
recoverのtarget-operation-idとrecovery-action=resume、または明示rollbackで照合できます。
recoverは同IDのdry-runを通してから実行します。起動停止結果不明やUNKNOWN writerを解除する操作ではありません。
監督生成・計画段階の中断も同じactionIdのresumeを明示します。監督接続を再照合し、まだ送信していない計画だけを作成します。
生成所有や正常終了を照合できない監督記録は保全し、新規生成やkillで回避しません。

## 検証

~~~bash
python3.12 -B -m unittest discover -s scripts -p test_human_server.py -v
python3.12 -B -m unittest discover -s scripts -p 'test_*.py' -q
~~~

macOSのテストプロセスだけはTMPDIR=/private/tmpを指定し、OS既定のリンクを通る一時領域を避けます。
合成Registry/Mod証跡と実FS試験は実Modの保存停止と区別します。
明示許可後の実Mod試験は scripts/verify-human-server.py --root ABSOLUTE_NEW_ROOT --bundle ABSOLUTE_FIXED_BUNDLE で行います。
既存rootを上書きせず、別媒体を確認済みの本番バックアップ試験として扱いません。


2026-10-12のIssue #16検証では、Windows Python 3.12.14とmacOS Python 3.12.4で
全327テストを実行しました。Windowsは319成功・8 skip、macOSは314成功・13 skipです。
その後の復旧境界とPythonランチャー修正は、追加20テストで再検証しました。
重点反証で見つかった途中要求・世代確定後の応答喪失・監督生成終了の収束を修正し、再確認しています。

実Mod試験の固定artifactはWindowsが4051b6a4c2a12cb4f36166f69246cfeb05a65f818f49ae5f006a0fcdaa49b305、
macOSがbe516b52b40ae02ccf70fa69c0cb970c81443a1cfa30510c7aa67febb4b9a72aです。
許可したhuman-16-windows / human-16-macosの使い捨てrootで、prepare、同世代restart、renew、handoff発行、
snapshot付き正常停止・返却、snapshot復元・起動、正常停止・返却、監督停止、固定port再bindを確認しました。
Macでは生成途中要求を保全し、同じactionIdの明示resumeで続行しています。
両rootのhuman-private/verification-report.jsonに結果を保存し、snapshot・旧世代・診断を残しています。
起動前のMac初期化失敗rootもhuman-16-macos-initialization-failedへ保全しています。
Manager画面接続・別媒体バックアップ・OS別wrapper・SSH起動入口の検証ではありません。

最新main cb2465dのSSH仲介変更を統合した後、Windowsで人間用入口・監督・接続profile・SSH仲介の85テストが成功しました。

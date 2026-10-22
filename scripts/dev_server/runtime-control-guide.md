# 固定runtimeの起動・停止

初回prepareから、固定したLoom開発bundleの起動・RCON正常停止までを扱います。
[監督・貸出](supervisor-guide.md)、[runtime出力](runtime-export-guide.md)を先に確認してください。
snapshot・外部writerは別ガイド、世代構築/置換/復旧は[世代ガイド](generation-guide.md)を参照してください。

## 入力と初回prepare

完成bundleを管理rootのartifacts/<artifactId>/へ配置し、acquireで得たtokenを使います。
prepareは空枠だけが対象です。既存データ・世代・未完了操作は上書きしません。
固定bundleのCOMPLETE、ファイル集合/サイズ/hash、data-layoutを検証し、
quotaBytes/reserveBytesと実空き容量を確認します。二枠の配備コピーはdeployment lockで直列化します。
macOSの管理IPC socketだけは、固定パス・socket型・所有UID・600権限と親のprivate権限を検証して容量走査から除外します。
他の特殊ファイル・リンクは許可しません。

payloadは次の項目すべてを指定し、未知項目を許可しません。

| 操作 | lease tokenに追加する項目 |
|---|---|
| prepare | artifactId（64桁SHA-256）、generationId、seed（64bit整数）、acceptEula=true |
| start | timeoutSeconds（正整数） |
| stop | timeoutSeconds（正整数） |

lease tokenはinstanceId、leaseId、epoch、expectedGenerationです。初回はexpectedGeneration=null、
prepare成功後は返却されたgenerationを指定します。acceptEulaは運用者が同意した場合だけtrueにします。
runtime操作のCLIは`--operation-id`を必須にし、結果不明でも新IDで再送しません。

stopだけは、同じleaseId・epoch・expectedGenerationの一致を条件に貸出期限切れを許容します。
監督が所有するRUNNING run、世代結合、未完了操作・writer、プロセス・待受・保存・終了の照合は維持します。
停止途中で期限を越えた場合も同じ照合で完了し、完了済みの同一stop要求は再実行せず結果を返します。
stopはexpires・owner・epochを変更しません。停止後に[世代ガイド](generation-guide.md)のrecoverで照合し、明示的にTTLを更新します。
start・renew・release・writer操作などは期限切れのまま拒否します。owner入力は追加せず、現行のlease能力トークンで認可します。
稼働中の監督は起動時に読み込んだ実装を使用します。CLIやソースを更新しても、既存監督の期限判定は更新されません。
監督の再起動は全枠を復旧待ちにするため、期限切れRUNNINGを自動解消する目的で再起動せず、既存runの対応を別途確認します。

```bash
python3.12 -B scripts/dev-server.py prepare --root /absolute/minecraft-dev --operation-id prepare-01 --payload-file /absolute/prepare.json --timeout-seconds 600
python3.12 -B scripts/dev-server.py start --root /absolute/minecraft-dev --operation-id start-01 --payload-file /absolute/start.json --timeout-seconds 600
python3.12 -B scripts/dev-server.py stop --root /absolute/minecraft-dev --operation-id stop-01 --payload-file /absolute/stop.json --timeout-seconds 600
```

`--timeout-seconds`はCLIのIPC応答期限、payloadのtimeoutSecondsは監督が起動/停止を待つ期限です。
RCON通信とJVM/child終了待機にはそれぞれpayloadの期限を使用します。
RCONは接続・認証・停止応答全体で一つの絶対期限を使い、断片応答ごとに延長しません。
IPC期限はそれらと検証・コピー時間を含む長さにしてください。CLI期限切れで監督操作をkill/再送しません。
start/stopの予約と確定だけでinstance OS lock→短いDB transactionを保持します。世代・bundleの検査、OS照合、spawn、RCON、終了待機は両方の外で行います。
予約したlease/epoch/世代/runとoperationId/fingerprint/未完了状態が一致するときだけ確定し、古い結果で新runを更新しません。
runsは新規INSERTと読取元record bytesによる条件付きUPDATEを使い、既存runの置換や再読込による勝手な確定を行いません。GameTestも同じrecord更新規則を使います。
作用後の結果不明ではpending/RECOVERY_REQUIREDを保全し、再spawn・新ID再送・期限による解除は行いません。
このlock分割は既writerとの競合拒否までで、P-v5のBLOCKED/accepted drain、native受付、最新writer台帳の証明は未接続です。

dry-runは条件照合だけで、DB、世代、予約、補助ファイル、プロセス、socketを作成しません。
dry-runの成功は実行時の空きport保証ではありません。

初回prepareも共通世代エンジンでstagingへruntime/、config/、server.properties、eula.txt、
generation.jsonを構築し、全hash照合後にactive.jsonとDBへ同じ世代/固定artifactを記録します。
途中失敗は部分データと参照予約を保全してRECOVERY_REQUIREDへ移します。
既存世代へのprepareは拒否します。restore/resetとjournal復旧は[世代ガイド](generation-guide.md)に従います。

## 起動照合と予約

normalのserver profileを使い、GameTest runner引数を手編集しません。
generationをcwdとして、診断用instances/<instanceId>/diagnostics/<runId>/.launchだけをrunごとに変えます。
nonce、proofPath、instance/generation、runtime manifestの全管理propertyを追加します。
Javaの引数ファイルはWindowsではシステム文字コード、macOSではUTF-8で出力し、byte単位で引用します。
Windowsのシステム文字コードで表現できないパスは置換せず拒否します。
[Java 21の引数ファイル仕様](https://docs.oracle.com/en/java/javase/21/docs/specs/man/java.html#java-command-line-argument-files)に従い、OSの文字コード設定は変更しません。
JAVA_TOOL_OPTIONS、JDK_JAVA_OPTIONS、_JAVA_OPTIONS、CLASSPATHによる暗黙入力は拒否します。

各枠の固定TCP portはinventoryの承認済み宣言値を使い、競合する既存環境を停止しません。通信試験用範囲もinventoryで明示し、
runtime_ports.network_endpointsで範囲を制限します。GameTest通信起動ハーネスは後続の責務です。
予約はhost/protocol/address/port/v6Onlyを区別し、IPv4/IPv6 wildcardとdual-stackの重なりを拒否します。
Managementは127.0.0.1固定、Minecraft/RCONは明示loopbackです。lan-explicitは具体アドレス契約が未接続なので拒否します。
Query/JDWPを黙って有効化したり、競合時に別portへ変更したりしません。

全socketの事前bindを同時に行い、閉鎖後にJVMを作成します。この間のraceは後段で検出します。
RUNNINGへの遷移には次の両方が必要です。

- OS識別: PID、native開始識別、host boot、実cwd、実行ファイル、実command hash、OS ownerが起動記録と一致。
- Mod証跡: 同じinstance/generation/nonce、PID/開始時刻、artifact/hash、実world/config/Mod config root、
  ready=true、Management開始成功、未知writerなし。serviceごとの報告listenerが宣言と一致し、当該PIDの実TCP/UDP待受集合も完全一致。

Windowsは64bit Python 3.12のnative開始時刻/PEB/owner SID、IP Helperの所有PID表を使用します。
自身のJVMをResumeする前に実識別を保存し、専用Jobへ割り当てます。KILL_ON_JOB_CLOSEは設定しません。
OS生成前に監督が所有オブジェクトを保持します。作成直後のPID/groupと段階を記録し、
識別・記録・Job割当の失敗ではhandle（未Resumeのthreadを含む）と予約を保全します。
記録媒体自体が書込み不能なら永続記録の成功は保証できず、監督内の所有オブジェクトを保持して停止・復旧判断を待ちます。
生成途中のプロセスを自動Resume/killせず、復旧判断までRECOVERY_REQUIREDに留めます。
macOSはlibproc、KERN_PROCARGS2/kern.bootsessionuuid、lsofで実cwd/command/待受を照合し、
専用session/process groupの親子を待ちます。未対応API/取得不能は推測やPIDだけの照合へfallbackしません。
group列挙がerrno付きで失敗した場合は、子プロセス不在の証明として扱いません。
各OSで同じnative試験を実行する必要があり、Windows成功はmacOS成功ではありません。
boot識別は起動時刻の推測ではなくOSのboot session UUIDを使います。
macOSでsysctlの権限が拒否された場合も時刻へfallbackしません。
API定義は[Apple XNU](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_sysctl.c)、
Windowsの親子所有は[Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)を参照してください。

## 正常停止と失敗保全

stopは自身のnative識別とRCON socket所有を再確認してから、privateなserver.propertiesの秘密で認証します。
接続後・stop送信前も所有を照合します。秘密・command全文・設定内容をJSON/監査へ転載しません。
RCON要求/応答、Mod/vanilla保存成功と背景writer drain、正常exitCode=0、所有JVM/childの終了が揃って
初めてruntime停止証明を記録します。親が終了してもJob/groupにchildが残れば停止完了ではありません。
port予約は所有JVMとchildの終了確認後だけ解除します。

証跡はdiagnostics/<runId>/のcreation.json、process.json、console.log、proof.json（Modが更新）、
startup-proof.json、runtime-stop.json、およびRegistryのrunsに残します。
同世代の再起動でも旧証跡を上書きしません。正常停止した直前runの保存/終了確認がなければ再起動しません。

API bind失敗、nonce/root/hash/socket不一致、保存Boolean=false/例外、writer未終了、
非正常exit、期限超過ではRECOVERY_REQUIREDとし、データ・診断・未完了操作の参照を保全します。
start失敗の後始末も自身の識別とRCON所有を確認できた場合の正常stopだけです。
他プロセス停止、stdin fallback、自動kill、期限切れleaseの自動再貸出しはありません。
監督再起動では全枠を保留し、recoverによる照合が完了するまで二重起動しません。

runtimeStopProof=trueだけではsnapshotCleanはtrueになりません。[snapshotガイド](snapshot-guide.md)の全検証が別途必要です。
協調Managerのwriter終了は[管理writerガイド](writer-guide.md)、世代引継ぎ・復旧は[世代ガイド](generation-guide.md)に従います。
同一ユーザーの非協調的な直接編集や、Job/group外の管理外writerをこのadapterの終了証明へ含めません。
snapshot/restore、実データ枠のreleaseには外部writer終了と世代照合も必要です。回収は[GCガイド](gc-guide.md)に従います。

## 検証

```bash
python3.12 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

合成bundle/Mod証跡の失敗注入と、自己終了型Python親子・実socket・RCON応答のnative試験を区別します。
合成試験はMinecraftの保存往復を証明しません。許可後の実二枠・Management API・RCON・再起動は別試験です。
quota/空き確認は開始時の余裕確認であり、将来の無制限なデータ増加やFSの停電耐久性を保証しません。

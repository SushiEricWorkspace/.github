# Windows・macOSの人間用入口

Python 3.12/64bitと、対象OS向けJDK 21を含む配備済み固定artifactを使います。
[一括操作の契約](human-server-guide.md)を、Windowsでは `scripts/human-server.ps1`、
macOSでは `sh scripts/human-server.sh` から呼び出します。設定済みの入口はコマンド省略時にstartを実行します。
両OSとも同じPython処理を使い、SSHでも対象ホストの管理CLIへ委譲します。

## 初回の登録

管理rootのinit・artifactの配備・運用承認は[管理CLI](supervisor-guide.md)で先に行います。
この入口はrootを再初期化せず、Python/JDK/SSHの導入、SSHサーバー・認証・Firewall・host keyの変更を行いません。
profileのroot・Python・CLIは対象ホスト上の絶対パス、approvalは既存承認の参照です。
localのhostは対象ホストの `socket.gethostname()`、sshのhost/sshUserは承認済みの接続先を指定します。

[profileのJSON例](connection-profile-guide.md#保存先と登録)を編集し、Git外のprivate保存先へ登録します。
保存先の親は先に用意してください。tokenやpasswordはJSONへ記載しません。

```powershell
$env:SUSHIERIC_PYTHON = 'C:\Python312\python.exe'
$env:SUSHIERIC_HUMAN_DIRECTORY = 'C:\開発 設定\human-private'
$OutputEncoding = [Text.UTF8Encoding]::new($false)
Get-Content -Raw -Encoding utf8 'C:\開発 設定\profile.json' | & $env:SUSHIERIC_PYTHON -B scripts/connection-profile.py register --directory $env:SUSHIERIC_HUMAN_DIRECTORY
& scripts/human-server.ps1 choices
& scripts/human-server.ps1 setup --profile '開発 Windows' --instance '永続枠 1' --owner human-session --artifact ARTIFACT
& scripts/human-server.ps1 doctor
```

```sh
export SUSHIERIC_PYTHON=/absolute/python3.12
export SUSHIERIC_HUMAN_DIRECTORY='/Users/example/開発 設定/human-private'
"$SUSHIERIC_PYTHON" -B scripts/connection-profile.py register --directory "$SUSHIERIC_HUMAN_DIRECTORY" < '/Users/example/開発 設定/profile.json'
sh scripts/human-server.sh choices
sh scripts/human-server.sh setup --profile '開発 Mac' --instance '永続枠 1' --owner human-session --artifact ARTIFACT
sh scripts/human-server.sh doctor
```

ARTIFACTは配備済みの64桁SHA-256へ置換します。setupはprofile・枠・owner・artifactの選択だけを保存し、借用も起動も行いません。
doctorは実行ホストのPython/native識別、承認root/inventory、固定artifactのOS・hash・JDK 21と固定portを確認します。
artifactを選択しないdoctorではJDK検査は行いません。別profileを指定した場合、以前のprofileの枠やownerを流用しません。
profile編集後はsetupを再実行し、固定設定を確認します。

Pythonの指定は環境変数のほか、PowerShellの `-Python '絶対パス'`、shの先頭の `--python '絶対パス'` でも行えます。
未指定時はWindowsが `py -3.12`、macOSが `python3.12` を使用します。PowerShellの既存実行ポリシーは変更しません。

## 起動と日常操作

以下はWindowsの例です。macOSでは `& scripts/human-server.ps1` を `sh scripts/human-server.sh` に置換し、初期templateに`initial-world-macos-v1`を指定します。
選択済みの貸出・停止世代は、コマンド省略の入口を一回実行するとmode=keepで起動します。
初回もOS別登録テンプレートを選びます。同じroot・artifactではresetを使用します。
新しいroot・artifactの準備は管理CLIの[template-init](template-guide.md#新しいビルド管理rootでの初期ワールド作成)で行い、
取得した同じ貸出tokenでstart・stopまで操作してください。この入口のmodeにtemplate-initはありません。
空worldが明示目的の試験だけprepareを選びます。同意前にaccept-eulaを指定しないでください。

```powershell
& scripts/human-server.ps1 --acquire --mode reset --template initial-world-windows-v1 --accept-eula
& scripts/human-server.ps1 status
& scripts/human-server.ps1 logs
& scripts/human-server.ps1 renew
& scripts/human-server.ps1 restart
& scripts/human-server.ps1 pause
& scripts/human-server.ps1
```

statusは接続先・port・状態・期限・renew担当・途中要求を表示します。logsは現在runの末尾を表示し、保有tokenとRCON秘密を伏せます。
出力は既定で日本語、機械向けJSONは `--json` を明示します。JSONのlogs本文は `result.text` です。
端末を閉じても停止・返却されません。ownerがexpiresAtより前にrenewし、stopで保存停止・返却します。
監督は必要なときだけ起動し、既存監督を再利用します。全体の自動起動や自動renewは設定しません。

返却済みの枠には旧世代が残ります。再借用はrestore/resetを明示し、旧世代を起動の副作用で破棄しません。
template候補は `choices --profile PROFILE` で確認します。

```powershell
& scripts/human-server.ps1 --acquire --mode reset --template accepted-default --accept-eula
& scripts/human-server.ps1 --acquire --mode restore --snapshot saved-work --accept-eula
```

## 保存停止とManagerへの引継ぎ

Managerを先に正常切断してwriterを終了します。未完了・UNKNOWN writerが残る場合はstopを拒否します。
正常保存停止、JVM/child終了の照合、任意snapshot、返却の順に実行します。snapshotの有無は必ず指定します。

```powershell
& scripts/human-server.ps1 stop --snapshot saved-work
& scripts/human-server.ps1 stop --without-snapshot
& scripts/human-server.ps1 supervisor-stop
```

stopの二例はどちらか一方を選びます。snapshotなしでも世代データは保持します。
supervisor-stopはこの入口が生成した同じ監督だけを、全枠の返却・writer/処理終了を確認して停止します。
他入口が生成した監督を引き取りません。停止未確認では証跡を残し、killしません。

```powershell
& scripts/human-server.ps1 handoff --target-client manager-session
```

引継ぎIDとprivate保存先を同じユーザーのManagerへ渡し、[共通claim契約](connection-profile-guide.md#既存貸出への明示的な引継ぎ)で受け取ります。
通常操作ではraw tokenをコピーしません。一回限りのofferはclient側private保存先の一つだけで、SSHホストへ複製しません。
Managerのwriter登録と正常切断、発行元ownerのrenew責任は継続します。この操作だけではManager画面へ接続されません。

## SSH profile

両端の既存OpenSSH・認証・確認済みknown_hostsが必要です。[SSH仲介の条件](ssh-guide.md#接続条件)に従います。
Windows→macOS、macOS→Windowsともprofileに対象OSのパスを記載し、client OSのパスへ変換しません。
client側setupで確認済みknown_hostsの絶対パスを追加し、doctorで対象ホストを確認します。
Windowsの監督はSSH親Jobから分離し、切断後も同じ監督がJVMを所有します。
親Jobが分離を許可しない場合は監督生成時に停止し、設定変更や別方式での起動は行いません。

```powershell
& scripts/human-server.ps1 setup --profile '承認済み SSH' --instance '永続枠 1' --owner human-session --artifact ARTIFACT --known-hosts 'C:\Users\example\.ssh\known_hosts'
& scripts/human-server.ps1 doctor
& scripts/human-server.ps1 --acquire --mode prepare --accept-eula
```

```sh
sh scripts/human-server.sh setup --profile '承認済み SSH' --instance '永続枠 1' --owner human-session --artifact ARTIFACT --known-hosts '/Users/example/.ssh/known_hosts'
sh scripts/human-server.sh doctor
sh scripts/human-server.sh --acquire --mode prepare --accept-eula
```

SSHは一括操作を対象ホストの同じHumanServerへ送り、貸出・保存停止・復旧は共通管理CLIが実行します。
同じprofile/枠/ownerには接続を越えて同じclient IDを保全し、ホスト側 `manager/human-clients/<clientId>` に秘密と操作journalを保存します。
SSHログには本文/tokenを記録せず、通常のstatusや結果にもtokenを表示しません。

## 未完了・復旧待ち

エラーにはcode、段階、要求ID、操作ID、証跡先を表示します。statusからホストの途中要求とclientの未確認要求をそれぞれ確認できます。
clientだけ未確認でも、同じ要求を明示照合するまで別IDの通常操作・handoff・監督停止を抑止します。recoverはホストの復旧契約に沿って明示できます。
通信喪失・端末終了・待機期限切れでは成功とも返却とも扱わず、監督/JVMを自動killせずに保全します。
別IDで起動し直さず、同じ選択を保ったまま表示された要求IDでresumeを明示します。

```powershell
& scripts/human-server.ps1 --action-id REQUEST_ID --resume --acquire --mode prepare --seed 52452502 --accept-eula
```

上の再送例は初回起動と同じ引数の場合です。setupの固定選択も変更しないでください。
期限切れ・RECOVERY_REQUIREDでは[復旧手順](human-server-guide.md#途中失敗と再送)に沿って、証跡の操作IDとresume/rollbackを明示します。
SSH応答待機は既定1800秒、接続開始と正常切断の待機は最大15秒です。期限切れでも自動再送しません。
ホスト側で完了済みでもclientが応答を受け取れなければ未完了として残し、同じ要求の再照合で確定します。

## Issue #17の検証記録

全体テストはWindows/Python 3.12.14で363件（355成功・8 skip）、macOS/Python 3.12.4で365件
（351成功・14 skip）でした。skipは他OS専用試験とCommon入力未指定の実Gradle試験です。
最終native修正後は、Windowsで人間用操作36件・SSH境界17件がすべて成功し、macOSでは人間用操作36件と
SSH境界17件（15成功・Windows専用2 skip）が成功しました。runtime bundle14件も両OSで成功しています。

Windows PowerShell 5.1とmacOSのshで、日本語/空白/引用符/空引数/末尾backslash、両streamと終了コード、
UTF-8 profile登録、実入口setup/doctorによるPython/native/JDK 21/固定portの診断を確認しました。
不正UTF-8を含む長いログも、末尾16KiBでJSON frame上限内に収まります。

両OSのローカル入口、Windows→macOSとmacOS→Windowsの実SSH入口で、固定artifactの実Mod起動、
同世代の再起動、renew、一回限りの引継ぎclaimと二重claim拒否、pauseからの既定keep起動、
保存停止/snapshot/返却、snapshot復元後の起動とsnapshotなし返却を確認しました。
各OSで8 runの停止証跡はmodSaveCompleted=true・exitCode=0です。両OSでSSH入口が生成した監督も正常停止しました。

実機で判明したshの改行をLFへ固定し、新規console.logとWindows private資源のowner/DACLを実行ユーザーへ設定しました。
WindowsのJava引数はシステム文字コードでbyte単位に引用し、「日 空 ソ 表」のclasspathを実JDKで起動できました。
Windows SSHのstdinはPythonへ直接引き継ぎ、binary内容・EOF・EOF前の二往復と実SSHの正常切断を確認しました。
監督はSSH親Jobから分離し、新規監督/Mod起動後の切断・再接続でも同じ監督/JVMが存続することを確認しました。

Java引数とSSH親Jobによる初回失敗rootは、データ・journal・貸出を保全しました。
再初期化せず、許可された別rootで修正後の試験を実施しています。初回Java失敗rootの監督は共通CLIで停止しました。
重点反証は未確認要求・同ID再送・引継ぎの正本・ログ上限・所有者・標準stream・監督生成を確認し、最終修正後の指摘はありません。
SSH・認証・host key・Firewallの設定変更、失敗rootの削除、強制終了コマンドは実行していません。

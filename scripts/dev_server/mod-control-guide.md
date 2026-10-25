# Mod専用controlの先行wire契約（v1）

Refs SushiEricWorkspace/.github#64。これは構文・元bytes・要求応答の相関契約です。
既存supervisorのstart/stop、writer受付、Registry、管理CLIへは接続していません。
native peer認証、descriptorの安全な公開・読取、台帳の最新性・完全性、耐久readback、
再実行の安全性、Data admission、実world保存・終了を証明する実装ではありません。

## descriptor

JVM propertyは次の2個を使用します。

- `sushieric.managed.controlDescriptor`: 絶対path
- `sushieric.managed.controlDescriptorSha256`: descriptor原UTF-8 bytesのSHA-256

公開先は `<managementRoot>/manager/control/<runtimeRunId>/mod-control.json` です。
Pが新規作成して不変に保ちます。読取側は同じopened handle/fd上でsize・hash・通常ファイル・
link/reparse不在・owner・ACL/modeを検査し、parentとrootを固定する必要があります。
本parserはその実体検証を実装していません。

全項目は必須、未知項目は禁止です。

| 項目 | 型・用途 |
| --- | --- |
| version / purpose | 整数1 / `mod-control` |
| descriptorId | descriptor識別子 |
| managementRoot | 管理rootの絶対path |
| instanceId / leaseId / epoch / generationId | 固定枠・lease・整数epoch・世代 |
| runtimeRunId / startNonce | Pの起動識別。Data runUuidとは別 |
| artifactId / runtimeManifestHash | 固定artifactとmanifest SHA-256 |
| worldRoot / shopDirectory | generation配下の絶対path。shop末尾はData契約の`shop_data` |
| controller | Data `WorldProcessIdentity`のP本人 |
| endpoint | `{transport,address}` |

識別子は既Registry同様の小文字英数字・ハイフン、1～128文字です。
Windowsは`windows-pipe`と`\\\\.\\pipe\\...`、macOSは`macos-unix`と
`<managementRoot>/manager/control/<runtimeRunId>/mod-control.sock`を使います。
world名はserver.propertiesのlevel-nameに従い、parserで`world`へ固定しません。
実体・manifest・正しいshop配置はPが後段で照合します。未知のP/M本人情報を埋めるfallbackはありません。

## frameと共通envelope

1frameは4byte unsigned big-endian body長と、その長さの単一UTF-8 JSON objectです。
prefixを含め最大65536bytes、bodyは1～65532bytesです。切断・不足・連結frame・末尾JSON・
重複key・未知envelope/payload項目・未知version/purpose/type/phase・不正UTF-8/BOM・
孤立surrogate・非有限数・整数箇所のbooleanを拒否します。
余白を含む元body全体を保持し、整形して同要求へ置き換えません。
巨大writer集合は切り詰めず、frame上限を超えれば拒否します。

全envelopeは `version,purpose,type,phase,operationId,requestNonce,descriptorSha256,payload` です。
version=1、purpose=`mod-control`、typeは`request|response|error`です。
各往復で別operationIdを使い、応答は元operationId・nonce・descriptor hash・対応phaseに束縛します。
各response payloadには元request body（prefixを除く原bytes）の`requestBodySha256`が必須です。

| request phase / payload | response phase / payload（共通requestBodySha256に加える項目） |
| --- | --- |
| HELLO / owner | GRANT / management |
| BOUND / owner, management, statusJson, statusJsonSha256 | ACK / statusJsonSha256, worldUuid, runUuid, stateRevision |
| STOP_BLOCK / management | BLOCKED / management, registryRevision |
| SAVE_REQUESTED / management, requestJson, requestSha256 | COMPLETION / requestSha256, completion |

ownerはData `WorldProcessIdentity`、managementはData `WorldManagementBinding`の形です。
controllerはP、ownerはMであり、GUI writer-session本人やoffline C/Wへ読み替えません。

## startupの2往復

Pはnativeに観測した所有M・P本人・lease/世代・offline owner不在・新受付遮断・accepted drainを
照合してGRANTを作る必要があります。Mは`correlate_grant`でdescriptorとGRANTを照合したうえで
Data beginRunを実行します。この関数はnative本人認証の代用ではありません。

BOUNDは同じM本人とGRANT managementを含み、Data Runningの元`WorldRunStatus`を
`statusJson`文字列に保持します。P側は`correlate_binding`に認証境界で保持したM本人と
元GRANTを渡し、descriptor・P・lease/epoch/世代・world/shop pathを照合します。
ACKはそのstatusJson原UTF-8 bytesのhashとworldUuid/runUuid/stateRevisionを返します。
ACK前にManagementを開いてはいけません。runtimeRunIdからData runUuidを推測しません。

## stopとRequested

STOP_BLOCKとBLOCKEDのmanagementは元GRANT全体と一致させます。
初期drainSha256を書き換えた新bindingを発行しません。Pは新受付遮断をfinish/exitまで維持します。

Mは保存・全DBclose後のData `WorldManagedSaveRequest`全体を`requestJson`へ保持します。
Data参照契約は固定commit
`e5719b310da191abd6aa94c534dbb1d37f35c62d`（Data129）で、
serializerは`Json { encodeDefaults = true; classDiscriminator = "type" }`です。
既定値・null・Data sealed payloadのFQN discriminatorを省略しません。
FQNと合成vectorはソース契約から転記しており、Data serializer実行の証跡ではありません。

`statusJsonSha256`と`requestSha256`は各文字列をUTF-8へ戻した原bytesのhashです。
Dataの`status.sha256`は別のstate hashであり、Pythonの再serializeやsortで代用しません。
inner JSONも重複key・UTF-8・必要相関の型を検査します。
Data全モデルのschema、state hashの真正性、状態遷移の可否はData側とP実体照合に残します。

COMPLETIONのcompletionはData `WorldManagedWriterCompletion`の形であり、
request全体が元Requestedと構造・型まで一致し、wire requestSha256も一致する必要があります。
BLOCKED/COMPLETE/pendingCount整数0、全writer CLOSED、writer/childの非重複を検査します。
ただし空集合が正しいか、全件か、最新revisionか、snapshotIdが新規か、元Runningから正しく
進んだRequestedかはparser単独では証明できません。Pの正本との同transactionでの予約・
freezeとDataの最終検証が必要です。旧snapshotへ新challengeを差し替えた応答は、
元Requestedとの構造照合で拒否します。

GUIのwriter-session identityをDataの死亡必須集合へ入れません。
session CLOSEDとP所有proxy C/W・全childの実終了を別に観測し、履歴を削除して空集合にしません。

## error・同要求readback

error envelopeのphaseは元request phaseです。payloadは厳密に
`requestBodySha256,code,outcome`で、outcomeは
`REJECTED_BEFORE_EFFECT|PENDING|UNKNOWN`です。元要求との相関は成功応答と同じです。
`retryable`や新ID再送を許す項目はありません。

`same_request`は同ID/nonceを含む元frame bytesの一致だけを判定します。
結果不明は保全し、同要求のreadStatus/resumeへ接続する責務は後続です。
Requestedの返答が不明でも新prepare/challengeへ自動retryしません。
malformed/未認証入力へ成功相関を生成するAPIはありません。

## APIと共有vector

`mod_control_wire.py`は完成frameとdescriptor bytesを受け取るpure parserです。
受信timeout・分割read・OS peer認証・秘密/ログの扱い・応答発行を担当しません。
`decode_frame`の成功だけでadmitしてはいけません。

`mod-control-vectors-v1.json`をPython/Kotlin共用の固定fixtureとします。
`descriptorJson`、`requestJson`、`responseJson`は元UTF-8文字列、
`*FrameHex`はprefix込みbytesの16進表記です。
正常4往復、3種error、構文拒否、相関拒否を含みます。
Dataのstate hash・native identity・snapshot・台帳値は合成値です。
ACLやpeer確認を済ませたfixtureとして使用してはいけません。
単体試験は別途ちょうど64KiB/1byte超過、巨大集合、inner原文差分を検査します。

```powershell
py -3.12 -B -m unittest discover -s scripts -p test_mod_control_wire.py -v
```

後続P統合では既runtime/writer正本と同transactionで予約・遮断・revision・完全台帳を設計します。
start/stopの待機はinstance OS lockとDB transactionの外です。既4 worker poolへcontrol待機も共有すると
worker枯渇でhandshakeが進まないため、実接続には別bounded制御進行が必要です。
未接続の予約table・新daemon・汎用SQL/path・認証fallbackはこの先行成果に含めません。

## 原bytes正本とUUIDの先行接続

既runtime_operationsを使う未接続のMod専用readbackと、
検証済みData runtimeだけに適用するUUID境界は
[正本・UUIDガイド](mod-control-operations-guide.md)を参照してください。
native受付・台帳最新性・停止成功は引き続き未接続です。


## wire2 pure契約（未接続）

`decode_frame_v2`、`correlate_response_v2`、`correlate_grant_v2`、`correlate_binding_v2`、`same_request_v2`は外wire2専用です。descriptorは既version1、profile証拠はinner version1であり、v1や異版errorへのfallbackはありません。v1の関数・vectorは維持します。通常launch、native本人/peer/実ロード元、Data begin、journal、Management受付には接続していません。

Pが検証済み原本から作るcontextはexact `{descriptorJson, owner, management, expectedEvidence}`。descriptorJsonは元UTF-8文字列、expectedEvidenceはexact `{platform,jdk,profileSha256,codeSources,mods,nativeSources}`です。Mから受けた証拠をexpectedへコピーして承認してはいけません。管理Data境界のlease/generationはcanonical UUIDを要求し、旧32桁値を自動変換しません。

EvidenceのTextNは非空・NUL禁止・UTF8 byte上限（whitespace単独可）、既identity/binding/Data抽出のnotBlankとは区別します。各source/native/mod idとsourceIdsはstrict昇順で重複を拒否し、承認集合とorigin/identityを完全一致させます。Darwin OS_CACHE_IMAGEは同OS buildの承認cache/image UUIDを必要とします。

GRANTは元HELLOの証拠hash、ACKは元BOUNDの証拠hashをechoし、各応答/errorは元body全bytesのhashへ相関します。BOUNDの`pre_evidence_json`はcallerが元HELLOから保持した原文です。PREとBOUNDはcheckpoint以外の構造・集合・originが同一で、rawhash同一は要求しません。意味同でも別rawのrequestは同要求readbackではありません。

完成frameは4byteBE込み65536以下、body1..65532。innerの二重escapeとBOUNDのData status原文も含めて検査し、切捨て/分割/外部参照をしません。共有`mod-control-vectors-v2.json`と別境界vectorは合成値です。Data全モデル・serializer hash・最大status・実candidate収容・native/OS受入を証明しません。Data原文へEvidenceのexact-field/null拒否を再帰適用せず、既相関抽出とData側最終検証を維持します。

共有vectorのbodyHex/frameHexが元bytesです。表示用bodyUtf8やcandidate metadataはAPIのcontextへ渡しません。両言語のencoderをcanonical化する契約はなく、それぞれの原bytesをhashし相互decodeします。native/実profile/peer/台帳耐久性/停止成功/各OS受入は後続の結合・明示許可検証まで未完です。

## Mod専用native transport（caller未接続）

`mod_control_transport.py`は、run限定のdescriptor・native本人・raw frame I/Oの資源ownerです。
通常CLI、runtimeのstart/stop、wire要求の業務処理にはまだ接続していません。
WindowsとmacOSの専用fixtureで、echo・frame上限拒否・accept timeout取消終端の3ケースをそれぞれ確認しました。
固定699件のsuiteはWindowsが685成功・14skip、macOSが670成功・29skipで、両OSのCLI helpも終了code 0です。
確認範囲は固定入力の試験と既知の自己所有資源です。suite内部・全系譜・全資源の終端や実profile受入を証明しません。
既wire成功、旧runtime試験成功、同UID確認を未接続callerの受入へ読み替えません。

`ModControlTransport(instance_ids)`はinventoryの有限instance集合を受け取り、同instanceにrun枠を一つ持ちます。
`create(context, raw_descriptor)`はcaller固定のmanagementRoot、instanceId、leaseId、整数epoch、
generationId、runtimeRunId、controller（P本人）、descriptorSha256のexact contextへ原bytesを照合します。
本文から期待root/run/Pを採用しません。P本人を保持してから、固定parentに対する新規descriptorを作り、
同handle/fdのsize・通常file・nlink1・owner/権限・identity・原bytesを照合します。既存fileは拒否します。
Windows親directoryの本人比較はvolumeとFileIdを使い、可変のsize・archive属性・linksは本人に含めません。
毎回の通常directory・DeletePending無し・reparse無し・private DACL検査と、leafの全metadata/原bytes照合は維持します。

返されたendpointの`accept(expected_m_identity, deadline)`には、Pが生成・保持したMのData形式本人を渡します。
HELLOやdescriptorからM期待本人を生成しません。Windowsは同pipeのpeer PIDと保持process handleの
FILETIME/owner/host/boot、macOSは同socket fdのUID/PIDとnativeのsec:micro/host/bootを照合します。
既`runtime_process.identity`の一時handleやlsof実行を、そのまま保持本人の証拠にしません。

接続の`receive_frame(deadline)`は元frame bytesを返し、`send_frame(raw_frame, deadline)`は確認済み転送数を返します。
deadlineはmonotonicの絶対時刻です。macOSはselect復帰後、新syscallの前にもdeadlineを再照合します。
headerは4byte固定で受け、長さ取得直後にbody1..65532を検査します。
全frame上限65536はheader込みです。JSONの再serialize、切捨て、分割送信プロトコル、自動再送はしません。
同じ接続の並行I/Oを待ち列へ入れず拒否し、受信・送信の前後でpeerとdescriptorを再照合します。

run内の接続枠は一つです。資源はnative発行前に上位ownerへ強参照で登録します。
期限後のCancelIoExは取消要求だけであり、同OVERLAPPEDの終端までevent/buffer/pipeを保持します。
ERROR_NOT_FOUND、WAIT_TIMEOUT、IO_INCOMPLETEを終端とみなしません。
`TransportError.transferred`は当該frameの確認済みbyte数、`cancel_pending`は未終端資源の保持を表します。
未回収枠への新accept/createを拒否し、`reap()`は保持I/Oの終端だけを非blockingで確認します。
新thread/daemon/killや期限だけの回収はありません。constructor途中の資源もownerに残します。初期化はendpointの局所排他、acceptは公開前に取得した接続排他で守り、
close/reapは使用中の資源を回収しません。全体lockは辞書・shutdown受付の更新だけに限定します。
旧endpoint/接続の再closeでは同objectだけを外し、新run/接続を消しません。
closeが資源を消費したか不明なら単調UNKNOWNを保持し、同じfd/handle値へ自動再closeしません。
取消未終端は終端観測後だけ回収しますが、close不明は期限や同値再利用で正常化しません。
`close(deadline)`は受付を閉じて有限確認し、未終端・使用中・close不明ならFalseです。
callerはownerをsupervisor寿命で保持し、Falseを正常shutdownとして扱ってはいけません。

transportはRegistry/DB・予約・業務作用を操作しません。将来のP callerが、認証済み完成要求を
既`mod_control_operations.reserve`で短tx commitした後に業務作用へ進む責任を持ちます。
未認証/不完全frame/確定した予約前拒否には業務作用・新operation・成功相関を生成しません。
予約commit不明や未完了操作の送受信不明はcallerが同ID/rawへUNKNOWNを結束します。
既COMPLETED原responseの保存後に送達不明となっても完成正本を格下げせず、同要求readbackを維持します。
積極確認できた作用前拒否は既error契約へ従います。通常CLIへのcaller接続は未実装です。privateの先行受付は後述します。

mockはpartial read/write、header込み上限、context/peer差替え、descriptor実体検査、取消正常競合、
未終端資源・枠・constructor故障を確認します。DLL/socket/process/file-openは禁止spyで検査します。
実nativeの有限3ケースと両OSの固定suiteは、個別の明示許可で検証しました。実運用callerへの接続・総合受入は未完です。
新DB/schema/台帳、GRANT/Data begin、停止成功、profile承認は追加していません。
COMCTL/conhost等の診断例外も製品採用していません。

## P private startupとrun受付門（先行接続）

`RuntimeControl._managed_startup`は、Pが独立保持したexact profile原bytes、検証済みbundle、
独立Observed、descriptor/P正本context・expectedEvidenceを明示供給するprivate入口です。
通常startから自動転送せず、公開command・実起動・承認供給元・通常start迂回拒否は未接続です。
候補の存在・schema2・Data座標からprofileを採用せず、新承認recordやLEGACY flagは設けません。

native peer認証後のHELLOから元operationId/requestNonceへ相関するGRANTを生成し、
固定P managementと既wire2で検査して元bytesを既runtime_operationsへ保存します。
BOUNDは元HELLOのPRE原文とP正本へ照合し、検証RunningからACKを生成・保存してから送信します。
受信nonceでPのrun nonceを更新しません。ACK全bytesの送信後だけ同revisionをCASして
OPEN/RUNNINGを同短transactionで確定します。P runに保持した元start operationId/fingerprintと
同instance/lease/epochのartifact予約を照合し、その同transactionで元起動結果をCOMPLETEDへ保存して
当該artifact参照だけを解放します。runは参照だけ、start rowは既起動結果、wire rowはACK原文を保持します。
通常executeは専用descriptorに結束した門を完成させず、完成ownerをprivate callerに限定します。
保存・送信不明なら門を開けず、
同所有PENDINGの元起動だけをRECOVERY_REQUIREDへ保全します。COMPLETED原応答をUNKNOWNへ格下げしません。
OPEN/start完成commit後の返答不明で完成済み起動やACKを格下げしません。
`_managed_readback`は保持した認証sessionで同BOUNDを受けたとき保存ACKだけを返し、元startのPENDINGを
要求せず、門/revision/起動完成/参照解放を再作用しません。別run/旧世代・別原要求は拒否します。
同要求readbackは新IDでの再作用・自動再送の許可ではありません。
startupとreadbackは同sessionで同時に進めず、先行処理中は受信frameを消費する前に拒否します。
失敗時はmutation前に保持したP checkpointから元起動を保全し、wire原文の保全とは別transactionにします。
malformed/競合wireの保全不能で元startの保全を止めず、descriptor結束commitの返答不明も同じ保全対象です。

fresh Registry v5にはrunごとのlease/epoch/generation/nonce・門・revisionを保持します。
旧v4 DB/markerは拒否・保全し、自動ALTER、旧root再初期化や原本の書換えは行いません。
通常startは従来の起動proofと同transactionで門を開き、通常stopは既pending/claim保護を維持して
停止予約と閉門を同transactionで行います。門行欠落をOPENへ補完しません。

新writer-open/ioとDB acceptはRUNNING・未pending・OPENと同run/lease/世代/nonce/revisionを
予約transactionで再確認します。private managed stop予約は新受付をBLOCKEDにします。
同run/sessionの受理済みoperationに結束したfinish・DB記録・closeはSTOPPING/pendingでも完結でき、
終了済みsession・別run・旧世代への新admitには広げません。STOPPING中のCLOSED sessionは
同run/session・元close operationId/fingerprintのCOMPLETED応答読戻しだけを許可します。閉門をdrain・全保存・reap・全系譜終了の証明にしません。

制御進行は同期でinventoryのinstanceごとに1枠、未終端/不明資源と原入力はownerへ保持します。
DB/instance lock内でnative・FS・受送信待機をしません。新threadは追加せず、
実supervisorの4worker枯渇を解決した扱いにはしません。

局所検証はSQLite memory、合成wire/Profile、bundle I/O mockとnative adapter fakeを使用します。
受理済みDB drainは共有memory SQLiteの独立2connection・単一threadの5件でも確認します。
停止閉門後のfinish、別connectionから見た保存後ACK、close失敗時のrollback、
UNKNOWNの確定と格下げ拒否、CLOSED読戻しの無作用を対象とします。
これはtransaction順序・commit可視性の検証であり、同時write、BUSY、別process、OS耐久性の実証ではありません。
全750件はWindowsで736PASS・14SKIP、Macで721PASS・29SKIP、FAIL/ERROR 0とCLI help exit0をOSごとに確認しました。
実P/M起動・profile/native loadの実証・実OS startgate・旧root・全系譜/全資源の終端は未検証です。
前成果の両OS699件/native3成功を本変更の再成功へ読み替えません。

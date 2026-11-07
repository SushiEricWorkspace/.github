# 停止中controller Cの固定入口

.github #64の第1・第2スライス（Cのowner/Assigned）、第3スライス先行部分（Wのprivate入口）、第4スライス先行部分（P内部起動部品）、第5スライス先行部分（Registry正本予約とC取得）、第6スライス先行部分（正本予約からW待機）、第7スライス先行部分（正本PREPAREDからC Assigned）、第8スライス先行部分（Assignedから候補packet）、第9スライス先行部分（W/全child終了観測）を扱います。停止中writerの公開受付は有効化していません。現在のwriter-ioはRunning限定です。

## 責務と固定配布

`offline_controller_bundle.offline_controller_command(bundle)`は完成bundleの全hash、同版DataModel Host配布、source・compiler・専用JAR・作成OSを照合し、固定JDK21と`OfflineControllerMain`の起動配列だけを返します。公開Host/CLIの管理root拒否は維持します。ローカルJDK、任意classpath、実行時のsource探索へ切り替えません。

新しいsource-lockでbundleを生成すると、`adapter/offline-controller.json`と`adapter/offline-controller/`を収録します。旧bundleの検証は維持しますが、helperのない旧bundleからこの入口を呼べません。既存bundleへ後付けせず、新bundleを生成してください。DB・run-stateの保存形式変更はありません。

Cは自分のData121 `openManagedOfflineWriter`／writer `assignWorker`／writer `close`だけを呼びます。ゲームのbeginRun/observeExit、119 Editの実行、receiptのcomplete、snapshot操作は受け付けません。元sealと不変chain・Ready・DBcloseの照合はData121へ委ねます。

## P側の前提

このmainはprivate inherited stdin/stdout専用です。ネットワークendpoint・任意SQL・Host許可flag・root常駐daemonを追加しません。Pは同じinstance/world境界でstart排他を取得し、元M/全childの終了、最新game sealと既存receipt、他writer受付遮断・accepted drainを確認した後にだけCを起動します。PがCの固定artifact・実PID/start/host/bootを観測し、C専用OwnedProcessと私設pipeを所有する必要があります。

PによるC/Wの独立OwnedProcess生成・本人固定と最初のprivate要求を扱う内部起動部品は、後述の「Pによる限定起動（第4スライス先行部分）」を参照してください。Registry予約からの内部接続は後述の第5・第6スライス節を参照してください。公開呼出元と実OS受入は後続です。Cのopenに渡すdrain hashはPの照合値であり、CがRegistry台帳の完全性を証明するものではありません。C側でもPの完全native identityの生存、実parent PID、C本人を照合します。起動引数は受け付けず、root・秘密・本文をcommand lineへ載せません。

## 私設frame version 1

長さは4-byte big-endian、UTF-8 JSON object。headerを含め65,536 bytes以下。本文の改行・不正UTF-8・部分frame・余分field・未知commandを拒否します。読み待ち期限とP死亡時のpipe切断はPが管理します。Pの全JSON objectキーは重複無しで送ります。

openは次のfieldだけを持ちます。

| field | 内容 |
|---|---|
| command | `open` |
| operationId / requestHash | P側要求IDとそのSHA-256。応答で同値を返す。hashの正本との照合はPが行う |
| sessionId / nonce | Manager接続と起動の識別。小文字英数字・ハイフン、128文字以下 |
| leaseId / epoch | Data管理bindingのUUIDと非負整数 |
| generation / worldUuid / runUuid | Data側の標準小文字UUID。RegistryのgenerationId/runtimeRunIdとの対応はPが固定する |
| controller / owner | PとCの `hostHash, bootId, pid, startId`。Pの実観測値を使用 |
| drainSha256 | Pが固定した同じ受付遮断/drain証拠hash |
| worldRoot / shopDirectory | Pが世代内へ固定した絶対path |
| expectedStateSha256 / expectedRevision | 最新Readyのhash・非負revision。自動で現在stateへ追従しない |

応答`OPEN`は同じoperationId/requestHashとsessionId/nonceに加え、Data owner取得後の`stateSha256, stateRevision, runUuid`を返します。Data自身がwriter UUIDを発行するため、このrunUuidはopen前と異なります。

closeは`command=close, operationId, requestHash, sessionId, nonce, openOperationId, expectedStateSha256, expectedRevision`だけを持ちます。openOperationIdは元open ID、期待stateは直前OPEN応答と一致させます。closeのoperationIdはopenと別にします。C本人close後の`OWNER_CLOSED`は新Readyのhash/revision/runUuidを返します。

`OWNER_CLOSED`はDB側のowner終了だけを表します。PがC/全childの実exitを観測し、Registry writer CLOSEDを確定するまでMod startを許可してはいけません。Data ReadyだけでCLOSEDを推測しません。

## Assigned登録（第2スライス先行部分）

`assign`はCが開いた同じownerの初回`OfflineIdle`からだけ受け付けます。PはDB未openで待機するWを固定bundleから独立Job/groupとして所有し、私設IPCの認証と実PID/start/host/bootの観測を完了してから送る必要があります。内部起動部品は後述の第4スライス節を参照してください。Registry予約からの内部接続は後述の第5・第6スライス節を参照してください。公開呼出元と実OS受入は後続です。Wのprivate入口とData連携は後述の第3スライスで追加しています。C側の生存照合だけでWのOwnedProcess所属を証明したとは扱いません。

| field | 内容 |
|---|---|
| command | `assign` |
| operationId | 119 EditのrequestIdと同じ標準小文字UUID。open IDとは別 |
| requestHash | PのIPC要求を識別するSHA-256。従来open/closeと同様に正本照合はPの責務 |
| sessionId / nonce / openOperationId | 元openと完全一致するbinding |
| expectedStateSha256 / expectedRevision / runUuid | 直前OPENのData state。現在値へ自動追従しない |
| worker | Pが観測したWの完全`hostHash, bootId, pid, startId` |
| request | Data121 `HostAdapterCodec`形式のJSON object。元worldUuid・operationIdと同じrequestId、`HostAdapterAction.Edit`だけ |

root・generation・leaseはopen時のbindingから引き継ぎ、assignでの上書きを受け付けません。余分field、期限切れ要求、別world・別操作、P/Cと同じPID、別host/boot、生存不明のWをData登録前に拒否します。要求codecと商品操作hashはData実装をそのまま使用します。

`ASSIGNED`は従来status応答と同じ`operationId, requestHash, sessionId, nonce, stateSha256, stateRevision, runUuid`、およびDataが公開した`OfflineAssigned.requestSha256`を`editRequestSha256`として返します。IPCの`requestHash`と、期限を除外し順序を正規化するDataの商品操作hashを混同しないでください。writerのsession/runUuidは維持し、revision/hashだけが次へ進みます。

一度登録するとこの入口では再登録・worker差替・recoverできません。Assignedのままcloseを要求してもDataが拒否し、ownerを解放しません。Wのadmission・Edit・候補公開は限定入口として追加しています。W/全child終了のP観測は後述の第9スライスを参照してください。C completeは後続のため、現時点のhelperだけではAssignedを完了できません。製品のwriter受付からの呼出元は追加しておらず、通常利用へ公開しません。

## 失敗時

owner取得を試みる前の拒否だけを`REJECTED`とします。Data取得開始後は公開済みの可能性を含め`UNKNOWN`と非正常exitで保全します。取得後の不正assign/close・二重open、Assigned公開後の例外・応答喪失、close後の応答喪失、EOFで自動close・rollback・再登録・再取得しません。UNKNOWNには元ID・本文・processとstate証拠を保持します。新Cで本人APIを代行しません。

W/全child終了のP観測は第9スライスで接続しています。同要求と不変終了証拠を照合する正規recover、Cの実終了とRegistry CLOSED、start gateは後続スライスです。今回のhelperだけで停止中編集や復旧を完了扱いにしません。

## 検証

Python3.12の合成bundle fixtureで収録・改変・外部classpath・欠落を確認します。`SUSHIERIC_OFFLINE_TEST_BUNDLE`に固定Data121 bundleを明示すると、同bundleのJDK21/Host依存でJava compileと非native protocol vectorsを実行します。fake Data/本人APIによるopen/assign/close/EOF/UNKNOWN試験はDBや実worldを作りません。固定Data artifactへのcompile試験とfakeによる状態機械試験を分けて評価してください。

実owner取得、実root、両OSの本人・Job/group・私設pipe、実bundleビルド、Mod/Manager結合、#54のDB snapshotは別途受入が必要です。


## 単発worker W（第3スライス先行部分）

`offline_worker_command(bundle)`は同じ固定helperのnested main `OfflineControllerMain$Worker`を返します。Cのmainへ引数を渡してWを同じプロセス内で実行する経路はありません。C/Wは同じJARでもPが各々独立のOwnedProcess Job/groupで所有する必要があります。その内部起動部品は後述の第4スライス節を参照してください。Registry予約からの内部接続は後述の第5・第6スライス節を参照してください。公開呼出元と実OS受入は後続です。旧helperにWorker classがない場合はworker commandだけを拒否し、従来のC commandの検証は維持します。

実entrypointのSystem.outはPrintStreamのため、共有frame出力はflush後にcheckErrorも照合し、下位のIOExceptionがPrintStream内で抑止された場合も非正常終了へ進めます。C close後・W候補公開後のPrintStream出力切断をfixtureで検証しています。

Wはprivate inherited stdin/stdoutで2フレームだけを受け取ります。サイズ・UTF-8・余分field・部分frameの規則はCと同じです。Pは認証済private pipeの唯一の相手であること、DB未openで待機する固定Wのartifact/実PID/start/host/bootを先に確認してください。今回のnative生存照合だけでこの認証・所属を証明したとは扱いません。

1. `prepare`: `command, operationId, requestHash, sessionId, nonce, leaseId, epoch, generation, controller, owner, worker, drainSha256, worldRoot, shopDirectory, worldUuid, runUuid, expectedStateSha256, expectedRevision, request`のみ。controller=P、owner=C、worker=Wの完全identityです。operationIdは元Editの標準小文字UUID、requestHashはIPC要求hashです。元CのIdleのhash/revision/runUuid・writer/managementと、型付き119 Editのworld/requestIdを照合します。P実parentとP/C/W本人の生存確認後にPREPAREDを返します。ここではadmissionもDBも開きません。
2. PがCの既存assignへ同じW/要求を渡し、CのASSIGNED応答を照合してから、`admit`を送ります。admitは`command, operationId, requestHash, sessionId, nonce, preparedStateSha256, expectedStateSha256, expectedRevision, runUuid, editRequestSha256`のみ。preparedStateSha256は元Idle、期待stateはCが公開した初回Assigned、editRequestSha256はDataの商品操作hashです。request本文の差替fieldは受け付けません。
3. Wは同じwriter・元Idleへのprevious state・W本人/op/Data hash・非recoverを照合し、元prepareの型付き要求を変更せず`beginOfflineEdit`と、本人が開いたDBの`shopDefinitions(...).execute`へ渡します。Dataの各作用gateがP/C生存と同じworld/run/revision/要求を再照合します。Wは全自分の接続close後、既知responseだけを`operation.finish`へ渡します。Dataのowner/seal/state/hash/receipt算法はhelperへ再実装しません。
4. CANDIDATEは`status, operationId, requestHash, sessionId, nonce, stateSha256, receiptSha256, worldUuid, runUuid, stateRevision, editRequestSha256`を返します。DataではAssigned時点のrunUuidがwriter session、Pending以降は操作UUIDへ変わります。最終SUCCESSやRegistry CLOSEDではありません。Wは最終ACKを読まず終了し、PがW/全child実終了を観測しCが同候補をcompleteするまでManagerへ完了を返してはいけません。

prepare/admitでのREJECTEDはWのadmission未試行を表すだけで、C ownerをcloseしません。admission試行後の例外・Edit UNKNOWN・DBclose失敗・候補公開後の例外/応答喪失はUNKNOWNまたは返答不能の非正常終了として元stateを保全します。再admission・Edit再送・finish再試行・C差替・自動recoverは行いません。EOF・CANDIDATE・Wの終了予定だけで実終了を推測しません。

今回の固定Data compile/非native fakeは、Idle→Assigned→Pending→SavedPendingExitと非空要求の透過性、作用前拒否・公開後失敗を検証します。実DB/実root/両OSの本人・IPC・Job/group・実bundleビルドの受入ではありません。起動認証/所有の内部部品は後述の第4スライス節を参照してください。W/全child終了のP観測は第9スライスを参照してください。Pの受付遮断/drainを含む製品呼出元への組込みと実OS受入、C complete、Registry CLOSED/start gateは後続です。

## Pによる限定起動（第4スライス先行部分）

`OfflineProcess`はP内部専用で、CのOPEN、その保持Cへ束縛した別WのPREPAREDまでを扱う。
生成前に呼出元がobjectを保持し、`verify`でlease/epoch/generation・受付遮断・accepted drain・Mod start排他を再照合する。
`native`には既存ModControlTransportのnativeを渡す。固定bundle検証、OwnedProcess、private匿名stdio、Data形式本人識別を再利用する。
C/Wの各groupと実PID/start/host/boot、cwd/executable/command/OS ownerを観測し、私設frameのcontroller/owner/workerとsession/nonceをPが固定する。
任意commandや本人欄を渡すAPIではなく、Cはopen、Wはprepareだけを一度送る。非空の型付きrequestは変更しない。
生成/送受信/応答照合/記録の失敗は例外とし、再送・別helperへの差替をしない。object/handle/pipe/元要求・応答を保持し、本文・秘密は監督記録に残さない。
Registry内部呼出元は第5・第6スライスで接続している。公開writer受付は後続である。Assigned登録は後述の第7スライスで接続する。admit/候補packetは第8スライスで接続する。W/全child終了のP観測は第9スライスで接続する。C実終了、C complete/close、Registry CLOSED/start gateは後続であり、OPEN/PREPAREDを編集成功や終了証拠に使わない。
実Windows/macOS本人受入・実root・実bundle起動は未確認。合成native/pipe fixtureと既存Java非native vectorsの成功を実機受入へ拡張しない。

## Registry正本に束縛するC取得（第5スライス先行部分）

P内部の`RuntimeControl._offline_open`は、同lease/epoch/generationの認証済runが正常保存・M/全child退出を完了し、受付BLOCKED・全writerの耐久close/実proxy退出・未完了要求不在が正本で一致する場合だけCを取得する。
P内部入口でも既存のresource_useでGC/監督停止と調停する。同instance lock内の短txでgate revision、offline-open PENDING、artifact参照、instances.pendingを予約し、DB外で保持Cを起動する。各I/O前に同予約と元run/descriptor/停止proof/drain snapshot・旧M PID不在を再照合する。
Data Readyのworld/run/hash/revisionは期待値としてCへ渡し、実state・seal/chainはData本人APIが検査する。Registryの正常停止だけからData Readyを推測せず、任意rootや空verifyを渡す入口は作らない。
OPEN後も予約/PENDINGを保持する。生成/通信/照合/正本記録の失敗で再取得・別Cへの差替・自動rollbackはしない。元Cと生成資源を保持し、start/lease解放を拒否する。
この入口はP内部用で、Manager/CLIの停止中writer受付は未公開。正本予約からW PREPAREDまでは第6スライスで接続している。Assigned登録は第7スライスで接続する。admit/候補packetは第8スライスで接続する。W/全child終了のP観測は第9スライスを参照する。C実終了/complete/close、Registry CLOSEDと予約解放は後続である。合成SQLiteと既存Java/native fakeの検証を実OS/実root受入へ広げない。

## 正本予約からW待機（第6スライス先行部分）

P内部の`RuntimeControl._offline_prepare(token, operation_id=..., request=..., deadline=...)`は、同Pが保持したC OPEN原文とRegistryの記録hash・元予約/閉門/drain/停止証拠を再照合する。元offline-open PENDINGのresponseへW操作IDとIPC要求fingerprintを先に記録し、独立OwnedProcessのWを保持してPREPAREDまで進める。型付きEdit本文はそのままDataへ渡し、Dataの商品操作hash算法をPythonへ移さない。

`OfflineProcess.acknowledge_startup`は、呼出元が正本へ記録したOPEN/PREPARED応答hashを照合し、本人再検証とローカル記録後にだけ既存ProxyPipeのbufferを解放する。送受信失敗・応答不一致・記録失敗なら次frameへ進まず、別要求・再生成・再送で回避しない。W応答が返ってもoffline-open PENDINGとartifact参照を保持する。

Assigned登録は後述の第7スライスで接続する。W admitは第8スライスで接続する。WはDB未openで待機し、実終了・C complete/close・Registry CLOSEDを完了扱いにしない。game候補のData退出→Ready供給と公開writer受付は後続で、今回もReady期待値をC本人が検査する。実OS/native受入は未確認。

## 正本PREPAREDからC Assigned（第7スライス先行部分）

P内部の`RuntimeControl._offline_assign(token, operation_id=..., deadline=...)`は、保持Wの記録済みPREPARED原文hash、元CのIdle、管理bindingとRegistry正本予約を照合し、assignment意図を同instance lock内の短transactionへ先行記録する。本文は呼出元から再受領せず、元Wの非空の型付き要求・IPC fingerprint・P観測の本人をCの既存assignへ渡す。Data自身がAssignedとeditRequestSha256を生成し、PythonはDataの正規化/hash算法を再実装しない。

CのASSIGNEDでは同op/IPC hash/session/nonce/runとrevision+1、state hashとData editRequestSha256を照合する。元OPEN/PREPARED原文は維持し、ASSIGNEDの正本記録と本人再照合・ローカル記録後だけC pipeを解放する。失敗時は例外で元要求/応答・C/W・PENDING/参照を保持し、再登録・再送・差替しない。

W admitと候補packetの内部接続は後述の第8スライスを参照する。WはPREPAREDのままDB未openで待機する。W/全child終了のP観測は第9スライスを参照する。game候補のData退出→Ready供給、C実終了/receipt/CLOSEDと公開受付は後続。実OS/native本人受入は未確認。

## 記録済みAssignedから候補packet（第8スライス先行部分）

P内部の`RuntimeControl._offline_admit(token, operation_id=..., deadline=...)`は、元C/Wと記録済みASSIGNED・ACKを再照合し、同instance lock内の短transactionでadmission意図を先行記録する。元worker/assignment記録を維持し、WへはC本人のAssigned hash/revision/run・Data editRequestSha256と元PREPARED hashだけを送る。本文を再受領せず、Java Wが保持する元HostAdapterRequestをData119へ渡す。

WはDBclose・候補公開後にACKを待たず終了する。送信前は元Wの本人も照合し、候補受信中は元専用pipe/OwnedProcess handleへのbindingとP/C・Registryを検証する。poll0なら旧W PIDを再解決せず受信でき、PID再利用で別processを採用しない。非zero exitは拒否する。poll0だけで全child終了やC completeを確定しない。

CANDIDATEは同op/IPC hash/session/nonce/world、run=操作UUID、Assigned revision+2、Data editRequestSha256とstate/receipt hash形式を照合し、正本へ応答hashを記録する。候補後はWへACK/次frameを送らずbufferとhandleを保持する。PENDING/参照を解放せず、packetだけで最終SUCCESSへ昇格しない。失敗時の再admit・再生成・差替・自動rollbackはない。

W/全child終了のP観測は後述の第9スライスを参照する。候補実体/hashのC本人照合、receipt complete、game候補のData退出→Ready供給、C close/Registry CLOSEDと公開受付は後続。Ready期待値を自動確定せず、実OS/native本人受入は未確認。

## 記録済み候補からW/全child終了観測（第9スライス先行部分）

P内部の`RuntimeControl._offline_observe_worker_exit(token, operation_id=..., deadline=...)`は、元Assigned・候補packetのローカル/Registry記録と元予約を再照合し、終了観測意図を同instance lock内の短transactionへ先行記録する。元Wの保持OwnedProcess handleの正常exit0と独立Job/groupの全child不在を観測した後だけ、ローカル証拠とRegistryへの応答hashを記録する。OS観測と最後の本人・正本照合の完了後、ローカル成功証拠を書き始める前に同じdeadlineを再検査し、Registry側でも最終照合後・結果更新前に検査する。待機はtransaction外で行い、その間も元P/Cの生存・bindingと正本を再照合する。旧W PIDのidentity取得や別processへのattachは行わない。

非zero/不明終了、残child、期限切れ、正本/本人不一致、記録失敗・応答喪失は例外で元証拠・C/W・PENDING/pending/参照を保全し、自動kill・再生成・再観測/replayを行わない。Wの候補pipeは解放せず、Cへ追加frameも送らない。戻り値`WORKER_EXITED`はPによる内部の途中観測結果だけであり、候補実体/receiptのC本人照合・complete、編集SUCCESS、Data Ready、Registry CLOSEDを表さない。

C本人による同候補/receipt実体照合・complete、同接続連続操作、game候補のData退出→Ready供給、C終了とRegistry CLOSEDは後続。Ready期待値を自動確定しない。実OS/nativeの本人/Job/group/child終了受入は未確認。

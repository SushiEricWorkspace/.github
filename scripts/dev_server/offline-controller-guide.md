# 停止中controller Cの固定入口

.github #64の第1・第2スライス（Cのowner/Assigned）、第3スライス先行部分（Wのprivate入口）、第4スライス先行部分（P内部起動部品）、第5スライス先行部分（Registry正本予約とC取得）、第6スライス先行部分（正本予約からW待機）、第7スライス先行部分（正本PREPAREDからC Assigned）、第8スライス先行部分（Assignedから候補packet）、第9スライス先行部分（W/全child終了観測）、第10スライス先行部分（C本人complete入口）、第11スライス先行部分（P終了証拠からC確定/正本記録）、第12スライス先行部分（記録済みIdleから次W待機）、第13スライス先行部分（同Cの操作別保持と連続編集）、第14スライス先行部分（game候補のData退出入口）、第15スライス先行部分（P内部の単発観測C接続）、第16スライス先行部分（認証済保存原文とstop正本の接続）、第17スライス先行部分（認証済Mの生成handle保持）、第18スライス先行部分（保存原文と保持元Mの結束）、第19スライス先行部分（不変候補原本の入力結束）、第20スライス先行部分（同stopから観測Cへの単発接続）、第21スライス先行部分（観測済みgame退出証拠とstop確定の結束）、第22スライス先行部分（P内部の同stop専用候補入力と観測開始）を扱います。停止中writerの公開受付は有効化していません。現在のwriter-ioはRunning限定です。

## 責務と固定配布

`offline_controller_bundle.offline_controller_command(bundle)`は完成bundleの全hash、同版DataModel Host配布、source・compiler・専用JAR・作成OSを照合し、固定JDK21と`OfflineControllerMain`の起動配列だけを返します。公開Host/CLIの管理root拒否は維持します。ローカルJDK、任意classpath、実行時のsource探索へ切り替えません。

新しいsource-lockでbundleを生成すると、`adapter/offline-controller.json`と`adapter/offline-controller/`を収録します。旧bundleの検証は維持しますが、helperのない旧bundleからこの入口を呼べません。既存bundleへ後付けせず、新bundleを生成してください。DB・run-stateの保存形式変更はありません。

CはData121 `observeExit`／`openManagedOfflineWriter`／writer `assignWorker`／writer `complete`／writer `close`を限定して呼びます。game退出の単発入口は第14スライス節、completeは第10スライス節を参照してください。ゲームのbeginRun/finishSaves、119 Editの実行、snapshot操作は受け付けません。元sealと不変chain・Ready・DBcloseの照合はData121へ委ねます。

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

closeは`command=close, operationId, requestHash, sessionId, nonce, openOperationId, expectedStateSha256, expectedRevision`だけを持ちます。openOperationIdは元open ID、期待stateは照合・正本記録済みの最新Idle（初回OPENまたはCOMPLETED応答）と一致させます。closeのoperationIdはopenと別にします。C本人close後の`OWNER_CLOSED`は新Readyのhash/revision/runUuidを返します。

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

未完了Assignedの再登録・worker差替・recoverはこの入口ではできません。Assignedのままcloseを要求してもDataが拒否し、ownerを解放しません。Wのadmission・Edit・候補公開は限定入口として追加しています。W/全child終了のP観測は後述の第9スライスを参照してください。C本人completeの限定入口は第10スライスにあります。Pの終了証拠からの呼出し・応答正本記録は第11スライスを参照してください。連続操作は後続です。製品のwriter受付からの呼出元は追加しておらず、通常利用へ公開しません。

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

今回の固定Data compile/非native fakeは、Idle→Assigned→Pending→SavedPendingExitと非空要求の透過性、作用前拒否・公開後失敗を検証します。実DB/実root/両OSの本人・IPC・Job/group・実bundleビルドの受入ではありません。起動認証/所有の内部部品は後述の第4スライス節を参照してください。W/全child終了のP観測は第9スライスを参照してください。C completeの限定入口は第10スライスを参照してください。Pの受付遮断/drainを含む製品呼出元への組込みと実OS受入、Registry CLOSED/start gateは後続です。completeのP呼出し/正本記録は第11スライスを参照してください。

## Pによる限定起動（第4スライス先行部分）

`OfflineProcess`はP内部専用で、CのOPEN、その保持Cへ束縛した別WのPREPAREDまでを扱う。
生成前に呼出元がobjectを保持し、`verify`でlease/epoch/generation・受付遮断・accepted drain・Mod start排他を再照合する。
`native`には既存ModControlTransportのnativeを渡す。固定bundle検証、OwnedProcess、private匿名stdio、Data形式本人識別を再利用する。
C/Wの各groupと実PID/start/host/boot、cwd/executable/command/OS ownerを観測し、私設frameのcontroller/owner/workerとsession/nonceをPが固定する。
任意commandや本人欄を渡すAPIではなく、Cはopen、Wはprepareだけを一度送る。非空の型付きrequestは変更しない。
生成/送受信/応答照合/記録の失敗は例外とし、再送・別helperへの差替をしない。object/handle/pipe/元要求・応答を保持し、本文・秘密は監督記録に残さない。
Registry内部呼出元は第5・第6スライスで接続している。公開writer受付は後続である。Assigned登録は後述の第7スライスで接続する。admit/候補packetは第8スライスで接続する。W/全child終了のP観測は第9スライスで接続する。C completeの限定入口は第10スライスで追加する。completeのP接続/正本記録は第11スライスを参照する。C実終了、closeのP接続、Registry CLOSED/start gateは後続であり、OPEN/PREPAREDを編集成功や終了証拠に使わない。
実Windows/macOS本人受入・実root・実bundle起動は未確認。合成native/pipe fixtureと既存Java非native vectorsの成功を実機受入へ拡張しない。

## Registry正本に束縛するC取得（第5スライス先行部分）

P内部の`RuntimeControl._offline_open`は、同lease/epoch/generationの認証済runが正常保存・M/全child退出を完了し、受付BLOCKED・全writerの耐久close/実proxy退出・未完了要求不在が正本で一致する場合だけCを取得する。
P内部入口でも既存のresource_useでGC/監督停止と調停する。同instance lock内の短txでgate revision、offline-open PENDING、artifact参照、instances.pendingを予約し、DB外で保持Cを起動する。各I/O前に同予約と元run/descriptor/停止proof/drain snapshot・旧M PID不在を再照合する。
Data Readyのworld/run/hash/revisionは期待値としてCへ渡し、実state・seal/chainはData本人APIが検査する。Registryの正常停止だけからData Readyを推測せず、任意rootや空verifyを渡す入口は作らない。
OPEN後も予約/PENDINGを保持する。生成/通信/照合/正本記録の失敗で再取得・別Cへの差替・自動rollbackはしない。元Cと生成資源を保持し、start/lease解放を拒否する。
この入口はP内部用で、Manager/CLIの停止中writer受付は未公開。正本予約からW PREPAREDまでは第6スライスで接続している。Assigned登録は第7スライスで接続する。admit/候補packetは第8スライスで接続する。W/全child終了のP観測は第9スライスを参照する。C completeの限定入口は第10スライスを参照する。completeのP接続/正本記録は第11スライスを参照する。C実終了、closeのP接続、Registry CLOSEDと予約解放は後続である。合成SQLiteと既存Java/native fakeの検証を実OS/実root受入へ広げない。

## 正本予約からW待機（第6スライス先行部分）

P内部の`RuntimeControl._offline_prepare(token, operation_id=..., request=..., deadline=...)`は、同Pが保持したC OPEN原文とRegistryの記録hash・元予約/閉門/drain/停止証拠を再照合する。元offline-open PENDINGのresponseへW操作IDとIPC要求fingerprintを先に記録し、独立OwnedProcessのWを保持してPREPAREDまで進める。型付きEdit本文はそのままDataへ渡し、Dataの商品操作hash算法をPythonへ移さない。

`OfflineProcess.acknowledge_startup`は、呼出元が正本へ記録したOPEN/PREPARED応答hashを照合し、本人再検証とローカル記録後にだけ既存ProxyPipeのbufferを解放する。送受信失敗・応答不一致・記録失敗なら次frameへ進まず、別要求・再生成・再送で回避しない。W応答が返ってもoffline-open PENDINGとartifact参照を保持する。

Assigned登録は後述の第7スライスで接続する。W admitは第8スライスで接続する。WはDB未openで待機し、実終了・C complete/close・Registry CLOSEDを完了扱いにしない。game候補のData退出→Ready供給と公開writer受付は後続で、今回もReady期待値をC本人が検査する。実OS/native受入は未確認。

## 正本PREPAREDからC Assigned（第7スライス先行部分）

P内部の`RuntimeControl._offline_assign(token, operation_id=..., deadline=...)`は、保持Wの記録済みPREPARED原文hash、元CのIdle、管理bindingとRegistry正本予約を照合し、assignment意図を同instance lock内の短transactionへ先行記録する。本文は呼出元から再受領せず、元Wの非空の型付き要求・IPC fingerprint・P観測の本人をCの既存assignへ渡す。Data自身がAssignedとeditRequestSha256を生成し、PythonはDataの正規化/hash算法を再実装しない。

CのASSIGNEDでは同op/IPC hash/session/nonce/runとrevision+1、state hashとData editRequestSha256を照合する。元OPEN/PREPARED原文は維持し、ASSIGNEDの正本記録と本人再照合・ローカル記録後だけC pipeを解放する。失敗時は例外で元要求/応答・C/W・PENDING/参照を保持し、再登録・再送・差替しない。

W admitと候補packetの内部接続は後述の第8スライスを参照する。WはPREPAREDのままDB未openで待機する。W/全child終了のP観測は第9スライスを参照する。game候補のData退出→Ready供給、C実終了/CLOSEDと公開受付は後続。C completeのP接続/正本記録は第11スライスを参照する。実OS/native本人受入は未確認。

## 記録済みAssignedから候補packet（第8スライス先行部分）

P内部の`RuntimeControl._offline_admit(token, operation_id=..., deadline=...)`は、元C/Wと記録済みASSIGNED・ACKを再照合し、同instance lock内の短transactionでadmission意図を先行記録する。元worker/assignment記録を維持し、WへはC本人のAssigned hash/revision/run・Data editRequestSha256と元PREPARED hashだけを送る。本文を再受領せず、Java Wが保持する元HostAdapterRequestをData119へ渡す。

WはDBclose・候補公開後にACKを待たず終了する。送信前は元Wの本人も照合し、候補受信中は元専用pipe/OwnedProcess handleへのbindingとP/C・Registryを検証する。poll0なら旧W PIDを再解決せず受信でき、PID再利用で別processを採用しない。非zero exitは拒否する。poll0だけで全child終了やC completeを確定しない。

CANDIDATEは同op/IPC hash/session/nonce/world、run=操作UUID、Assigned revision+2、Data editRequestSha256とstate/receipt hash形式を照合し、正本へ応答hashを記録する。候補後はWへACK/次frameを送らずbufferとhandleを保持する。PENDING/参照を解放せず、packetだけで最終SUCCESSへ昇格しない。失敗時の再admit・再生成・差替・自動rollbackはない。

W/全child終了のP観測は後述の第9スライスを参照する。候補実体/hashのC本人照合・receipt completeの限定入口は第10スライスを参照する。Pからのcomplete呼出し/正本記録は第11スライスを参照する。game候補のData退出→Ready供給、C close/Registry CLOSEDと公開受付は後続。Ready期待値を自動確定せず、実OS/native本人受入は未確認。

## 記録済み候補からW/全child終了観測（第9スライス先行部分）

P内部の`RuntimeControl._offline_observe_worker_exit(token, operation_id=..., deadline=...)`は、元Assigned・候補packetのローカル/Registry記録と元予約を再照合し、終了観測意図を同instance lock内の短transactionへ先行記録する。元Wの保持OwnedProcess handleの正常exit0と独立Job/groupの全child不在を観測した後だけ、ローカル証拠とRegistryへの応答hashを記録する。OS観測と最後の本人・正本照合の完了後、ローカル成功証拠を書き始める前に同じdeadlineを再検査し、Registry側でも最終照合後・結果更新前に検査する。待機はtransaction外で行い、その間も元P/Cの生存・bindingと正本を再照合する。旧W PIDのidentity取得や別processへのattachは行わない。

非zero/不明終了、残child、期限切れ、正本/本人不一致、記録失敗・応答喪失は例外で元証拠・C/W・PENDING/pending/参照を保全し、自動kill・再生成・再観測/replayを行わない。Wの候補pipeは解放せず、Cへ追加frameも送らない。戻り値`WORKER_EXITED`はPによる内部の途中観測結果だけであり、候補実体/receiptのC本人照合・complete、編集SUCCESS、Data Ready、Registry CLOSEDを表さない。

C本人による同候補/receipt実体照合・completeの限定入口は後述の第10スライスを参照する。Pの終了証拠からの実接続・対応応答の正本記録は第11スライスを参照する。同接続連続操作、game候補のData退出→Ready供給、C終了とRegistry CLOSEDは後続。Ready期待値を自動確定しない。実OS/nativeの本人/Job/group/child終了受入は未確認。

## C本人の候補確定入口（第10スライス先行部分）

Pが保持する同じCのprivate stdioへ`complete`を送る入口を追加した。呼出前にPは第9スライスの元W/全child正常終了証拠と正本候補、元予約/閉門/drain/本人を検証する必要がある。このP側呼出しとRegistry結果記録は第11スライスで内部接続した。製品受付からは呼ばない。CのData側W死亡検査はPの全child観測を代用しない。

frameは`command=complete, operationId, requestHash, sessionId, nonce, openOperationId, expectedStateSha256, expectedRevision, worldUuid, runUuid, receiptSha256, editRequestSha256`のみ。op/IPC hash/Data edit hashは元Assigned、session/nonce/open IDは元接続と一致させ、world/run/revisionとstate/receipt hashは同候補packetの値を渡す。runは操作UUID、revisionは元Assigned+2。本文・root・worker差替・終了済み自己申告flagは受け付けない。

Cは保持Assignedと現在SavedPendingExitの元Wを照合する。候補値を現在値へ置換せず、そのまま`WorldOfflineWriterSession.complete`へ渡す。元writer・C本人・W死亡・DBclose・候補/receipt実体・元sealの再照合とIdle確定はData121の責務で、helperは保存/hash/recover算法を持たない。Dataが返すHostAdapterResponseをcodecのまま`result`へ載せ、`COMPLETED`と元candidateStateSha256/receiptSha256、更新後IdleのstateSha256/stateRevision/runUuidを返す。Idleではwriter session/runへ復帰し、Readyは作らない。

取得後の不正complete、作用後失敗、応答喪失、EOFはUNKNOWN/非正常終了で保全し、再complete・自動close/recover・C差替をしない。COMPLETEDはC本人のData確定までを表し、Pの対応応答照合・正本記録と元W/全child終了証拠が揃うまでManagerへ最終結果を返さない。同pipeのCOMPLETED記録後解放は第11スライスを参照する。連続操作、game退出→Ready供給、C実終了/Registry CLOSEDは後続。実OS/native・実root・実helper bundle起動は未確認。

## 終了証拠からC completeのP接続（第11スライス先行部分）

P内部の`RuntimeControl._offline_complete(token, operation_id=..., deadline=...)`は、保持した元C/Wと記録済み候補・W/全child正常終了証拠を使う。元終了証拠のhash、同候補への参照、元Assignedの完全W本人、生成PID/独立groupと正本を再照合し、completion意図を短transactionへ先行記録する。旧W PIDを再解決・再観測せず、候補・本文を呼出元から再受領しない。

同Cのprivate pipeへ元candidate値と元AssignedのIPC/Data hash・session/nonce/open IDを送る。COMPLETEDでは厳密field集合、同op/hash/session/nonce、元candidate/receipt、候補revision+1、元OPENのwriter run復帰とstate hash形式を照合する。Dataの`result`は元requestIdと確定status（SUCCESS又はREJECTED）を照合して本文を変更せず返す。DataのREJECTEDをSUCCESSへ変換せず、UNKNOWNを確定しない。候補/receipt実体とIdleへの確定はC本人のData APIが検査する。

I/O中と局所記録前にP/C本人・元予約/閉門/drain・completion意図を照合し、同じdeadlineを検査する。Registry最終transactionでも元証拠/意図を再照合した後・結果更新前に期限を検査する。対応応答hashを正本へ記録してからだけ`OfflineProcess.acknowledge_completion`がローカル記録後に同C pipeを解放する。Registryにはhash/参照だけを残し、要求・応答本文やsession/nonceを残さない。Wの候補bufferと保持C/W、offline-open PENDING・pending・artifact参照は維持する。

部分送信・候補不一致・応答喪失・記録/ack失敗・期限切れは元意図と証拠を保全し、自動complete再送・再生成・kill・close/recoverを行わない。Cの成功応答だけではManager公開結果・Ready・Registry CLOSEDへ進めない。今回の戻り値はP内部の照合/記録済みData結果である。次W待機は第12スライスを参照する。同接続連続編集は第13スライスを参照する。公開受付・game退出→Ready、C close/実終了/CLOSEDは後続。Ready期待値を自動確定しない。実Registry競合・合成OS/IPCと固定Java vectorsの検証を、実OS/native/実root/receipt実体受入へ拡張しない。

## 記録済みIdleから次W待機（第12スライス先行部分）

同じ`RuntimeControl._offline_prepare`は、前W/全child正常終了と同候補のC completeについて、元局所/Registryの応答hashとACKまで一致した場合、次の独立WをPREPAREDへ進める。最新Idleのhash/revision/runは照合・記録済みCOMPLETED原文を使い、初回OPENへ戻さない。元C/session/nonceとlease/epoch/generation/root/worldは維持し、Cの再open・起動ACK再送をしない。型付き非空要求は変更せず次Wへ渡す。PREPAREDはDB未openの待機である。

短transaction内で旧worker/assignment/admission/workerExit/completionのhash参照を`history`へ移し、次worker意図を記録する。Pは旧Wのhandle/pipe/要求・応答をメモリ履歴へ保持し、再解決・終了再観測・kill・解放しない。Registryの履歴には本文・session/nonceを保存しない。I/O前と最終結果transactionで元予約・閉門/drain・次worker意図・正本/メモリ履歴を再照合する。次WのPREPAREDを正本記録してからだけ次W pipeを解放する。PENDING/pending/参照は維持する。

元操作ID・過去history・既存operation表とのID再利用、未完了W、未記録/未ACK complete、旧PREPARED応答、履歴/本人不一致、期限切れを拒否する。部分生成/送信、応答喪失、局所/正本記録/ACK失敗後も旧証拠と新意図・元C/旧W/新W資源を保全し、再送・再生成・自動close/recoverをしない。次W生成前と最終Registry照合後/更新前にも同deadlineを確認する。

この節は次W待機までを扱う。Cの操作別Assigned/complete保持と連続編集は第13スライスを参照する。旧操作や旧応答で次段階へ進めない。game退出→Data Ready供給、C close/実終了/Registry CLOSED/start gate・公開受付は後続で、Ready期待値を自動確定しない。合成OS/IPC・実Registryと固定Java vectorsの検証を実OS/native/実root受入へ拡張しない。

## 第13スライス: 同Cの操作別保持と連続編集

次W生成意図を正本へ保存した後、元CのAssigned/complete原文・応答・ACKをoperationId別に保持し、次操作枠を作る。Pの旧W/操作履歴とC履歴の集合、正本hash、局所hash/ACKを再照合し、履歴末尾のCOMPLETED Idleから次Assignedへ進む。同C/session/nonce/pipeを維持し、各Wは独立groupでPREPARED→Assigned→admit→候補→全child正常終了→C completeと進む。各対応応答の正本記録とACKの後だけC pipeを次frameへ進める。旧ID/旧応答は採用しない。

失敗後は元操作履歴・新意図・C/Wのhandle/pipe・PENDING/参照を保全し、再送/自動差替/終了再観測をしない。製品公開入口への組込み、game本人のData退出→明示期待値によるReady供給、C close/Registry CLOSED/start gateは後続。Ready期待値を自動確定しない。実OS/native/実root受入は未確認。

assign/admitも操作全体を同じdeadlineの対象とし、応答受領後の本人/正本照合完了時、Registry最終照合後かつ成功結果更新前、assignのACK前に期限を検査する。期限超過時はSTOP_TIMEOUTとし、元要求・応答・意図・作用後のstateを保全する。自動再送・kill・rollbackを行わない。ProxyPipeの完成bufferを先に返す契約は維持し、操作側で最終期限を確認する。

## game候補のData退出入口（第14スライス先行部分）

新Cの最初のprivate frame `observe-game-exit` は単発です。`open`と同じ起動bindingの各fieldに、`gameOwner`（元MのhostHash/bootId/pid/startId）、`saveProofSha256`、`requestedStateSha256`を加えます。`expectedStateSha256`/`expectedRevision`/`runUuid`はReadyではなく元SavedPendingExit候補の明示期待値です。CはP/C本人と実parent、候補world/run/generation/revision/hash/元Requested hash、元M本人、元management全fieldとsaveproof hashを照合してから、固定Data `observeExit(expected)`を一度だけ呼びます。

M本人のfinishSavesによる保存候補の公開は固定Mod597の責務です。Dataが元chain/seal・全DBclose・元Mと必要writer本人の死亡を照合し、Readyを公開します。CはDBをopenせず、保存/hash/recover算法を実装しません。PによるM正常終了/全child/全writerの列挙・実観測は呼出元の必須前提です。Dataの本人死亡検証はPの全child観測を代用しません。

応答`GAME_EXITED`はoperationId/requestHash/sessionId/nonce、ReadyのstateSha256/stateRevision/worldUuid/runUuid、元candidateStateSha256/saveProofSha256を返して終了します。同じCで次frameを読みません。作用前拒否はREJECTEDと非正常終了、Data呼出開始後の不明はUNKNOWNと非正常終了です。応答喪失でも元候補/Readyを保持し、再観測・再送・writer取得・kill・rollbackへ進みません。

P内部の単発接続部品は第15スライス節を参照してください。認証済保存原文を既存stop結果へ引き継ぐ前段は第16スライス節を参照してください。観測Cと既存Pの停止/受付/Registry経路への実接続は後続です。返却Readyは観測結果であり、次openの期待値の自動承認ではありません。次openは引き続き独立して明示したReady期待値を要求し、候補からhash/revisionを推測しません。単発接続のdeadlineは第15スライスで検査し、Registry最終結果更新までの接続は後続です。実OS/native/実root受入は未確認です。

## game退出観測CのP内部部品（第15スライス先行部分）

未使用の`OfflineProcess.observe_game_exit(frame, game_process=..., game_identity=..., game_group=..., deadline=...)`は、元Mの生成時native identityと保持handle/groupから正常exit・全child不在を確認し、固定Cの第14スライス入口へ同候補bindingを一度だけ送ります。呼出元はこのobjectと元Mを作用前から保持し、constructorの必須`verify` callbackでlease/epoch/generation・正本予約・BLOCKED/accepted drain・全writer終了・認証済Requested/COMPLETIONと候補原本の対応を継続照合する必要があります。callbackだけを用意したこの部品を、製品停止経路への組込み済みとは扱いません。

`frame`は元SavedPendingExitの明示hash/revision/world/run、元M全identity、saveproof/Requested hash、元managementとrootを含み、C/P本人欄とprivate session/nonceだけを生成時に補います。GAME_EXITEDの厳密field集合・要求binding・元候補/proof参照・同world/runと候補revision+1・Ready hash形式を照合します。Ready hashは実観測C本人を含むData退出証拠に依存するため、起動前の期待hashを要求しません。返却Readyは局所観測値であり、次openの期待値として自動採用しません。次openには観測後の独立した明示Ready期待値を別途供給する必要があります。Data保存/hash/退出算法はC本人APIへ委譲し、PはDB/119操作をしません。

応答送信直後のC正常exitを受信中も許容し、Cと全childの終了を保持handle/groupで確認します。生存照会の結果は生成snapshotへ照合します。照会中に本人が不在になった場合だけ、同じ保持handleを再pollし、正常exit0と固定bindingを確認できれば終了済み経路へ移ります。取得できた本人情報の不一致、非zero・不明終了は拒否します。終了確認後の旧PID再照会・別process採用・再attachは行いません。既存writer用verifyの生存必須契約は変更しません。観測Cは送信時点の独立した要求とnative生成snapshotへ固定し、PID/start/host/bootを応答後・局所記録完了後も照合します。操作全体の同deadlineをOS観測後、対応応答照合後、局所証拠書込み前後まで検査します。失敗/期限超過時は元要求・応答・生成記録と作用後のReadyを保全し、再入口・既存startへの切替・再送・kill・rollbackをしません。書込み後に期限を跨いだ場合は局所観測証拠を保全しますが、受理済み結果を返しません。

成功時もpipe buffer/handleを保持し、`game_exit_result`と戻り値は局所観測結果です。Registry確定・writer CLOSED・次openの許可を表しません。製品呼出元の正本予約/台帳/元Mhandle供給、最終transactionでの再照合・期限検査・結果記録、明示Ready供給を後続で接続します。第15スライスでは稼働RuntimeControl/ManagedRuntime/Registryを変更していません。認証済保存原文を停止結果へ引き継ぐ第16スライスの前段と、観測C自体の製品接続は区別します。実OS/native/実root受入は未確認です。


## 認証済保存原文とstop正本の接続（第16スライス先行部分）

既存ManagedRuntimeは認証済SAVE_REQUESTED/COMPLETIONの原frameを元sessionへ保持します。COMPLETIONの送信とpeer/endpoint close後、同じtransaction内で元descriptor・lease/epoch/generation/run、RunningからのRequested、原文readback、最新BLOCKED revision・全writer台帳、元stop PENDINGと参照予約を照合してから、そのstop意図へhash参照を記録します。close不明・応答喪失では再送せず、原文と元予約を保全します。

`ManagedRuntime.save_evidence(connection, instance, session, deadline)`は呼出元のtransactionでこの対応を再照合し、hash参照と元stop予約本文を返す内部APIです。`managedSave`はSAVE_REQUESTEDの`operationId`、要求/応答bodyの`requestBodySha256`/`responseBodySha256`、`descriptorSha256`です。本文は既存mod-control台帳を参照し、追加保存しません。公共stopと直接stopの最終結果transactionでも同一参照を再照合し、結果へ引き継ぎます。

元STOP_BLOCKのdeadlineをSAVE_REQUESTEDの受信・close・直接stopの結果確定まで引き継ぎ、phaseごとに再起算しません。公共stopは通常待機・直接stopへの合流予約と待機にも、呼出元の固定期限と元STOP_BLOCKのいずれか早い期限を渡します。記録前・照合直後・commit後・返却前まで同deadlineを検査します。確定前の失敗は元参照と原文を保全し、公共stopのRECOVERY_REQUIRED記録にも保存原文参照を残します。commit後の期限超過では確定記録を巻き戻さず、正常応答を返しません。 同じ確定済みstopから旧control slotを終端するため、timeout後の同要求readbackと次startに旧slotを残しません。合流は元stopの確定証拠を変更せず、自分の予約前・最終照合後・commit後・返却前を検査します。新規合流の初回応答と、同一の確定済み要求のreadbackを区別します。元要求のreadbackは追加作用をしません。Pの原要求/応答に対する自動再送・kill・rollbackは追加しません。

この参照は認証済台帳の由来を表すもので、元M/全childの実終了、Data候補・saveproof実体、観測C終了、Ready、writer CLOSEDの証明ではありません。元Mhandle保持部品は第17スライスを参照します。候補原本取得・観測C実接続・最終退出証拠のRegistry記録・独立明示Ready供給は後続です。現在のstop結果や局所Readyから、次openの期待値を自動確定しません。実OS/native・実root受入は未確認です。


## 認証済Mの生成handle保持（第17スライス先行部分）

認証済handshake後、Pは元MのOwnedProcessと生成時のrun/instance/generation/artifact/nonce/native identity/creation、Data owner、descriptor hashを固定します。macOSのboot照合は生成identityと同じUUID表現へ正規化し、Data ownerと生成snapshotの原値は保持します。停止時は同じ保持objectと生成snapshotを照合し、既存の終了観測・Registry停止記録を続けます。認証済Mは停止後もhandleをcloseせず、稼働process mapからだけ退避します。通常control slotの終端と次startは維持し、元Mと次Mをrun別に保持します。従来の非認証通信fixture境界とGameTestのcloseは変更しません。

`RuntimeControl._game_process(run)`は保持した元handleと生成snapshotのcopyを返す内部取得です。PIDからの再attach・別processの採用・native本人再照会を行いません。この取得だけで停止・保存・全writer・候補・Registry退出・Ready/CLOSEDを証明しません。原要求hash参照/候補原本から観測Cへの単発供給は第20節、観測済みslotを停止結果・最終Registry transaction/同deadlineへ結束する処理は第21節を参照してください。既存stopからの自動観測開始・独立明示Ready供給・元handle正常解放は後続です。

保持資源はPのメモリ内にあり、P再起動後に復元しません。正常停止・失敗とも自動解放せず、正常解放は後続のRegistry退出確定に接続します。その接続が完了するまではrunごとのhandleがPの寿命中に残ります。実OSでのhandle資源消費・本人受入は未確認です。Ready期待値を自動承認する変更はありません。


## 保存原文と保持元Mの結束（第18スライス先行部分）

`ManagedRuntime.save_evidence`は、認証済Requested/COMPLETION・元stop予約・閉門/全writerの原文照合に続き、同じRegistry transactionで元run記録を読みます。run/instance/generation/artifactと、保持した元Mの生成identity/creation/handleを照合し、生成時のData ownerとdescriptor hashを保存原文へ結束します。Data ownerの原表記を維持し、native identityのUUID表現へ書き換えません。

初回の保存原文参照の記録前と、公共stop/直接stopの最終transactionで同じ照合を行い、その完了後に元deadlineを検査します。不一致・期限超過では原文・PENDING/参照・保持資源を保全し、追加IPC/生成・自動再送・再attach・killへ進みません。既存stopの返却形式とcontrol slot終端は維持します。

この照合は元Mの実終了、候補/退出証拠、Ready/CLOSEDを表しません。候補原本・全writer・正本予約を供給するP内部の観測Cへの単発接続は[第20節「同stopから観測Cへの単発接続」](#同stopから観測cへの単発接続第20スライス先行部分)で実装済みです。観測済みslotを停止結果・最終Registry確定へ結束する処理は第21節を参照してください。既存stopからの内部明示候補による観測開始は第22節を参照してください。明示Readyの次open供給と元handle正常解放は後続です。実OS/native/実root受入は未確認です。

## 不変game候補原本の入力結束（第19スライス先行部分）

P内部の`ManagedRuntime.game_exit_inputs(instance, session, candidate, deadline)`は、元stopのPENDING/参照を事前と最終の短いtransactionで再照合し、終端した認証済保存原文と記録済みmanagedSave・保持元Mを照合します。candidateにはworldUuid/runUuid/stateSha256/stateRevisionの期待値を明示します。currentから現在値を読み取って期待値を自動更新しません。

原本I/Oは双方のtransactionとinstance lockの外で行い、競合するstartのPENDING拒否を妨げません。元descriptorのworld配下`sushieric/run-state/states/<期待hash>.json`と候補内の参照に対応する`saveproofs/<hash>.json`だけを読みます。既存codecでSavedPendingExitと元Requestedのworld/run/generation/paths/previous-state/revision/owner/management、saveproofのsource/owner/認証済COMPLETIONを相関照合し、原bytesを再serializeせず返します。raw fileのSHA-256には既存hash関数を使い、Dataの正規化・保存・chain/seal・recover・退出算法を実装しません。current/不変chain/実DBclose/本人死亡の最終検査はC本人のData APIへ委譲します。

戻り値は観測C向けの管理/candidate欄、候補原bytes、saveproof原bytesのtupleです。operationId/requestHash、P/C本人、private session/nonceは含めず、後続の正本予約と起動時に補います。原本I/O後に元保存原文/正本予約/保持Mを再照合し、最終境界で同deadlineを検査します。不一致・原本欠落/変更・期限超過は例外で保全し、別候補/currentへのfallback、DB更新、IPC、生成、終了観測、kill、handle解放を行いません。

この読取り入口を用いるP内部の観測Cへの単発接続は[第20節「同stopから観測Cへの単発接続」](#同stopから観測cへの単発接続第20スライス先行部分)で実装済みです。観測済みslotを停止結果・最終Registry確定へ結束する処理は第21節を参照してください。既存stopからの内部明示候補による観測開始は第22節を参照してください。独立明示Readyの次open供給と元handle正常解放は後続です。戻り値をReadyやCLOSEDへ自動昇格しません。実OS/native本人受入・実root/Mod/実bundleビルドは未確認です。

## 同stopから観測Cへの単発接続（第20スライス先行部分）

P内部の`ManagedRuntime.observe_game_exit(instance, session, candidate, deadline)`は、保持sessionに作用前から単発slotを確保し、第19スライスの原bytes/要求欄と同stop ID/fingerprint/control ID・保持元M/生成snapshotを固定します。固定generation/runtimeのartifactとData世代を既存検証へ通し、独立OwnedProcessの未使用Cをslotへ保持してから、既存`OfflineProcess.observe_game_exit`へ一度だけ渡します。operationIdは元stop ID、requestHashはcommand/stopFingerprint/inputsから既存canonical/digestで固定します。P/C本人とprivate session/nonceは生成部品が補います。

callbackは元session/slot、stop識別、同原本/要求欄、保持M object/生成snapshotとslot記録の一致を継続照合します。第19スライスの原文照合を通し、BLOCKED/drain/全writerと元PENDING/参照を維持します。原本I/OとOS/IPCは短いtransactionの外です。元Mの正常exit/全child不在→C本人Data退出→C正常exit/全child不在は既存単発部品へ委ね、旧PIDへ再attachしません。

同deadlineをcallbackとC完了後、局所結果保持の前後まで検査します。失敗後もslot/原文/元M/観測Cを残し、別候補への変更・再生成・再送・再観測・kill・rollbackをしません。局所結果保持後に期限を跨いだ場合は証拠を保持しますが、成功として返しません。

返却`GAME_EXIT_OBSERVED`とslotのresultは局所観測です。観測済みslotを既存公共/直接stopの結果と最終Registry transactionへ結束する処理は第21節を参照してください。既存stopからの内部明示候補による観測開始は第22節を参照してください。独立明示Readyの次open供給、元M/Cの正常handle解放は後続です。呼出元Pが元stop予約とsessionを保持する前提で、解除/差替はcallbackで拒否します。Ready/CLOSEDへ自動昇格しません。実OS/native本人/実Job/group/実IPC結合と保持資源量は未確認です。


## 観測済みgame退出証拠とstop確定の結束（第21スライス先行部分）

第20節のslotがある場合、公共/直接stopは元候補原本をtransaction外で再照合し、`ManagedRuntime.game_exit_evidence`で元M/Cの保持object・生成record・要求/応答/結果原本を照合します。開始済みで結果不明のslotを既存proofだけで正常確定しません。未開始の既存stopは従来の形式・経路を維持します。観測Cを自動起動するP内部の候補入力は第22節を参照してください。公開RPCは追加していません。

最終transactionでは既存`save_evidence`による元stop/認証済原文・閉門/drain/全writer照合の後、同じgame退出証拠を再照合します。ここではOS照会・file読取り・追加IPCを行いません。同deadlineを最終照合後・結果更新前と、確定後/返却前にも検査します。結果JSONの任意欄`gameExit`は明示候補4欄と要求/応答/観測/観測C recordのSHA-256だけです。要求本文・本人の原UUID・局所Ready本文を監督DBへ追加せず、Ready/CLOSEDへ自動昇格しません。テーブル変更はありません。

確定前の不一致・期限超過では元予約/原文/slot/M/Cを保持し、追加Data作用・再観測・再送・killを行いません。確定後timeoutではCOMPLETEDのgameExit証拠を保持し、既存のsession解放を実施します。直接stopの受信handlerは従来どおりRECOVERY_REQUIREDを保全し、公共stopの確定済み結果を巻き戻す処理は加えません。active sessionの解放前に、観測Cのslot（元OwnedProcess・固定原本/生成snapshotを含む）とdescriptor/startup/stop/Requested/COMPLETION原文をRuntimeControl.game_exitsへ元run/stopをkeyにして移します。既存M保持と同じRuntimeControl/lockを使い、_game_exitで同じC objectを取得できます。active mapは残さず、次startのcontrol slotを塞ぎません。元M/C handleの正常解放はまだ接続していません。

P内部の明示候補供給と観測開始の順序付けは第22節を参照してください。公開供給元、独立明示Readyの次open供給、正常handle解放は後続です。呼出元Pは観測からstop結果確定まで元session/予約を保持します。確定後は終端保持mapが観測Cと固定原文/生成snapshotを所有し、呼出元のsession参照に依存しません。実OS/native・Job/group・private IPC結合と保持資源量は未確認です。

## 第22スライス先行部分: P内部の同stop専用候補入力

案1を採用し、P内部だけに候補入力契約を追加した。公開execute payload・Mod wire・固定依存Modは変更しない。案2の保存後wire通知は本repoのみ・固定依存成果の範囲を維持するため採用していない。候補の実供給元（Manager/CLI公開受付）は後続であり、現時点では未定。

信頼済みP呼出元は停止予約前のRUNNINGに対して `managed.select_game_exit(token, run_id=元run, operation_id=予定stop, descriptor_sha256=元descriptor原hash, deadline=固定期限)` を一度呼ぶ。その後は既存 `runtime.execute("stop", payload, operation_id=予定stop)` を使う。選択しない停止の入力・挙動は従来どおり。

保存原文が確定した後、同じPから `managed.supply_game_exit(token, run_id=元run, operation_id=予定stop, descriptor_sha256=元descriptor原hash, candidate=明示4欄, deadline=同期限)` を一度供給する。4欄はworldUuid/runUuid/stateSha256/stateRevision。候補は呼出元が独立に指定し、callback・任意path・current自動採用を受け付けない。供給tokenと元run/stop/descriptor・正本予約を最初の短いtransactionで認証し、成功後に一度限りの受付を確保する。未認証・元予約不一致は受付/event/原本/予約/元handleを変えず拒否する。原本I/O後にも認証と元予約を再照合し、Registryへ入力hashだけを記録する。Pが保持する入力原文に供給・選択・元stopの期限のminを固定する。

候補受付を選択したstopだけ保存原文確定後に同deadline内で入力を待つ。元run/stop/descriptorと正本参照を再照合して観測Cを一度開始し、第21スライスの最終transactionへ進める。受付確保後の候補欠落・期限超過・不一致は原本/予約/M/Cを保持して非正常終了し、既存proofへfallback・再供給・自動再観測をしない。認証前の拒否は受付を消費しないので、正規入力を引き続き一度だけ受けられる。通知と成功確定をcontrol lock内で行い、停止側は通知後に確定状態と供給期限を再読込する。短い供給期限をC観測/callback/最終transactionまで引き継ぎ、通知後timeoutの入力からCを開始しない。予定stopと異なる直接停止への選択流用も拒否する。候補待機/原本照合/観測/結果確定は元stopと選択入力の短い方の期限に従う。

観測に使った入力原文もactive解放前に元run/stopの終端保持先へ移管する。局所結果をReady/CLOSEDへ自動昇格しない。独立明示Readyの次open供給と元M/Cの正常handle解放は後続。実OS/native受入は未実施。

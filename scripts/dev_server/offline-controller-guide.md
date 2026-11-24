# 停止中controller Cの固定入口

.github #64の第1・第2スライス（Cのowner/Assigned）、第3スライス先行部分（Wのprivate入口）、第4スライス先行部分（P内部起動部品）、第5スライス先行部分（Registry正本予約とC取得）、第6スライス先行部分（正本予約からW待機）、第7スライス先行部分（正本PREPAREDからC Assigned）、第8スライス先行部分（Assignedから候補packet）、第9スライス先行部分（W/全child終了観測）、第10スライス先行部分（C本人complete入口）、第11スライス先行部分（P終了証拠からC確定/正本記録）、第12スライス先行部分（記録済みIdleから次W待機）、第13スライス先行部分（同Cの操作別保持と連続編集）、第14スライス先行部分（game候補のData退出入口）、第15スライス先行部分（P内部の単発観測C接続）、第16スライス先行部分（認証済保存原文とstop正本の接続）、第17スライス先行部分（認証済Mの生成handle保持）、第18スライス先行部分（保存原文と保持元Mの結束）、第19スライス先行部分（不変候補原本の入力結束）、第20スライス先行部分（同stopから観測Cへの単発接続）、第21スライス先行部分（観測済みgame退出証拠とstop確定の結束）、第22スライス先行部分（P内部の同stop専用候補入力と観測開始）、第23スライス先行部分（確定game退出と独立Readyから次C取得）、第24スライス先行部分（確定stopに束縛した元M/C単発handle解放）、第25スライス先行部分（公共stopの自己予約確定から正常解放）、第26スライス先行部分（直接stopと公共合流の待機予約終端）、第27スライス（直接stopの正常解放と合流応答）、第28スライス先行部分（最新IdleからC本人closeの要求・応答記録）を扱います。停止中writerの公開受付は有効化していません。現在のwriter-ioはRunning限定です。

## 責務と固定配布

`offline_controller_bundle.offline_controller_command(bundle)`は完成bundleの全hash、同版DataModel Host配布、source・compiler・専用JAR・作成OSを照合し、固定JDK21と`OfflineControllerMain`の起動配列だけを返します。公開Host/CLIの管理root拒否は維持します。ローカルJDK、任意classpath、実行時のsource探索へ切り替えません。

新しいsource-lockでbundleを生成すると、`adapter/offline-controller.json`と`adapter/offline-controller/`を収録します。旧bundleの検証は維持しますが、helperのない旧bundleからこの入口を呼べません。既存bundleへ後付けせず、新bundleを生成してください。DB・run-stateの保存形式変更はありません。

CはData121 `observeExit`／`openManagedOfflineWriter`／writer `assignWorker`／writer `complete`／writer `close`を限定して呼びます。game退出の単発入口は第14スライス節、completeは第10スライス節を参照してください。ゲームのbeginRun/finishSaves、119 Editの実行、snapshot操作は受け付けません。元sealと不変chain・Ready・DBcloseの照合はData121へ委ねます。

## 現在Readyの公式読取（第37スライス先行部分）

`offline_ready_command(bundle)`は同じ固定C JARの`OfflineControllerMain$Ready`だけを選びます。既存bundleにこのclassが無ければ拒否し、owner取得mainへ代替しません。新しいsource-lockによるbundle生成が必要です。このスライスではPの`OfflineProcess`・start受付への呼出しをまだ接続していません。

私設frameは上記と同じ上限・形式で、`command=verify-ready, operationId, requestHash, sessionId, nonce, controller, owner, generation, worldRoot, shopDirectory, worldUuid, runUuid, expectedStateSha256, expectedRevision`だけを受け付けます。UUID・hash・識別子の形式はopenと同じ、revisionは非負の64-bit整数です。`controller`は実parent P、`owner`はこの読取helper本人を指します。Registry lease/drainをこの読取から推測しません。

固定Dataの`WorldRunLifecycle.readStatus()`でcurrent・不変state・chain/receiptを公式検証し、独立入力のworld/generation/run/hash/revisionとReady型を照合します。Data owner取得・observeExit・Edit・closeは呼びません。P/native本人を読取前後に照合し、`READY_VERIFIED`と`operationId, requestHash, sessionId, nonce, worldUuid, generation, runUuid, stateSha256, stateRevision`を一回だけ返して終了します。不正入力・公式検証失敗・本人不一致は`REJECTED`と非正常終了です。応答不明時の再送・再起動をこの入口は行いません。

結果は読取時点の一致であり、start許可・Registry CLOSED・資源解放を表しません。後続のP側接続で元終端/予約不在・原文・全binding、同一要求と応答原文、読取helperの正常exit/全child不在と正常解放を継続照合する必要があります。入口で固定した単一期限はPの受付・予約・adapter生成準備後・GRANT送信・BOUND/ACK・最終記録/返却まで使用します。GRANT後のMod内部作用は既存Mod期限に委ね、作用後不明は元原本・予約・識別を保全します（2026-11-13の確定判断）。このスライスでは固定Data/Mod契約を変更していません。

## P所有の単発Ready読取（第38スライス先行部分）

`OfflineProcess.verify_ready(frame, deadline)`は未使用objectで一度だけ呼びます。Pは呼出前にobjectを保持し、constructorの`verify`で独立Ready/現在原文、元CLOSED・終端・予約不在を継続照合します。frameは`operationId, requestHash, generation, worldRoot, shopDirectory, worldUuid, runUuid, expectedStateSha256, expectedRevision`だけです。本人/session/nonceは既存生成部品が固定します。

第39スライスの内部入口`RuntimeControl._offline_verify_ready(token, *, operation_id, open_operation_id, ready, deadline)`が、この照合を既存`read_ready_original`と終端専用照合へ接続します。`operation_id`は新しいUUID、`ready`は独立明示の四項目です。呼出時の同じ期限で固定generationの原文と元C/W解放・CLOSED・終端・元予約不在を再照合し、Pの`readers`へoperation別に元reader・入力・現在原文・結果を保持します。同ID再実行と未完了読取がある元終端での別ID再生成を拒否します。読取中はRegistry transactionを保持せず、各作用前後の短い照合だけに使います。

戻り値`OFFLINE_READY_VERIFIED`は元終端証拠・`stateJson`・独立Ready・helper読取結果を結びます。新しいRegistry予約やstart承認は記録せず、既存startの保守的遮断を維持します。この内部読取枠はPのメモリ上の保持で、公開入力の同ID再提示やP再起動後の復旧契約を追加するものではありません。新start予約・GRANT/BOUND/ACKと公開呼出元は後続です。

入口の同じdeadlineを生成準備後の`before_create`、私設送受信、exit0/全child不在、正常handle/pipe解放、最終記録/返却まで使います。固定bundleの実読取bytesへmanifest上限/hashを適用し、file/dir実体識別も各境界で照合します。Windowsではprocessとjobの各handle解放前にも確認します。解放開始後は旧PID/handleを再照合せず、確定終了と保持bindingを使います。

正常結果`READY_READ_RELEASED`は同じ操作の要求/応答hash、artifactId、読取本人/group、正常exit/全child不在/解放と観測Readyを返します。start許可やRegistry更新は行いません。要求/応答・元objectとUNKNOWN/RELEASED進捗を保持し、失敗後の再生成・再送・部分解放retryは拒否します。timeout後にも残る正常記録は、受付成功や再利用可能な承認を意味しません。

この照合は既存adapterの生成直前境界までです。実OSの実行ファイル/遅延classpath openとの原子的固定を追加しておらず、実OS/native本人受入は未確認です。Data/Mod契約・artifactは変更しません。GRANT後Mod内部作用を既存期限へ委ねる保証範囲も維持します。

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

## 新startのrun・閉門正本（第43スライス先行部分）

P内部の`_offline_record_start`は第41の同じ固定結果/callbackとstart/ports予約を使い、新runとCLOSED/revision 0のadmissionを既存`_record`の一つのwriteへ記録します。旧`_start`を呼ばず、run/nonceの再採番やportsの再取得を行いません。元callbackは同じconnection内の純比較にも使い、ファイル/本人確認はwriteの外で行います。正常renewは現在expiresで確認し、同じ入口deadlineを延長しません。

入口全体を既存`resource_use`と`_lock_wait`で囲み、予約から戻った後の原文確認・新run記録・最終返却までGC排他と同じlock待機期限を維持します。

新run原文・admission・元予約結果は一度固定し、保持objectと最終gateで照合するcallbackを結果と組で返します。記録前拒否では新run/admissionをrollbackし、記録後不明では元slot/読取資源/原文・予約を保持して再生成しません。この結果はM生成やRUNNINGの成功ではありません。descriptor/`_create`・生成後資源の確定binding・GRANT/ACKの接続は後続です。Pの入口期限保証はP側に限定し、GRANT後のMod内部作用は既存期限へ委ねる確定判断を維持します。

第44スライスでは、最終read退出後の純メモリ照合へ元予約callbackの完成条件全体を合成しました。元helperの資源・原文/hash・完成進捗・artifact/期限・承認object/値も同じ固定基準で比較し、退出後に変化した場合はreturnedを立てません。この純比較でファイル再読・native再観測・解放済handleの再pollは行いません。descriptor/M接続は、native準備境界・descriptor正本遷移と準備済み資源の引継ぎを含む後続単位です。

## 通常M生成入口の共通化（第42スライス先行部分）

通常`_start`のdescriptor/launch準備・生成時記録・`OwnedProcess.start`を、内部の`_create` context managerへ切り出します。再採番・run記録・ports予約は呼出元に残し、元run_id/nonce/diagnostics/期限を明示して渡します。contextは生成済みsession/processを渡した後もconsole logを開いたまま保持し、通常handshake/proofの終了又は例外時にだけ親logを閉じます。生成失敗時のfinally記録と所有process保持、adapter内部準備後のbefore_create期限検査を維持します。

第42の部分は通常経路の切出しだけです。第43では第41の固定結果/callbackをrun正本のwrite内照合まで接続しました。元予約の再取得・可変slotからの基準再採用を許可する入口は追加しません。新offline runのdescriptor/M生成・GRANT/ACK/最終返却は後続で同じ固定callback/期限へ接続します。

## 新startのports予約（第41スライス先行部分）

`_offline_reserve_start`は、同じ固定run/nonce/承認・Ready証拠から、start/PENDING・artifact参照と全endpoint予約を一つの短いRegistry transactionで取得します。既存`runtime_ports.reserve`のwildcard/dual-stack/host/protocol/address/port競合規則を使い、部分予約や新startの記録だけを残しません。他runの予約を消したり空きportへ切り替えたりしません。

返却する同じ確定callbackは、元の全bindingに加えて、当該instance又はrunのendpoint予約が固定宣言の全service/値と完全一致することを照合します。commit前は全体rollback、commit後の不明・期限超過・改変では元slot/原文/資源と永続予約を保全し、再生成/再送/retryしません。正常renewでは現在expiresを確認し、入口deadlineは延長しません。

本スライスはportsの永続予約までです。既存`_start`の再採番・生成・handshakeを分離して同じrunへ接続するM生成、OS adapter準備後の新予約照合、GRANT/BOUND/ACK・RUNNING確定・公開Ready入力は後続です。既存startの保守的遮断と固定Mod/Data契約は維持します。実socket bind・実OS/native受入は未確認です。

## 新startの内部予約（第40スライス）

RuntimeControl._offline_reserve_start(token, *, operation_id, read_operation_id, open_operation_id, ready, deadline) は、同じ期限で公式読取を一度行い、新しいstartの耐久予約まで接続します。start・読取・元openは別IDとし、Ready四項目を呼出元が独立に指定します。読取IDはUUID、start/open IDは既存identifier契約です。

Pは新runtime run/nonceを一度生成し、短いRegistry transactionでstart/PENDING、新runのSTARTING/pending=1、専有artifact予約を記録します。元run/受付/stop/offline終端/writer正本と全解放資源を保持し、予約後は旧STOPPED・旧current run・予約不在を要求するcallbackへ戻りません。現在Ready原文、元helperの確定binding、独立profile承認、新startの要求指紋・意図・元正本集合を最終返却まで照合します。

戻り値はOFFLINE_START_RESERVEDの結果dictと、確定時の同じ原本・参照を閉じ込めた照合callbackの組です。Pメモリ上のreadersを耐久予約として流用せず、可変属性から期待基準を採り直しません。commit後や返却不明では元意図・予約・原文・識別を保全し、再生成・再送・予約置換をしません。

この入口はMを生成せず、port予約・GRANT/BOUND/ACK・RUNNING確定・公開Ready入力は後続です。新しいpending意図の正規recoverも後続のため、公開startへ接続しません。既存startの保守的遮断は維持し、旧runの承認だけでは通しません。P側限定の期限保証とGRANT後Mod内部を既存期限へ委ねる判断、実OS/native/実Data受入の未確認範囲は維持します。

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

成功時もpipe buffer/handleを保持し、`game_exit_result`と戻り値は局所観測結果です。Registry確定・writer CLOSED・次openの許可を表しません。製品呼出元の正本予約/台帳/元Mhandle供給と最終transactionでの退出確定は第20・第21節、独立明示Readyの次open供給は第23節を参照してください。第15スライスでは稼働RuntimeControl/ManagedRuntime/Registryを変更していません。認証済保存原文を停止結果へ引き継ぐ第16スライスの前段と、観測C自体の製品接続は区別します。実OS/native/実root受入は未確認です。


## 認証済保存原文とstop正本の接続（第16スライス先行部分）

既存ManagedRuntimeは認証済SAVE_REQUESTED/COMPLETIONの原frameを元sessionへ保持します。COMPLETIONの送信とpeer/endpoint close後、同じtransaction内で元descriptor・lease/epoch/generation/run、RunningからのRequested、原文readback、最新BLOCKED revision・全writer台帳、元stop PENDINGと参照予約を照合してから、そのstop意図へhash参照を記録します。close不明・応答喪失では再送せず、原文と元予約を保全します。

`ManagedRuntime.save_evidence(connection, instance, session, deadline)`は呼出元のtransactionでこの対応を再照合し、hash参照と元stop予約本文を返す内部APIです。`managedSave`はSAVE_REQUESTEDの`operationId`、要求/応答bodyの`requestBodySha256`/`responseBodySha256`、`descriptorSha256`です。本文は既存mod-control台帳を参照し、追加保存しません。公共stopと直接stopの最終結果transactionでも同一参照を再照合し、結果へ引き継ぎます。

元STOP_BLOCKのdeadlineをSAVE_REQUESTEDの受信・close・直接stopの結果確定まで引き継ぎ、phaseごとに再起算しません。公共stopは通常待機・直接stopへの合流予約と待機にも、呼出元の固定期限と元STOP_BLOCKのいずれか早い期限を渡します。記録前・照合直後・commit後・返却前まで同deadlineを検査します。確定前の失敗は元参照と原文を保全し、公共stopのRECOVERY_REQUIRED記録にも保存原文参照を残します。commit後の期限超過では確定記録を巻き戻さず、正常応答を返しません。 同じ確定済みstopから旧control slotを終端するため、timeout後の同要求readbackと次startに旧slotを残しません。合流は元stopの確定証拠を変更せず、自分の予約前・最終照合後・commit後・返却前を検査します。新規合流の初回応答と、同一の確定済み要求のreadbackを区別します。元要求のreadbackは追加作用をしません。Pの原要求/応答に対する自動再送・kill・rollbackは追加しません。

この参照は認証済台帳の由来を表すもので、元M/全childの実終了、Data候補・saveproof実体、観測C終了、Ready、writer CLOSEDの証明ではありません。元Mhandle保持部品は第17スライスを参照します。候補原本取得・観測C実接続・退出証拠のRegistry記録は第19〜第21節、独立明示Ready供給は第23節を参照してください。現在のstop結果や局所Readyから、次openの期待値を自動確定しません。実OS/native・実root受入は未確認です。


## 認証済Mの生成handle保持（第17スライス先行部分）

認証済handshake後、Pは元MのOwnedProcessと生成時のrun/instance/generation/artifact/nonce/native identity/creation、Data owner、descriptor hashを固定します。macOSのboot照合は生成identityと同じUUID表現へ正規化し、Data ownerと生成snapshotの原値は保持します。停止時は同じ保持objectと生成snapshotを照合し、既存の終了観測・Registry停止記録を続けます。第17スライスの保持段階では稼働process mapからだけ退避し、正常handle解放は第24/25/27節の確定退出経路で行います。通常control slotの終端と次startは維持し、元Mと次Mをrun別に保持します。従来の非認証通信fixture境界とGameTestのcloseは変更しません。

`RuntimeControl._game_process(run)`は保持した元handleと生成snapshotのcopyを返す内部取得です。PIDからの再attach・別processの採用・native本人再照会を行いません。この取得だけで停止・保存・全writer・候補・Registry退出・Ready/CLOSEDを証明しません。原要求hash参照/候補原本から観測Cへの単発供給は第20節、観測済みslotを停止結果・最終Registry transaction/同deadlineへ結束する処理は第21節を参照してください。既存stopからの内部明示候補による観測開始は第22節、独立明示Ready供給は第23節、元handle正常解放は第24/25/27節を参照してください。

保持資源はPのメモリ内にあり、P再起動後に復元しません。正常解放の対象外・失敗/不明の資源は保全し、対象game退出の確定後にだけ第24/25/27節の単発解放へ進みます。解放済みobjectと固定snapshotも元run/stopの終端保持先へ残します。実OSでのhandle資源消費・本人受入は未確認です。Ready期待値を自動承認する変更はありません。


## 保存原文と保持元Mの結束（第18スライス先行部分）

`ManagedRuntime.save_evidence`は、認証済Requested/COMPLETION・元stop予約・閉門/全writerの原文照合に続き、同じRegistry transactionで元run記録を読みます。run/instance/generation/artifactと、保持した元Mの生成identity/creation/handleを照合し、生成時のData ownerとdescriptor hashを保存原文へ結束します。Data ownerの原表記を維持し、native identityのUUID表現へ書き換えません。

初回の保存原文参照の記録前と、公共stop/直接stopの最終transactionで同じ照合を行い、その完了後に元deadlineを検査します。不一致・期限超過では原文・PENDING/参照・保持資源を保全し、追加IPC/生成・自動再送・再attach・killへ進みません。既存stopの返却形式とcontrol slot終端は維持します。

この照合は元Mの実終了、候補/退出証拠、Ready/CLOSEDを表しません。候補原本・全writer・正本予約を供給するP内部の観測Cへの単発接続は[第20節「同stopから観測Cへの単発接続」](#同stopから観測cへの単発接続第20スライス先行部分)で実装済みです。観測済みslotを停止結果・最終Registry確定へ結束する処理は第21節を参照してください。既存stopからの内部明示候補による観測開始は第22節を参照してください。明示Readyの次open供給は第23節を参照してください。元M/Cの正常handle解放は第24節、公共/直接stopの自動接続は第25/27節を参照してください。実OS/native/実root受入は未確認です。

## 不変game候補原本の入力結束（第19スライス先行部分）

P内部の`ManagedRuntime.game_exit_inputs(instance, session, candidate, deadline)`は、元stopのPENDING/参照を事前と最終の短いtransactionで再照合し、終端した認証済保存原文と記録済みmanagedSave・保持元Mを照合します。candidateにはworldUuid/runUuid/stateSha256/stateRevisionの期待値を明示します。currentから現在値を読み取って期待値を自動更新しません。

原本I/Oは双方のtransactionとinstance lockの外で行い、競合するstartのPENDING拒否を妨げません。元descriptorのworld配下`sushieric/run-state/states/<期待hash>.json`と候補内の参照に対応する`saveproofs/<hash>.json`だけを読みます。既存codecでSavedPendingExitと元Requestedのworld/run/generation/paths/previous-state/revision/owner/management、saveproofのsource/owner/認証済COMPLETIONを相関照合し、原bytesを再serializeせず返します。raw fileのSHA-256には既存hash関数を使い、Dataの正規化・保存・chain/seal・recover・退出算法を実装しません。current/不変chain/実DBclose/本人死亡の最終検査はC本人のData APIへ委譲します。

戻り値は観測C向けの管理/candidate欄、候補原bytes、saveproof原bytesのtupleです。operationId/requestHash、P/C本人、private session/nonceは含めず、後続の正本予約と起動時に補います。原本I/O後に元保存原文/正本予約/保持Mを再照合し、最終境界で同deadlineを検査します。不一致・原本欠落/変更・期限超過は例外で保全し、別候補/currentへのfallback、DB更新、IPC、生成、終了観測、kill、handle解放を行いません。

この読取り入口を用いるP内部の観測Cへの単発接続は[第20節「同stopから観測Cへの単発接続」](#同stopから観測cへの単発接続第20スライス先行部分)で実装済みです。観測済みslotを停止結果・最終Registry確定へ結束する処理は第21節を参照してください。既存stopからの内部明示候補による観測開始は第22節を参照してください。独立明示Readyの次open供給は第23節を参照してください。元M/Cの正常handle解放は第24節、公共/直接stopの自動接続は第25/27節を参照してください。戻り値をReadyやCLOSEDへ自動昇格しません。実OS/native本人受入・実root/Mod/実bundleビルドは未確認です。

## 同stopから観測Cへの単発接続（第20スライス先行部分）

P内部の`ManagedRuntime.observe_game_exit(instance, session, candidate, deadline)`は、保持sessionに作用前から単発slotを確保し、第19スライスの原bytes/要求欄と同stop ID/fingerprint/control ID・保持元M/生成snapshotを固定します。固定generation/runtimeのartifactとData世代を既存検証へ通し、独立OwnedProcessの未使用Cをslotへ保持してから、既存`OfflineProcess.observe_game_exit`へ一度だけ渡します。operationIdは元stop ID、requestHashはcommand/stopFingerprint/inputsから既存canonical/digestで固定します。P/C本人とprivate session/nonceは生成部品が補います。

callbackは元session/slot、stop識別、同原本/要求欄、保持M object/生成snapshotとslot記録の一致を継続照合します。第19スライスの原文照合を通し、BLOCKED/drain/全writerと元PENDING/参照を維持します。原本I/OとOS/IPCは短いtransactionの外です。元Mの正常exit/全child不在→C本人Data退出→C正常exit/全child不在は既存単発部品へ委ね、旧PIDへ再attachしません。

同deadlineをcallbackとC完了後、局所結果保持の前後まで検査します。失敗後もslot/原文/元M/観測Cを残し、別候補への変更・再生成・再送・再観測・kill・rollbackをしません。局所結果保持後に期限を跨いだ場合は証拠を保持しますが、成功として返しません。

返却`GAME_EXIT_OBSERVED`とslotのresultは局所観測です。観測済みslotを既存公共/直接stopの結果と最終Registry transactionへ結束する処理は第21節を参照してください。既存stopからの内部明示候補による観測開始は第22節を参照してください。独立明示Readyの次open供給は第23節を参照してください。元M/Cの正常handle解放は第24節、公共/直接stopの自動接続は第25/27節を参照してください。呼出元Pが元stop予約とsessionを保持する前提で、解除/差替はcallbackで拒否します。Ready/CLOSEDへ自動昇格しません。実OS/native本人/実Job/group/実IPC結合と保持資源量は未確認です。


## 観測済みgame退出証拠とstop確定の結束（第21スライス先行部分）

第20節のslotがある場合、公共/直接stopは元候補原本をtransaction外で再照合し、`ManagedRuntime.game_exit_evidence`で元M/Cの保持object・生成record・要求/応答/結果原本を照合します。開始済みで結果不明のslotを既存proofだけで正常確定しません。未開始の既存stopは従来の形式・経路を維持します。観測Cを自動起動するP内部の候補入力は第22節を参照してください。公開RPCは追加していません。

最終transactionでは既存`save_evidence`による元stop/認証済原文・閉門/drain/全writer照合の後、同じgame退出証拠を再照合します。ここではOS照会・file読取り・追加IPCを行いません。同deadlineを最終照合後・結果更新前と、確定後/返却前にも検査します。結果JSONの任意欄`gameExit`は明示候補4欄と要求/応答/観測/観測C recordのSHA-256だけです。要求本文・本人の原UUID・局所Ready本文を監督DBへ追加せず、Ready/CLOSEDへ自動昇格しません。テーブル変更はありません。

確定前の不一致・期限超過では元予約/原文/slot/M/Cを保持し、追加Data作用・再観測・再送・killを行いません。確定後timeoutではCOMPLETEDのgameExit証拠を保持し、既存のsession解放を実施します。直接stopの確定後失敗で同run/元stopの停止proofを保全する扱いは第27節を参照してください。active sessionの解放前に、観測Cのslot（元OwnedProcess・固定原本/生成snapshotを含む）とdescriptor/startup/stop/Requested/COMPLETION原文をRuntimeControl.game_exitsへ元run/stopをkeyにして移します。既存M保持と同じRuntimeControl/lockを使い、_game_exitで同じC objectを取得できます。active mapは残さず、次startのcontrol slotを塞ぎません。元M/C handleの正常解放の内部入口は第24節を参照してください。公共stopの自己予約経路への自動接続は第25節、直接stopと合流応答の解放正本への結束は第27節を参照してください。

P内部の明示候補供給と観測開始の順序付けは第22節を参照してください。独立明示Readyの次open供給は第23節を参照してください。公共stopの自動解放は第25節を参照してください。直接stop/合流の自動解放と応答接続は第27節を参照してください。公開供給元は後続です。呼出元Pは観測からstop結果確定まで元session/予約を保持します。確定後は終端保持mapが観測Cと固定原文/生成snapshotを所有し、呼出元のsession参照に依存しません。実OS/native・Job/group・private IPC結合と保持資源量は未確認です。

## 第22スライス先行部分: P内部の同stop専用候補入力

案1を採用し、P内部だけに候補入力契約を追加した。公開execute payload・Mod wire・固定依存Modは変更しない。案2の保存後wire通知は本repoのみ・固定依存成果の範囲を維持するため採用していない。候補の実供給元（Manager/CLI公開受付）は後続であり、現時点では未定。

信頼済みP呼出元は停止予約前のRUNNINGに対して `managed.select_game_exit(token, run_id=元run, operation_id=予定stop, descriptor_sha256=元descriptor原hash, deadline=固定期限)` を一度呼ぶ。その後は既存 `runtime.execute("stop", payload, operation_id=予定stop)` を使う。選択しない停止の入力・挙動は従来どおり。

保存原文が確定した後、同じPから `managed.supply_game_exit(token, run_id=元run, operation_id=予定stop, descriptor_sha256=元descriptor原hash, candidate=明示4欄, deadline=同期限)` を一度供給する。4欄はworldUuid/runUuid/stateSha256/stateRevision。候補は呼出元が独立に指定し、callback・任意path・current自動採用を受け付けない。供給tokenと元run/stop/descriptor・正本予約を最初の短いtransactionで認証し、成功後に一度限りの受付を確保する。未認証・元予約不一致は受付/event/原本/予約/元handleを変えず拒否する。原本I/O後にも認証と元予約を再照合し、Registryへ入力hashだけを記録する。Pが保持する入力原文に供給・選択・元stopの期限のminを固定する。

候補受付を選択したstopだけ保存原文確定後に同deadline内で入力を待つ。元run/stop/descriptorと正本参照を再照合して観測Cを一度開始し、第21スライスの最終transactionへ進める。受付確保後の候補欠落・期限超過・不一致は原本/予約/M/Cを保持して非正常終了し、既存proofへfallback・再供給・自動再観測をしない。認証前の拒否は受付を消費しないので、正規入力を引き続き一度だけ受けられる。通知と成功確定をcontrol lock内で行い、停止側は通知後に確定状態と供給期限を再読込する。短い供給期限をC観測/callback/最終transactionまで引き継ぎ、通知後timeoutの入力からCを開始しない。予定stopと異なる直接停止への選択流用も拒否する。候補待機/原本照合/観測/結果確定は元stopと選択入力の短い方の期限に従う。

観測に使った入力原文もactive解放前に元run/stopの終端保持先へ移管する。局所結果をReady/CLOSEDへ自動昇格しない。独立明示Readyの次open供給は第23節を参照してください。元M/Cの正常handle解放の内部入口は第24節を参照してください。公共stopの自動解放は第25節、直接stop/合流の自動解放と応答接続は第27節を参照してください。実OS/native受入は未実施。


## 確定game退出と独立Readyから次C取得（第23スライス先行部分）

信頼済みP呼出元は、元stopのRegistry確定後に独立して指定したReady4欄を既存 `runtime._offline_open(token, operation_id=次操作, revision=閉門revision, ready=明示4欄, deadline=次openの固定期限)` へ渡します。第22節の観測Readyと同じworldUuid/runUuid/stateSha256/stateRevisionを明示して供給します。旧stopとは別の操作期限を指定します。

元stopにgameExitが記録された取得では、確定済みCOMPLETED stop・保存proof・閉門/全writer台帳に加え、Pが保持した元run/stopの終端binding、認証済保存原文、C要求/応答/生成record/resultのhash参照を照合します。独立Readyと観測応答の4欄を型も含めて一致させ、C本人が既存Data APIで実Readyを再照合してOfflineIdleへ進みます。初回、予約と同transaction、起動中、結果更新前に原本を照合し、予約直前・結果更新直前・記録後返却前まで次openのdeadlineを適用します。 固定bundle command生成・P本人照会・journal・stderr設定を終えた後、OwnedProcess.start直前にも元verifyを呼び、準備中の期限超過をOS生成前に拒否します。

拒否時は元stop・原本・保持M/Cを保全します。次C生成後の失敗では次openのPENDING予約と同Cを保持し、追加作用と同open再入口を拒否します。明示値を供給する呼出元の内部契約に限定し、Manager/CLI公開受付は後続で扱います。元M/Cの正常handle解放の内部入口は第24節を参照してください。公共stopの自動解放は第25節を参照してください。直接stop/合流の自動解放と応答接続は第27節を参照してください。C本人closeの内部接続は第28節を参照してください。C実終了後のRegistry CLOSEDとReady/CLOSED start gateは後続です。実OS/native受入は未確認です。

選択→公共stop→候補供給→観測C→stop最終確定→active解放→独立明示Ready→次C OPENは、同じ実Registry transactionの結合fixtureで検査します。元供給期限を過ぎた次openでも独立した新期限内なら取得し、game_exit_inputの原文を元run/stopの終端保持へ残します。

## 確定stopの元M/Cを単発解放（第24スライス先行部分）

信頼済みP呼出元は、game退出観測を含むstopのRegistry確定とactive session解放後、次OPEN前に `runtime._release_game_handles(token, operation_id=新規解放操作, stop_operation_id=元stop, deadline=解放操作の固定期限)` を一度呼びます。公共stopの自己予約経路からの自動接続は第25節で実装しています。直接stop/合流への自動接続は第27節を参照してください。

元COMPLETED stopとrun/世代/lease、受付BLOCKED・全writer終了、Pが保持する元M/C objectと生成snapshot、認証済原文・観測要求/応答/結果を既存検査で再照合します。同じinstanceのtransactionでPENDING操作とartifact予約を作り、同transactionでも単発枠を再確認します。M→観測Cの順で既存OwnedProcess.closeを一度ずつ呼び、観測Cの親側pipe input/outputも既存close_after_exitで解放します。close直前、close後、最終transactionの結果更新前、記録後返却前まで同deadlineを検査します。

各資源は呼出し直前にUNKNOWNとして元slotへ保持します。MはOwnedProcess.closeの正常返却後、観測CはOwnedProcessと親側pipeの両fdの解放後だけRELEASEDへ進みます。各親側fdのclose直前にも同じ正本/bindingとdeadlineを検査し、片端失敗では成功した端をNone、残る端とCのUNKNOWN/PENDINGを保持します。WindowsのJob CloseHandle失敗も既存_checkから例外として伝播します。部分失敗・期限超過・原本不一致では、解放済みobjectも含め固定原文/生成snapshot・進捗・PENDING予約を保全し、同一/別operationの再入口を拒否します。自動再close・再観測・再attach・kill・C再生成・rollbackはしません。確定後timeout/応答喪失もCOMPLETED結果を巻き戻しません。失敗後の正規recoverは今回の入口に含めません。

M/CのOwnedProcessと観測Cの親側pipe両fdを解放した成功時だけ、game-handle-release操作をCOMPLETEDにして当該予約/pendingだけを解放します。元stopのproofは書換えず、Ready承認・writer CLOSEDを作りません。元M/C object・固定PID/group・snapshotと原文はPの終端保持先に残し、解放済みhandleのpoll/membersを呼ばずに第23節の次OPENへ独立明示Readyを供給できます。解放を呼ばない従来経路の契約は維持します。実OS/nativeの正常解放と部分失敗受入は未確認です。

## 第25スライス 公共stop確定後の正常解放

公共execute stopが自分の予約を確定したgame退出では、元stopの結果へP内部のgameHandleReleaseOperationIdを固定する。active control slotを終端資源保持先へ移した後、第24節の専用解放を同じdeadlineで一度開始し、解放正本のCOMPLETEDと元stopとの結束を照合してから正常応答を返す。停止結果の保存原文・退出参照を保持したまま、解放の結果は独立runtime operationに記録する。

解放前の期限切れではOS作用を開始しない。部分解放失敗では第24節のUNKNOWN/PENDING・成功したfdのNone・残存元handleを保持する。解放完了後の期限切れでも正常応答を返さず、元stopと解放の確定済み結果を保持する。再提示は同run/leaseと固定stop結果・解放operation・RELEASED進捗を照合する。未開始/PENDING/不明は拒否し、正規recoverへ残す。

第25節の自動接続は公共stopの自己予約経路に限定する。直接stopと公共stop合流の待機予約終端は第26節、直接stopからの自動解放開始と解放結果への合流応答の結束は第27節で扱う。独立明示Readyは次OPEN呼出元が供給し、M/Cの解放結果をReadyやwriter CLOSEDへ自動昇格しない。実OS/native受入と公開候補供給元は後続。

第25節の公共stopは同leaseの期限切れでも完了・再提示を受け付ける。元stopのproofに固定されたgameHandleReleaseOperationIdと同じ解放操作に限り、解放予約・各close・最終結果の照合へ期限切れ許容を引き継ぐ。leaseId/epoch/世代/run・確定proof・単発枠・元M/C・正本予約の照合とmonotonic deadlineを適用する。解放済み結果の再提示は同じstop契約で読み戻し、closeを再実行しない。次offline取得と、元stopへ結束されていない解放は期限内leaseを要求する。

## 第26スライス先行部分 直接stopと公共合流の待機予約終端

直接stopを親として待機した公共stopは、元run/stopと同lease/epochの予約へ束縛する。元stopの最終transactionで、同じstop参照を持つ待機予約とPENDING要求を照合し、親の不変結果を各合流操作へ同時に記録して予約を終端する。待機側の返却や生存に予約解放を依存させない。別stop/instanceの予約は変更しない。不一致や最終期限超過はtransactionを確定せず、原本・予約・元handleを保持する。

受付前照合済みの公共呼出しが親確定後に合流登録へ進んだ場合は、確定proofを照合して同じ結果だけを登録し、新しい待機予約を作らない。待機中と返却前に同じbinding・親と合流の結果・予約終端・固定deadlineを検査する。親の確定後に待機側が期限切れとなった場合も正常応答せず、既知の停止結果を保持し、再提示では元結果だけを照合する。lease期限切れのstop完了・再提示は既存どおり受け付ける。

この節は予約終端と停止結果への結束を独立させた先行部分である。直接stopから元M/Cの自動解放を開始し、合流が解放結果まで待って応答する接続は第27節を参照する。第25節の公共自己予約stopは変更しない。新しいRPC/wire・Data算法・Ready/CLOSED自動昇格・自動再close/recoverは追加しない。実OS/native受入は未確認。

## 第27スライス 直接stopの正常解放と公共合流応答

game退出の確定証拠がある直接stopも、停止結果へP内部のgameHandleReleaseOperationIdを固定する。親stop・全合流結果と待機予約の終端を同transactionで確定し、元M/Cと原文・snapshotを終端保持へ移した後、第24節の正常解放を同じ期限で一度呼ぶ。候補供給/観測で短縮されたstop_deadlineを最終transaction・解放・返却まで引き継ぐ。game退出を伴わない既存停止は解放IDを作らず従来の契約を維持する。

公共合流は親のCOMPLETEDだけでは正常返却せず、同じ解放operationの確定を待つ。受付前照合済みの遅延合流も、解放PENDING中は待機予約を再生成せず同じ停止結果へ結束する。元run/lease/stop・親proof・要求fingerprintと自分の停止結果を照合し、既存の解放結果照合を読み取りだけで呼んでから、固定min deadline内に返す。合流側がcloseを開始・再実行することはない。

制御threadの確定後timeout・解放失敗はsession.errorへ残す。同run/元stop/fingerprint/control operationと停止proofの確定が一致する場合、既知の停止結果や認証済原文をRECOVERY_REQUIRED/UNKNOWNへ上書きしない。確定前失敗と別runの拒否は維持する。部分失敗では元handle/object・原文・snapshot・UNKNOWN/RELEASED進捗・解放PENDING予約を保持する。解放完了後の期限切れや応答喪失も既知結果を保全する。

再提示は第25節と同じく、同run/stopの確定済み解放正本だけを照合する。解放未開始/PENDING/不明は拒否し、自動再close・再attach・C再生成・kill・rollbackは行わない。期限切れleaseの同stop完了/再提示を維持し、次offline取得のlease条件は緩めない。元M/Cや親pipeを解放したことからReadyやwriter CLOSEDを推測せず、次OPENには独立明示Readyを供給する。

部分失敗/応答不明の正規recover、第28節のclose応答後のC実終了/Registry CLOSED、Ready+CLOSED start gate、Manager/CLI公開候補供給元、実OS/native/root/Mod受入は後続である。

第27スライスの解放結果照合は、元M/CのRELEASED進捗と同stopへ束縛したRegistryのCOMPLETED結果を正本にする。DB確定後のメモリ完了公開前でも同じ結果を読み戻し、closeを再実行しない。

現行の直接stopはSTOP_BLOCK受信時に独自のstopOperationIdを生成する。停止前にIDを固定するselect_game_exit/supply_game_exitとは一致しないため、合流受付後に選択済み候補の供給期限でstop_deadlineを短縮する経路は未接続である。選択済み公共stopの供給期限の伝播と、直接stop合流の受付時min期限を区別する。将来この内部入力を直接stopへ接続する際は、合流待機・結果照合・返却にも短縮後期限を伝播して検証する。

## 第28スライス先行部分 最新IdleからC本人close

P内部の`RuntimeControl._offline_close_owner(token, operation_id=..., deadline=...)`が元Cのcloseを一度開始する。既存offline-open正本予約、管理binding、受付遮断、全writer、操作履歴を再照合し、最後のWの正常終了・complete結果記録とACKから最新Idleを選ぶ。OPEN直後のW未受理sessionでは、記録済みOPEN応答を使う。非空の型付き編集要求は元操作の原文として保持する。

元offline-open操作へ`ownerClose`意図を同transactionで固定し、次のW/編集受付を遮断する。同じclose操作IDの検証が正本の意図と最新Idleを照合しながら、同pipeで既存Java close入口へ期待state hash/revisionを送る。C本人の`WorldOfflineWriterSession.close`がDataのcurrent/OfflineIdle・owner・management/generation・DBclose/sealを検証する。

PはOWNER_CLOSED応答のoperation/hash/session/nonce、同run、revision+1を照合し、元Cに要求・応答bytesと生成記録を保持して正本へ応答hashを記録する。応答直後の正常exitは保持した元handleと生成snapshotで照合する。全体deadlineを応答後・最終正本照合後・結果更新前・記録後まで検査する。拒否や失敗時は元意図・原文・C/W・予約を保持する。

この段階の正常応答後もoffline-openはPENDING、instance.pending=1、artifact予約を保持する。Cの実exit0/全child不在の観測とRegistry writer CLOSED・予約解放は次段階に接続する。close後の返答不明は正規recoverで扱う。Ready期待値は独立明示入力として次openへ供給する。Ready+CLOSED start gate、#54復旧、公開呼出元、実OS/native/root/Mod受入も後続で扱う。

Cが保持するcallbackは元予約・確定stop/Ready原本・lease/本人bindingの継続照合を担います。openの期限は取得操作に適用し、以後のcloseには今回の期限を用います。取得時の期限経過後も、同じCと最新Idleを照合してcloseできます。closeへ渡す期限は起動応答ACKと最終結果記録まで検査します。

prepareも今回のdeadlineをC/W起動ACKへ渡します。Wを含むOS生成直前、ACKの本人照合後・記録前と記録後、最終結果のJSON直列化後・正本更新前、commit後、ACKから返却する直前に期限を検査します。期限超過時はPREPAREDを返さず、確定済み原文・予約・ACK進捗を保持します。Wが保持するbinding照合も準備期限をsession寿命として捕捉せず、後続操作にはその操作の期限を用います。

## 第29スライス先行部分 C本人close後の実終了観測

記録済みclose応答を持つP呼出元は、`runtime._offline_observe_owner_exit(token, operation_id=元close操作ID, deadline=今回の固定期限)`を一度呼びます。close時に固定した期限と今回の呼出期限の短い方を、観測意図・待機・最終transaction・返却まで使用し、close期限を延長しません。取得時のopen期限はCの寿命へ持ち込みません。元offline-open予約・最新Idleと履歴・全W終了/complete ACK・同close要求/対応応答を再照合し、短いtransactionで`ownerExit`意図を記録します。待機中も同じ意図と正本を照合し、競合する観測・close・prepare・startを拒否します。

close時に固定したP/CのPID/start/host/boot・生成記録・元OwnedProcess/group・pipeと原文を継続照合します。生存中の本人照会と終了が競合した場合は元handleのexit0を要求し、終了済みPIDは再解決しません。元Cのexit0と所有groupの全child不在を観測した場合だけ、局所の`OWNER_EXIT_OBSERVED`原文とそのhash参照を元offline-open正本へ記録します。DataのReady応答だけでは終了と扱いません。

今回期限は本人照合・poll/group観測の前後、原文直列化・ローカル記録後、最終正本の直列化後/更新前、commit後/返却前まで適用します。不明・非zero・期限切れ・不一致・記録失敗は原文・意図・元予約・handle/pipeを保全し、再送・再観測・kill・再attach・C差替を行いません。commit後の期限超過でも既知の結果を巻き戻さず正常返却を拒否します。

成功時もRegistry writer CLOSED、予約終端、handle/pipe解放は行いません。元offline-openはPENDING、pending=1、artifact予約を保持します。終了証拠からCLOSEDと正常解放を接続する段階、独立明示Ready+CLOSED start gate、#54復旧、部分解放失敗/応答不明の正規recover、公開呼出元は後続です。実OS/native本人受入は未確認であり、既存fake/合成fixtureで検証しています。

## 第30スライス先行部分 元Cの正常handle/pipe解放

P呼出元は終了観測の正本記録後、`runtime._offline_release_owner_handles(token, operation_id=元close操作ID, deadline=呼出期限)`を一度呼びます。元closeの要求/対応応答・最新Idle/履歴・全W終了とcomplete ACK、ownerExit正本hashと保持原文、元P/C生成binding・同handle/pipeを照合します。close時の固定期限と呼出期限のminを、解放意図の記録・lock待機・各作用・最終更新/返却まで引き継ぎます。

元offline-openへownerRelease意図を保存し、元CのOwnedProcess.close、親側pipeのclose_after_exitを順に呼びます。元Cの終了を再観測したりPIDを再解決したりしません。各資源は作用直前にUNKNOWN、正常返却後にRELEASEDを保持し、各fdの直前にも正本/bindingと期限を照合します。途中失敗では成功したfdのNone、残存fd、UNKNOWN/RELEASED、要求/終了原本と元objectを保全し、同一/別IDの再close・再送・kill・C再生成へ進みません。

正常解放の原文と正本hashを記録した成功も、元offline-open PENDING/pending=1/artifact予約を保持します。Registry writer CLOSED・予約終端、残るW等の資源解放との統合は後続です。Ready+CLOSED start gate、公開受付、#54復旧、部分解放失敗/応答不明の正規recoverと実OS/native本人受入も今回の範囲外です。

## 第31スライス先行部分 履歴を含む全Wの正常解放

P呼出元は元Cの解放記録後、`runtime._offline_release_worker_handles(token, operation_id=元close操作ID, deadline=呼出期限)`を一度呼びます。ownerReleaseの保持原文・正本hash・RELEASED進捗・親側pipe解放を要求し、元close/終了原文・最新Idle・全履歴のcomplete ACKと予約を再照合します。生成時に保持したprocess/pipe参照、P/Cと各Wの生成本人・独立group、元割当/候補/終了原文と正本hashが一致した場合だけ、履歴順に各Wのhandle、親側pipeを正常解放します。WのないOPEN後closeは空の解放結果を記録します。

元offline-openへworkerRelease意図と元C解放hash・全Wの要求/割当/終了/生成記録hashを保存します。全Wを各作用の前後・各fd直前に再照合し、close時固定期限と呼出期限のminを最終JSON/更新・commit後/返却まで適用します。終了済みC/WのPID/group再観測、再attach・kill・再closeは行いません。部分失敗では各WのUNKNOWN/RELEASED、成功したfdのNone、残存元object・原文・元予約を保全します。後続Wの解放を続行しません。

正常結果WORKER_HANDLES_RELEASEDの原文と正本hashもC/Wと共に保持します。元offline-open PENDING/pending=1/artifact予約は継続します。Registry writer CLOSED・予約終端、Ready+CLOSED start gate、公開受付、#54復旧と正規recoverは後続です。実OS/native本人受入は未確認で、既存fake/合成fixtureと実Registryで検証しています。

## 第32スライス先行部分 offline writer CLOSEDの正本記録

P内部の`runtime._offline_record_writer_closed(token, operation_id=元close操作ID, deadline=呼出期限)`は、元C/全Wの正常解放後に一度呼びます。元close/終了/解放原文、ownerRelease/workerRelease正本hash、全RELEASED進捗・全親pipe解放、元binding・最新Idle/履歴・起動/complete ACKと元予約を再照合します。終了済みPID/groupの再観測・native close・Data API呼出しは行いません。

元offline-open応答のwriterClosedへ単発意図を先行記録し、元CへOFFLINE_WRITER_CLOSEDの原文と局所記録を保持します。最終transactionで対応するstate=CLOSEDと応答hashを記録した場合だけoffline writer CLOSEDが確定します。意図の存在や局所応答だけではCLOSEDと扱いません。既存writer_sessionsはRunning中のManagerのrun/nonce/PID登録であり、このoffline Cのprivate sessionとは混在させません。

close時と呼出時の短い期限を、lock待機・各照合・局所記録・最終JSON/更新・commit後/返却へ適用します。失敗は原文・意図・元object/予約を保全し、再close・再観測・自動recoverしません。commit後の期限超過も既知CLOSED証拠を保持して正常返却を拒否します。今回は元offline-open PENDING/pending=1/artifact予約を維持するため、既存startはCLOSED確定後も拒否します。元予約の終端と終端後照合の分離、独立明示Ready+CLOSEDのstart gate、公開受付、#54/recoverと実OS/native受入は後続です。

## 第33スライス先行部分 元offline-openと予約の終端

P内部の`runtime._offline_finish_close(token, operation_id=元close操作ID, deadline=呼出期限)`は、記録済みwriterClosedのstate/hashと原文、元C/全Wの解放正本・全RELEASED/親pipe閉鎖、元binding・最新Idle/履歴・全ACK・元予約を再照合します。元offline-open応答へtermination意図を先行記録し、OFFLINE_SESSION_COMPLETED原文と局所記録を元Cへ保持します。最終instance transactionで元offline-openだけをCOMPLETED、元artifact予約を削除、instance.pendingを0にします。元stop/Ready/run・解放/CLOSED正本は維持します。

終端時に元Cと履歴を含む全Wをactive枠から退避します。`_offline.retired[(instanceId, openOperationId)]`は元process/pipe/object・要求/対応応答bytes・履歴・生成bindingを保持します。終端後はPENDING前提の元callbackを変更・再実行せず、終端正本・元run/閉門/停止証拠と退避証拠を専用経路で照合します。終了済みC/Wの再観測・再解放・Data呼出しは行いません。

履歴を含む全Wのcontroller参照が元Cと同一objectであることも、終端後の正常返却条件に含めます。snapshot比較・read transaction終了後に期限を再検査し、結果copy後も局所記録の開始直前に検査します。期限到達後は局所記録を開始せず、意図・原文・元objectを保持します。

close時と呼出時の短い期限を意図・局所記録・最終直列化/SQL・transaction内の終端照合・commit後照合・返却まで引き継ぎます。最終SQL後の不一致やcommit失敗ではDBをrollbackし、退避済み元object・意図・局所結果は保持します。この場合active枠は復元せず、元pending/予約が後続取得を遮断します。commit後の期限切れ/不一致でも既知終端を巻き戻さず正常返却を拒否します。通常入口から再終端・再生成・自動recoverしません。

予約解放だけで既存startが通らないよう、同世代のoffline終端正本を事前確認と最終予約transactionの双方で拒否します。新しいRuntimeControlでも永続正本を参照し、旧runのprofile承認を再利用して起動しません。独立明示Ready+CLOSEDを受理する肯定gate、公開呼出元の接続、#54復旧・正規recoverと実OS/native本人受入は後続です。

### 独立明示ReadyとCLOSED終端の一致証拠

通常startは`RuntimeControl.execute`入口で`timeoutSeconds`からmonotonic期限を一度固定します。instance lockの競合は`LOCK_BUSY`で即時拒否します。事前確認、起動予約、OS生成直前、GRANT/ACKの記録と送信、起動proof、最終結果の記録と返却に同じ期限を適用します。予約前の期限超過は予約作成を拒否し、予約後の失敗では元予約と所有processを保全します。commit直後の期限超過では確定済み結果を保持し、呼出元への正常返却を拒否します。

2026-11-13のユーザー判断により、この期限の保証範囲はP側の受付・予約・OS adapter生成準備後・GRANT送信・BOUND/ACK確認・最終記録/返却です。GRANT後のMod内部のData beginRunは固定Modの既存期限を使用し、Pから見た作用後不明は元原文・予約・識別を保持します。

`RuntimeControl._offline_check_closed_ready(token, open_operation_id=..., ready=..., deadline=...)` は、呼出元が独立に固定した `worldUuid/runUuid/stateSha256/stateRevision` を、保持した元Cのclose応答と照合します。入力は検査前にsnapshotへ固定し、以後はそのsnapshotだけを検査・照合・返却に使います。worldUuidは元C取得要求との一致を要求します。Ready入力の省略や終端応答・旧承認からの自動補完は行いません。

完成済みの退避slotだけを専用照合へ渡します。終端SQL更新の失敗でresult/encoded未作成のslotが残った場合は、元object・原文・元予約を保全してPROOF_REQUIREDで拒否します。前後のread transactionでは、元C/全Wのobject・解放原文・生成binding・全RELEASED進捗、CLOSEDとoffline-open COMPLETEDの正本、予約不在、元lease/世代/run/閉門/停止証拠を再照合します。終了済みC/Wの再観測・再close・旧PENDING callback・局所記録の追加は行いません。失敗は元object・原文・正本を保全します。新しい照合期限を返却まで適用し、完了済みcloseの旧期限は変更しません。

`OFFLINE_CLOSED_READY_VERIFIED` はclose時Readyと終端証拠の一致だけを表します。Dataの現在のReady、chain/receiptの妥当性、Mod startを許可するものではありません。Data原本の確認から起動予約・OS生成直前・ACK・最終記録/返却までの継続照合と公開入力の接続は後続です。この照合に成功しても、第33スライスの既存start遮断を維持します。保持証拠のない新しいRuntimeControlでは、証拠の自動再構築をせず拒否します。

`RuntimeControl._offline_read_ready_original(token, open_operation_id=..., ready=..., deadline=...)` は、独立Readyと元終端へ一致する固定generationの`world-id`・`current.json`・不変state原文を読み返します。戻り値`OFFLINE_READY_ORIGINAL_MATCHED`の`stateJson`は元UTF-8原文です。返却前に原文と元C/W・CLOSED・終端・予約不在を再照合します。chain/receiptの公式検証と新しいstart予約以降の接続は後続であり、この読取結果をcached admissionとして使用しません。

### 第45スライス: 新runのdescriptor準備

`RuntimeControl._offline_prepare_start(token, operation_id=..., read_operation_id=..., open_operation_id=..., ready=..., deadline=...)` は、独立Ready・公式読取・元終端から同じ新run/start/全portsを予約し、run記録に続けて`ManagedRuntime.prepare`を実行する内部入口です。旧`_start`の再採番・ports再取得は行いません。`OFFLINE_START_PREPARED`と`(準備済みsession, 固定callback)`を返します。callbackは元run保存原文を返し、元helper完成条件・承認・予約・descriptor原文/資源を継続照合します。可変slotからsessionや比較基準を読み直しません。

同じ入口期限を承認再読後・P本人確認後・descriptor実体確認後・listener生成前・準備後と最終返却まで適用します。実際に作成したdescriptorのpinで同じ原文を再確認した後、短いRegistry write内の純比較で元CLOSED/0を再照合し、同じdescriptor hashを持つCLOSED/1へ遷移します。正常renewによるexpires変更は許容し、期限を延長しません。helper確定時に原文・session/endpoint/pin/listener/controller・handle/FDと承認を固定し、最終read退出後も保持objectと値を純比較します。

実launch展開結果の`auxiliaryFiles`は補助fileのbytesを含むため、JSON化せず原文bytesを含むsnapshotとして保持・比較します。後から文字列へ変換した値や、補助fileを省略した展開結果を基準にしません。

helperが最初に取得した入力と生成物も、元start意図の固定値へ結びます。純memory判定で元profile hash・承認hash/同一object、descriptorのroot/lease/epoch/generation/artifact/manifest・world/shop・run/nonce、launchの全項目を元run/固定generationと比較し、admission write内から最終read退出後まで同じ判定を再利用します。入力が後から元へ戻っていても、異なる入力から作られた生成物を採用しません。

失敗時は元reader/原文・新run/admission・start/ports予約・準備済み資源を保全し、再生成・再送・自動recoverを行いません。prepare途中のnative資源は既存transportのclose/未解放保持規則に従います。通常`_offline_record_start`はCLOSED/0と元callbackを返すままで、通常startのprepare/GRANT処理も従来の経路です。

この入口はMを生成せず、GRANTも送信しません。M生成は次の内部入口で同じsession/callbackを引き継ぎます。`_Startup.start`のdescriptor未結束前提は、このCLOSED/1証拠を明示的に引き継ぐ経路へ接続する必要があります。準備結果を既存handshakeへそのまま流用したり、既存の未結束前提を緩めたりしません。実OS/nativeでの受入は未確認です。

### 準備済みdescriptorからのM生成

`RuntimeControl._offline_create_start(token, operation_id=..., read_operation_id=..., open_operation_id=..., ready=..., deadline=...)` は同じ予約・run記録・descriptor準備に続けて、共通`_create`へ準備済みsessionと固定callbackを渡します。prepareの再実行・再採番・ports再取得は行いません。`OFFLINE_START_CREATED`と`(session, process, 固定callback)`を返し、callbackは現在の生成記録原文を返します。呼出元はこの組を直接引き継ぎ、可変slotから基準を採用し直しません。

元準備runと原文を保持したまま、別に保持する生成中runのcommand・creation・本人識別を、元保存原文をexpectedとする`_record`で更新します。同じ短いwrite内で元Ready/予約/承認の純比較、元又は更新後のrun全列とCLOSED/1を照合します。command構築後からOS adapter準備後/生成直前まで同じ期限とcallbackを使い、同じpinのdescriptor bytes確認後にもRegistryを再照合します。正常renewは許容して期限を延長しません。

commandは構築時、生成資源はcreated時、本人識別はnative観測時から固定します。生成完了時に同じprocess・handle/job・group・識別object/原文とcommandを一度固定し、返却値に純比較callbackを結束します。最終copy・read退出後・context退出後も元helper完成条件/descriptor証拠と合成して比較し、再pollや旧PID再解決は行いません。例外時は元session/process・生成中原文・start/ports予約を保全し、自動kill・解放・再準備・再生成は行いません。

prepareの純比較callbackは、同じ完成snapshotから元pin/descriptor bytes・argv・補助file bytes・環境/cwdを固定した組も返します。生成入口はこの組を直接受け継ぎ、可変sessionから基準を取り直しません。生成commandを固定argvへ照合し、実使用argfileの固定pathから補助fileを各65532byte上限のbounded読取で再照合します。環境は固定項目から作った元dictを渡します。全読取後にもRegistryを再照合し、生成直前の最後のhash/環境比較後と、最終返却前の最後の純比較後にも同じ期限を確認します。

生成結果は起動成功ではなく、instanceはSTARTING/PENDING、admissionはCLOSED/1のままです。GRANT/BOUND/ACK・proof・RUNNING確定と公開呼出元は後続です。Pの期限保証はP側に限定し、GRANT後Mod内部の作用は既存期限へ委ねます。実OS/native受入と実使用時のartifact固定は未確認です。

### CLOSED/1を継承するstartup受入

`RuntimeControl._offline_prepare_handshake(token, operation_id=..., read_operation_id=..., open_operation_id=..., ready=..., deadline=...)` は同じ内部生成経路から、M本人のcontextとstartup枠の準備まで接続します。`OFFLINE_STARTUP_PREPARED`と`(session, process, startup_session, 固定callback)`を返します。callbackは元保存run原文を返し、入口の同一期限、元helper完成条件、descriptorとM資源、新run/start/ports正本、startupの保持object・原文・checkpointを照合します。

通常startと共通の準備処理を使い、短いRegistry write内で元CLOSED/1・同じdescriptor hashを明示的に要求します。通常startは初期CLOSED/0から1へ進みます。受入結果はSTARTING/PENDING・CLOSED/1で、元session/endpoint・startup枠・要求応答原文と予約を保持します。正常renewは現在のtoken有効性で確認し、入口期限を継続します。

今回は接続交換に先立つ受入までを独立レビュー単位とします。acceptで変化するconnection/listenerの正当な状態遷移とGRANT/BOUND/ACK、最終RUNNING確定、公開入力は残作業です。実OS/nativeと実Mod/Dataの本人受入は未確認です。

### 同じstartup枠からのaccept

`RuntimeControl._offline_accept_start(token, operation_id=..., read_operation_id=..., open_operation_id=..., ready=..., deadline=...)`は同じ内部生成・受入から、一度だけacceptしてM本人を認証します。`OFFLINE_START_ACCEPTED`と`(session, process, startup_session, 固定callback)`を返します。callbackは元run保存原文を返します。

元準備callbackは、保持したdescriptor・native・listener・M生成証拠を照合し続けます。acceptで新規取得するconnection・peer・channel・I/Oを取得順に固定し、Windows listener.activeのFalse→True、Mac channel.streamの取得、I/O正常解放を明示的な遷移として扱います。完成基準はhelper内部で固定して返却値と一体で渡し、外側は同じ保持object・値を最終read退出後にも比較します。

artifact再読後、peer取得・open後、channel準備後、I/O準備後・発行直前・正常解放前後、認証・最終返却へ同じcallbackと期限を渡します。Windowsはevent準備後、Macは各nonblocking syscall直前にも確認します。正常renewは現在のtoken有効性で確認します。

observe指定のhandoff失敗時はconnection.handoff_failedを保持し、取得済みconnection・peer pin・channelと識別を元endpointに残します。終端済みI/Oのevent等は既存規則で正常解放します。失敗後のreapは未終端I/Oを再pollせず、接続の切断・本人pin解放へ進みません。closeも未完了として返し、元枠・原文・start/ports予約と再accept拒否を継続します。通常startのobserve未指定経路は既存cleanupへ進みます。

今回の完了範囲は認証済み接続の確定までです。instance STARTING/PENDING・admission CLOSED/1を保持し、GRANT/BOUND/ACK・RUNNING確定・公開入力を後続へ渡します。Pの期限保証とGRANT後Mod内部の期限は確定済み判断を継承します。実OS/native・実Mod/Data本人受入は未確認です。

### 同じ認証済み接続でのHELLO/GRANT

`RuntimeControl._offline_grant_start(token, operation_id=..., read_operation_id=..., open_operation_id=..., ready=..., deadline=...)`は同じ内部経路で一度だけacceptし、HELLO受信・公式wire3照合・GRANT原文の保存と送信まで進めます。`OFFLINE_START_GRANTED`、HELLO/GRANT body hashと`(session, process, startup_session, 固定callback)`を返します。callbackは元run保存原文を返します。prepare・M生成・ports予約・acceptを再実行する経路は追加しません。

元acceptの固定callbackに、各read/writeのpending取得と正常解放だけを明示的に合成します。同じnative/channel・OVERLAPPED・event/buffer・要求値・受信原文・正常終端を保持し、実送信bufferも元GRANT bytesへ照合します。原文・I/O資源・完成基準はhelper内で固定して返却packetへ結び、外側の最終gateまで同じ純比較を継承します。可変slotから基準を採用し直しません。

各I/Oの準備後・実発行直前、応答・正常解放前後、短いGRANT保存writeの前後、送信直前・最終read退出後と返却直前へ同じcallbackと入口期限を渡します。保存済みHELLO/GRANT原文は現在正本へ再照合し、正常renewでも期限を延長しません。途中失敗・部分送信・解放不明では自動再送・再accept・接続切断・本人pin解放を行わず、元connection/peer/channel・I/O進捗・原文・start/ports予約を保持します。終端済みI/Oの正常解放は接続の切断と区別します。

結果はSTARTING/PENDING・CLOSED/1のままで、BOUND/ACK・RUNNING確定・公開入力・正規recoverは後続です。GRANT後のMod内部Data beginRunはMod既存期限へ委ね、P側の不明は作用後不明として保全します。固定Mod/Data契約・artifactは変更しません。実OS/native・実Mod/Data本人受入は未確認です。

### 認証済みBOUNDとACK

`RuntimeControl._offline_acknowledge_start(token, operation_id=..., read_operation_id=..., open_operation_id=..., ready=..., deadline=...)`は同じ内部経路をBOUND受信・ACK原文保存/送信まで進めます。`OFFLINE_START_ACKNOWLEDGED`、元HELLO/GRANTとBOUND/ACKのbody hash、および`(session, process, startup_session, 固定callback)`を返します。callbackは元run保存原文を返し、元資源・原文・I/O完成証拠と今回のstart/ports・run/admissionを継続照合します。

GRANT送信直前までcurrentは元Ready原文との完全一致を要求します。GRANTの発行段階を元packetと一体の固定値へ結び、BOUND取得中も元Ready immutable・公式読取・終端正本と同一期限を照合します。GRANT後に初めて読んだ非Ready原文はbytes/hashと一緒に一度保持し、前Ready hash、revisionの+1、新Data run UUID、world/generation/path、M本人/managementと不変state原文を直ちに検査します。不正な候補は保持したまま停止し、currentを修復しても別の候補へ採用し直しません。認証済みMの公式Data statusを含むBOUNDを公式wire3で照合し、保持候補とそのstatusのhash・内容を結びます。currentは元Ready原文または保持したRunning原文へ限定し、同じ候補とBOUNDが一致してからACKを保存・送信します。

この段階別照合は2026-11-16の判断によります。Issue #64のM本人によるbeginRun/finishSavesと、固定DataModel deb13317のRunning耐久公開を前提とします。GRANT後のMod内部作用はMod既存期限へ委ね、P側は受付・I/O準備後/発行直前・応答・正常解放・短い保存write・最終返却を入口の単一期限で確認します。

BOUND・ACKのpending/event/bufferと正常終端/解放を元接続証拠へ順に合成します。失敗・部分送信・読取不能・相関不一致は元connection/peer/channel・pending進捗・原文・識別・予約を保持します。結果はSTARTING/PENDING・CLOSED/1で、ACK/proofと正本の最終RUNNING記録・公開入力・正規recoverは後続です。実Mod/Dataと実OS/nativeの本人受入は未確認です。


## ACKと起動proofの正本記録

内部入口`RuntimeControl._offline_record_start_proof`は、同じ起動経路のACK完成packetから未公開又はINITIALIZINGのproofを同じ入口期限で待ち、STARTEDの実読bytesを固定して既存検証へ渡す。各観測で元ACK・新run/予約・元Ready/RunningとM生存・本人を照合する。読取は16MiB+1までに限定し、初回lstatでの未公開だけを待機できる。読取途中の欠落、破損・超過・不正phase・本人不一致は保全して停止する。

固定ModのINITIALIZINGはstartupが空で、一度作成した後にSTARTEDを原子的に置換公開する。INITIALIZINGも共通_proofのrun/nonce・責務・本人/runtime検証を通し、初回の原文bytes/hashを一度固定して保持する。同phaseの原文差替えと、公開済みINITIALIZINGの欠落は拒否する。この元tupleをSTARTEDのproof_packetへ一体で継承し、純比較・最終返却まで同じ参照を照合する。元Readyと認証済みBOUNDへ結んだRunning候補、全frame原文・I/O完成証拠、生成済みsession/processと単一期限を継承する。

pollから本人観測へ移る直前、proofのJSON解析から共通検証へ移る直前にも同じ入口期限を確認する。offline呼出しは共通_proofにも同じdeadlineを渡し、各直接のpath確認・resolve・設定原文読取の開始前に検査する。同期helper単位の処理は途中中断せず、戻った後の次の作用を遮断する。保存用serialization・返却copy後に入る元ACK継承callbackも入口で同じ期限を確認する。保存・最終gateで共用するownerとproof再読にも同じ検査を適用し、期限到達後に次の観測を始めない。通常経路は追加期限を指定せず、従来の検証・待機を維持する。

元生成runを保持したまま、元保存原文をexpectedとするCASでACK body hash・BOUND status原文/hash・起動proof原文/hashをrun正本へ記録する。write内では元run/admission・start/ports予約・元ledgerと保持object/値の純比較だけを行う。結果`OFFLINE_START_PROOF_RECORDED`と同じ組の固定callbackを一体で返す。正常renewは入口期限を延長せず許容する。

最終返却では継承callbackの承認・ファイル・native観測、proof原文・M生存・本人・listener観測、短いread transactionによる元run/admission・予約・ledger・GRANT/ACK原文の照合を先行する。退出後、元Readyで固定したserver.properties・world-id・Ready不変原文、段階別currentと固定Running不変原文、proof原文を再読し、M生存・本人を確認する。以後は継承memoryを含む保持値の純比較と同一期限の確認だけを行い、returnedを立てる。

この最終確認はP側の各観測時点を保証する。複数ファイルとprocessの観測は逐次であり、観測済み対象の以後の変更と、最終確認後から返却までの変更が残余窓として残る。最後の実読まとまりの後にはファイル・native・Registryの作用を置かない。失効を検出した場合はreturnedを立てず、元原文・資源・予約を保持する。

成功時もSTARTING/PENDING・CLOSED/1と全予約を保持する。OPEN/RUNNINGと予約終端・公開後監督接続は次の独立段階とする。保存前後の不明、期限超過、証拠変更、commit応答喪失では元資源・原文・識別・予約を保全し、自動解放・再生成・再送を行わない。


### 第53スライス: 同じproofからOPEN/RUNNINGを原子的に確定

内部入口`RuntimeControl._offline_finish_start`は、第52までの同じ準備・生成・HELLO/GRANT/BOUND/ACK・proof待機/保存経路を一度だけ通り、固定proof完成packetから新runのOPEN/revision 2・RUNNING/pending 0・start操作COMPLETED/結果原文と元artifact予約の解放を一つの短いRegistry writeで確定します。ports予約は通常startと同じく同じrunへ保持します。結果`OFFLINE_START_RUNNING`と同じ`(session, process, startup_session, 固定callback)`を返します。公開Ready入力・公開呼出元は後続です。

確定writeの前に通常handshakeと共通の処理で元sessionへcontext/tokenと元Mを登録し、既存listen/_serveを同じ認証済みconnection/peer/channelへ接続します。受信threadは入口終了通知を待つため、最終照合中に新しい受信I/Oで元完成証拠を変更しません。入口終了後は同じ起動確定正本を確認して受信を進めます。write失敗で元PENDING/CLOSEDなら受信せず保全し、commit応答喪失でも同じ確定が耐久保存済みなら既存監督を進めます。いずれも起動成功へ読み替えたり接続資源を自動解放したりしません。公開stop/status/refreshとSTOPPING proof監督は通常経路のsession・受信進行・doneを使用します。受信引継ぎ後の正当なpending/停止進行へ、起動完成時のcallbackを継続適用しません。

元runのACK/proof保存原文、生成資源・descriptor・本人・接続・全I/O原文、公式読取と段階別currentの固定基準を取り直しません。確定結果原文とOPEN正本bytesを元proofと一体で固定し、reserve_start・record_start・startupの三層へ渡して確定後の正本を照合します。旧STARTING/PENDING/CLOSED/1を確定後に再要求しません。write内ではファイル・native観測を行わず保持object/値と元正本を比較します。正常renewのexpiresを固定列から分け、現在のtoken有効性を確認します。

同じ入口deadlineを延長せず、確定結果の直列化/copy後・各更新前・commit後と最終返却へ継承します。失敗・commit応答喪失・作用後不明では元M・connection/peer/channel・reader・原文・識別と残る予約/終端正本を保全し、自動解放・再生成・再送・再確定しません。commit後の不明をPENDINGへ戻したりartifact予約を再取得したりしません。

最終gateは継承する外部/native観測・承認読取・Registry照合の後、最後に同じcurrent/proof・不変原文の実読とM生存/本人確認をまとめ、以後は保持値の純比較と期限確認だけで返却します。保証はP側の観測時点に限り、最後の実読まとまり後の外部変更は検出できない残余窓です。以後の不変は保証しません。GRANT後のMod内部作用の期限は既存Mod契約へ委ねます。固定Mod/Data契約・artifactと通常startの0→1は変更しません。

### 第54スライス: 独立Readyからの公開offline-start

`Registry.execute('offline-start', payload, operation_id=..., dry_run=False)`と既存JSON CLIの`offline-start`を接続します。同じPが保持する元offline終端・C/W/readerの原文と資源が前提です。P再起動で保持証拠を失った状態を再構成したり、新Cへ引き継いだりしません。通常`start`は従来のpayloadとoffline終端後の遮断を維持し、旧run承認だけでは通しません。

payloadの必須項目は`instanceId/leaseId/epoch/expectedGeneration/timeoutSeconds/openOperationId/readOperationId/ready`です。`ready`は運用者が独立して指定する`worldUuid/runUuid/stateSha256/stateRevision`の四項目だけを受け付けます。終端応答・CLOSED記録・旧run承認から期待Readyを作りません。二つのUUIDとSHA-256の形式、revisionの整数型（bool/float不可）と0以上のsigned 64bit範囲、必須・未知項目を検査します。元open、新読取、今回startの三operation IDは別にしてください。任意path、実行command、allowflagは受け付けません。

CLIでは`--operation-id`を必須とし、本文は`--payload-stdin`又は`--payload-file`で渡せます。ファイル・標準入力は実際のstreamからIPC上限+1 bytesまでだけ読み、実読bytesの上限を検査します。例えば`dev-server.py offline-start --root <絶対root> --operation-id <新start ID> --payload-stdin --timeout-seconds 35`です。公開RPCとSSHの既存CLI転送も同じpayloadを使用します。固定Mod/Dataと他repoのManager契約は変更しません。

`timeoutSeconds`はPの公開Registry受付で採ったmonotonic起点から一度だけ作る起動期限です。入力snapshotの固定・検証、resource_use/GC照合、起動予約、OS adapter準備後/生成直前、GRANT/BOUND/ACK、proof、確定writeと公開返却まで同じ期限を渡します。CLIの`--timeout-seconds`は既存のIPC応答待機であり、起動期限を延長したり、応答喪失後に自動再送/killしたりしません。GRANT後Mod内部作用はModの既存期限へ委ねます。

公式読取自体がhelper生成を必要とするため、この明示操作の`dryRun=True`は作用前に拒否します。構造検査だけで起動可能とは返しません。通常`start`のdry-runと再提示は従来どおりです。`offline-start`は内部の一度限りの受付に合わせ、同じoperationの再実行も拒否します。新IDによる自動再送・元枠の破棄・再予約をしないでください。Pメモリ上の公開要求枠は原入力/完成packet/返却進捗の保全用で、start/portsの永続予約を代用しません。

内部返却と一体の固定callback・元result objectを公開最終gateで再照合します。受信開始通知はこのgateまで公開側が保持し、正常完了/例外のfinallyで引き継ぎます。これにより、同じ認証済みconnectionの受信が起動完成基準を途中で変更しません。最後の実読まとまり後は公開入力・packetの純比較と期限確認だけを行い、返却進捗を立てます。保証はPの観測時点だけであり、以後の変更を保証しません。

公開起動contextはRegistry.execute外側のresource_useより後に退出します。資源調停の入場後、GC読取前にも同じ入口deadlineを確認します。起動contextは元結果を一度yieldし、counter解放とdb_lock待機を含む資源調停の退出完了後、元完成callbackのmemory_only・公開入力/packet・同一期限を純比較してから公開returned=Trueを確定します。入場中の期限到達はGC/起動作用を開始せず、退出中の期限到達はSTART_TIMEOUTで公開成功を拒否します。commit済みのRUNNING/COMPLETED・全ports・元M/接続は保全し、finallyの通知で既存受信へ引き継ぎます。db_lock待機を強制中断したり、期限を再設定したりはしません。

失敗・作用後不明では元入力canonical・元M/connection/peer/channel・reader・原文・残る予約/終端正本を保全します。内部完成と公開返却を別の進捗として記録し、内部RUNNING確定後に公開gateで失効しても成功応答へ進みません。commit済みのMには第53と同じ監督/受信を引き継ぎ、再生成・再handshake・GRANT/ACK再送・自動解放・再確定はしません。既存の公開status/stop/STOPPING監督は同じsessionと受信進行を消費します。起動の正規recoverと同一P/instanceの保持枠の再利用は後続とし、保全規則を緩めて再受付しません。

## 確定済みcloseの内部recover照合（第55スライス）

`RuntimeControl._offline_recover_close`は、同じPが保持する元offline-open/close IDと終端原文のSHA-256を明示した、新しい照合operation専用です。元closeの終端write前に固定した原文・保持object・解放進捗・生成bindingのpacketを使用し、CLOSED/COMPLETED・元予約不在の正本とP本人を再照合して、`OFFLINE_CLOSE_RECOVERED`を記録・返却します。元closeのcommit後に返却が失敗した場合も、正本と同じ固定packetが一致する場合だけreadbackできます。

今回のdeadlineは照合要求の単一期限であり、元close期限の更新や未完了処理の継続には使いません。Registry write内ではSQLと保持値の純比較だけを行い、write退出・返却copy・外側resource_use退出後にも固定値と期限を確認します。期限や照合の失敗では元正本・元資源・記録済みの復旧結果を保全します。解放済みprocessの再poll/再close、close再送、Cの差替え、保持枠の破棄は行いません。

終端前の失敗、部分解放UNKNOWN、元packetのないP再起動、別run/世代は拒否します。この入口は旧generationの正常化・Ready/start許可を返しません。公開recoverへの接続、未確定の正規解決、保持枠再利用と#54の検証snapshotから新generationへのrestoreは後続です。現在の公開offline-startに再試行は追加しません。

## 未commitのclose終端を確定する内部recover（第56スライス）

`RuntimeControl._offline_recover_pending_close(token, operation_id=..., open_operation_id=..., close_operation_id=..., termination_sha256=..., deadline=...)`は、同じPが元終端write直前に固定したpacketと全C/W正常終了・解放原本を保持する場合に、元offline-openの終端を明示要求で確定します。元open/closeとは別の照合IDと元終端原文hashを指定します。結果は`OFFLINE_CLOSE_TERMINATED`と元終端結果です。今回の単一期限を使い、元close期限と終了・解放進捗は維持します。

元終端意図を保存した原文は、元packetのcallbackへ一度固定して引き継ぎます。元offline-openのPENDING原文、元lease/epoch/generation/run/artifact、元run/proof/gate/writer台帳と元artifact予約を短いRegistry write内で照合し、元offline-openと今回照合のCOMPLETED、元予約終端とpending解除を同一transactionで確定します。正常renewでは現在のtoken有効性を確認し、入口期限は延長しません。

元writeのrollbackでactive mapが既に外れた場合も、retiredに保持した同じobjectを使用します。別objectへ再利用された枠、変更された原文・資源・進捗・本人bindingは保全して拒否します。元C/Wへの再poll・再close、資源の追加解放、Data API再送、元Cの再生成は行いません。元packetがない場合、部分解放UNKNOWN、P再起動は引き続き拒否します。

元latest workerの参照は元slot作成時に固定し、packetのcopy後に可変slotから基準を取り直しません。slotとactive mapを揃えて差し替えた場合も、元参照との不一致で終端writeを開始せず保全します。

今回の照合がcommit済みなら、同じ入力・同じ照合IDでその記録をreadbackできます。別の新照合IDで既にCOMPLETEDの元操作を未確定として扱う要求は拒否します。既知COMPLETEDの照合には第55の入口を使います。write前の失効はPENDINGと予約を維持し、commit後の失効は確定済み終端と今回の記録を維持して正常返却を拒否します。

P本人観測の後に短いSQL照合・更新を行い、write退出後、返却copy後、資源調停退出後は同じpacketと返却値の純比較・期限確認だけを行います。新しいfile・native・SQL作用を最終純比較後に置きません。保証はP側の観測時点に限られ、各観測・比較後の外部変更は残余窓として残ります。既存起動の最終gateと監督・受信接続を維持します。公開recoverへの接続、部分解放・作用後不明の追加復旧、保持枠の再利用と#54 restoreは後続です。実OS/native本人受入は未確認です。

## 局所記録の正常復帰後からの明示recover（第57スライス）

解放snapshotは、最初のwriter CLOSED照合後の元slot作成時に参照と値を可変slotから独立して保持します。`_record()`前の通常照合、正常復帰後のpacket作成前、以後の保持callbackは、この同じ基準へ純比較します。解放進捗や局所recordとslot基準を同時に変更しても、新しい完成基準には採用しません。追加の記録I/Oは行いません。

packetの元枠・C/全W・history・closeBinding・資源参照、handle/job/thread・Popen・pipe全FD/buffer、native/adapter/本人callbackと補助値も、`_record()`前に参照・値を固定します。記録後に初めて資源の基準を採用する処理はありません。記録番号は事前値からCだけが1進み、全Wは変わらないことを要求し、terminationは同じ固定結果へ照合します。記録中に変わった資源はそのまま保持して拒否し、再closeしません。

`_offline_finish_close`は、終端局所記録の`_record()`が正常復帰した直後、次のowner callback・Registry照合より先に事前固定した基準を同じ完成packetへ結びます。元終端意図の保存原文から終端目標原文を固定し、元C/全W・終了/解放原文・資源・本人・artifact・期限の同じ参照と値を保持します。後続照合の失敗・期限到達で元終端がPENDINGのままでも、このpacketを同じPが保持していれば、第56の`_offline_recover_pending_close`で明示的に正本終端へ進めます。

`_record()`の正常復帰は、新規ファイルへの原文write・flush/fsync・権限設定と記録番号の更新が完了したことを表します。元局所記録・close・資源解放を再実行せず、元意図と新しい照合要求を既存の短いRegistry writeで結びます。元終端目標はwrite内でも同じ原文へ照合し、slotや現在正本から完成基準を採用し直しません。終端前の枠比較をworker参照より先に行い、別objectの枠は保持して拒否します。

結果エンコード後のverify開始前、owner callback復帰後の次Registry読取前にも元入口deadlineを確認します。局所記録からの復帰時点で期限に達した場合は純メモリの証拠固定だけを行い、後続のI/O・終端write・正常返却へ進みません。元deadlineは保持したまま、新しい明示recover要求の期限で元packetと正本を照合します。

局所writeが例外・部分公開となって正常復帰を確認できない場合は、ファイルの存在だけで完成packetを作りません。第58では、次の実write完了通知を保持できた場合だけを追加で扱います。部分解放UNKNOWN、P再起動、保持bindingや元正本の不一致は保全して拒否します。保持枠再利用、公開recoverと#54 restoreは後続です。正常起動・第55/56のrecover・固定Mod/Data契約は従来の入口を使います。実OS/native本人受入は未確認です。

## 実write完了後のpacket引渡し失敗（第58スライス）

終端局所記録の作用前に、元意図・目標原文・局所記録のcanonical bytesと全保持群・完成packet・照合callbackを固定します。任意のファイル再読や可変slotから完成基準を取り直しません。共有writerは同じbytesのwrite・flush/fsync・file close・権限設定がすべて正常完了した後だけ、その実write bytesをP内の完了observerへ渡します。C記録番号+1後、事前原文と一致した通知をlexical状態へ保持し、同じpacketを引き渡します。既定のwriter呼出しの戻り値とI/O順序は維持します。

`RuntimeControl._offline_recover_recorded_close`は第56と同じ引数で、同じPがこの完了通知と作用前の準備packetを保持する場合だけ、その同じpacketの引渡しを明示的に照合します。正常復帰直後の例外やpacket引渡し失敗を成功へ読み替えません。元close/局所記録/解放を再実行せず、第56の正本照合と元意図CASへ接続し、同じwriteで`OFFLINE_CLOSE_TERMINATED`を記録します。新しい照合operationを元open/closeと区別してください。既知COMPLETEDには第55、完成packetを保持する未commit終端には第56も引き続き使用できます。

write・flush/fsync・close・権限設定の例外で完了通知がない場合は、ファイル全bytesや記録番号が一致しても完了とは扱いません。実write bytesの不一致、準備packetや資源の変更、部分解放UNKNOWN、P再起動も保全して拒否します。引渡し途中の失敗後も元準備packet・原文・識別・資源・予約を保持し、自動解放・再close・旧handle操作・追加の記録I/Oは行いません。

今回の単一期限は準備packetの引渡し前後・資源調停退出後と第56の正本照合から返却まで継承します。元close期限は変更しません。保証はPが観測した正常I/O完了であり、電源断時の永続性や観測後の外部不変を保証しません。通知前の部分公開、P再起動、保持枠再利用と公開recoverは後続です。ゲームDBの種類や保存契約への前提は追加しません。

## 最初Wのhandle解放不明の内部観測（第59スライス）

`RuntimeControl._offline_observe_worker_release_failure(token, operation_id=..., open_operation_id=..., release_operation_id=..., deadline=...)`は、同じPでCの正常解放が正本記録済み、最初Wのhandle解放が例外となった場合だけを照合記録します。元open/解放とは別の観測IDを指定します。`OFFLINE_RELEASE_UNKNOWN`の`source`は元Cの正常解放hashと全Wの資源別進捗を区別し、`terminatesOriginal=False`を返します。新しい観測操作のCOMPLETEDは観測記録だけで、元openはPENDING・pending1・元予約保持のままです。CLOSED・start許可・正常解放への変更を意味しません。

全保持群・資源値・元意図原文・固定row・期待進捗とpacket/callbackを、W解放作用前に一度固定します。最初Wのclose例外だけをlexicalな発生証拠として保持し、UNKNOWNを可変slotから正常解放へ読み替えません。元process/pipe/creation_resources、native/adapter/本人callback、全handle/job/thread/FD/buffer、C/W生成・終了/解放原文とhistory/latestを同じ基準へ比較します。P再起動、pipe段階、2件目以降W、未発生の例外、別object/原文/進捗は保全して拒否します。

今回の単一期限で、P本人観測をwrite前に行います。write内は既存_reservedと元意図原文・固定row・保持値の純比較、新しい観測INSERTだけです。正常renewのexpiresは固定値から除き、現在の_tokenで有効性を確認します。最終readと資源調停退出後は純比較・期限確認だけで返却し、その後の外部変更は検出できない残余窓です。記録前の失効では観測をcommitせず、commit後の失効では観測正本と元資源・原文・予約を維持して正常返却を拒否します。

旧PID/group/handleの再解決・再poll/再close、pipe追加解放、元Data API再送・局所記録I/Oは行いません。局所write再同期の選択肢1（明示recoverの新しい同期完了を元write成功と区別した別証拠へ結ぶ）は2026-11-20にユーザーが許可済みです。次スライスで実装し、本スライスには含めません。本スライスでは同期作用やpacket完成扱いを追加せず、第58の保全/拒否を維持します。部分解放の残りの段階・P再起動の追加復旧、保持枠再利用・公開recover・#54 snapshot/restoreは後続です。固定Mod/Data契約とゲームDBへの前提は変更しません。

## 元close済み局所記録の明示同期（第60スライス）

2026-11-20に許可された再同期の最小ケースとして、`RuntimeControl._offline_recover_synchronized_close(token, operation_id=..., open_operation_id=..., close_operation_id=..., termination_sha256=..., deadline=...)`を追加しました。第59スライスの範囲外だった再同期の一部を本スライスで実装しています。同じPが元write・flush/fsync・元file closeの正常完了を観測し、権限設定の例外で完了通知を失った場合が対象です。部分write・元fsync/close不明・P再起動は保全して拒否します。

元C/全Wの保持群・終端意図/期待record bytes・packet/callbackは_record前から保持し、新しい局所fileは元作成直後・実write前に元file objectからpinを取得します。元token/open/close/hashの要求canonicalも_record前に固定し、今回の要求を本人観測前から最終gateまで同じ基準へ比較します。元実体のdevice/file IDとpin object/FDを固定し、再openしたファイルを基準に採用しません。明示recoverでは保持pinから期待bytes長+1まで読み、全bytes一致・同じpath実体・独立通常file・現在の所有者限定権限と元Registry正本を照合した後だけ新しいflush/fsync（macOSは完全同期も）を行います。上書き・不足追記・権限の再設定は行いません。

元_recordも同じframe専用lockを取得し、取得後に元期限を確認してから元write・権限設定・番号進行・written通知までを実行します。file close済みでも元writerが進行中なら、明示recoverはRECOVERY_REQUIREDとなり、同期・pin操作・終端DMLを開始しません。元writerが正常完了した場合は元の通知と通常終端へ進み、元pinを正常解放します。例外終了した場合だけlock解放後に既存の元close証拠・固定番号・原文を照合でき、条件を満たす元権限設定例外を新しい同期へ結べます。recorded=Falseだけで進行中の元writerを失敗扱いにしません。

新しい同期は並行要求を含めて一度だけです。元write前に固定したframe専用lockを、明示recoverの最初の照合からpinのseek/read・同期・終端CAS・最終読取まで保持します。同じframeへの競合要求は、同一ID・異なるIDとも待たずにRECOVERY_REQUIREDで保全拒否し、新しい同期やpin操作へ進みません。file I/Oを囲む長いRegistry writeは追加しません。失敗・期限到達・結果不明時はpinと元資源/原文/予約を保持し、自動再同期・元記録の再書込・C/W再close・Data再送をしません。同期が正常完了した場合だけ元記録番号を一段進め、`recordSynchronization`に今回のID・元record hash・元実体ID・`originalWriteCompleted=False`と`synchronizationCompleted=True`を固定します。元write成功のflagは立てず、第58の実write完了recoverへ読み替えません。

同じ元packet/callbackと新しい同期証拠を短いRegistry writeの純比較へ合成し、元offline-openのCOMPLETED・元予約終端・pending解除と新しい`offline-recover-synchronized-close/COMPLETED`を同一writeで確定して`OFFLINE_CLOSE_SYNCHRONIZED`を返します。正常renewでexpiresが変わっても元期限・今回期限を延長しません。元の正常write完了時は今回取得したpinを期限内に正常解放し、失敗時又は明示同期後のpinは同じPの証拠として保持します。

最後のRegistry/native観測と資源調停退出の後、同じpinの原文/実体/権限を最後に照合し、以後は保持値の純比較と同一期限だけを確認して返します。権限はrequire_privateのfd指定を使い、Windowsは同じ保持HANDLEの既存GetSecurityInfo、macOSは同じFDのfstatで確認します。パスの権限だけを別実体から採用しません。fd未指定の既存呼出しは従来のパス検査を維持します。最後の実読後の変更は検出できない残余窓です。commit後に不一致・期限到達した場合も、正当な終端正本と元証拠・pinを保全して正常返却を拒否します。電源断耐久性、残る失敗段階・P再起動・公開recover・#54 restoreは未確認又は後続です。通常start・固定Mod/Data・ゲームDBの契約は変更しません。


### 元fsync不明の明示同期（第61スライス）

第60スライスの権限設定例外に加え、元write・flushと元file closeの正常復帰を同じwriterで肯定観測でき、元fsyncだけが不明の場合を同じ内部recoverへ接続する。元作成時のpin・期待bytes・同一実体・権限・全保持bindingを再照合し、新しい同期完了を元write成功とは別の証拠として記録する。

元writerと並行recoverは同じframe lockで排他し、新しい同期は並行要求を含め一度だけ開始する。元write/flush又はcloseの完了通知がない場合、全bytesやclosed属性だけでは受理しない。上書き・追記・再close・Data再送は行わず、不明時は元資源・原文・予約を保持する。通常writerの保存・通知順と既存公開start/stopは維持する。


### 元flush不明と元buffered closeの肯定証拠（第62スライス）

第61の対象を、元write通知は正常だが明示flushが例外となり、同じbuffered streamの元closeが正常復帰した場合へ拡張する。元exitは作用前の同じ参照を使い、開始前に未closeであることとbufferのflushを含むcloseの正常復帰を観測する。全bytes・closed属性・FD番号だけから完成を推測しない。元write不明、事前closed、close例外又は肯定通知がない場合は保全拒否する。

このclosed通知は明示flush/fsyncや元write全体の成功を意味しない。元writerが終了した後だけ、同じ元pin・期待bytes・実体・権限と全保持bindingを既存の明示recoverへ結び、新しい同期完了を別証拠として扱う。元writer/並行recoverのframe排他と同期一度だけを維持する。追加close・再close・元記録の上書き/不足追記・権限修復・Data再送は行わない。同期不明は原本・予約・資源を保持し自動再同期しない。

### 新同期正常復帰後の照合失敗（第63スライス）

新しい同期の証拠原文は、今回のrecover IDと元record hash・元実体IDへ同期作用前に固定する。flush・fsync（macOSは完全同期も）がすべて正常復帰した時点で、次の照合I/Oより前に同じframeへ保持し、C記録番号を一度だけ進める。後続の権限/原文照合の例外や期限到達は正常返却せず、元openはPENDING・元予約とpin/全保持資源を保全する。

完了進捗を立てる直前にも既存の全保持群の純比較を行い、同期中に差し替わったpacketを上書きして正常基準へ戻さない。不一致は変更後のobjectも保全し、番号進行・終端writeへ進めない。

この正常復帰を保持する同じP・同じrecover IDからの明示照合は、新しい同期を繰り返さず、元pinの原文・実体・権限と全保持binding、元意図/正本を再照合して既存の短い終端CASへ進める。別IDは拒否し、元write成功へ読み替えない。今回要求の入口期限は延長せず、別の明示照合要求はその入口で受け取った期限を使う。同期作用自体の例外・正常復帰を保持できない場合は、ファイル全bytesや番号だけで補わず保全拒否し、自動再同期しない。

元writerと並行recoverは従来と同じframe lockで排他し、cached証拠を使う場合も終端write前の実体照合を省かない。資源調停退出後の最後の実読まとまりと、その後の純比較/期限確認を維持する。最後の実読後の外部変更は残余窓であり、以後の不変や電源断耐久性は保証しない。元write/通知・元closeの残る不明段階、部分解放UNKNOWN・P再起動・公開recover・#54 snapshot/restoreは対象外とする。

### 最初Wの元process close完了の独立観測（第64スライス）

最初のWの元process closeで、Windowsの元process HANDLEとjobのclose、又はmacOSの元Popenのwaitがすべて正常復帰した事実を、その元呼出し内で保持する。保持する原文・資源参照・期待値は作用前に一度固定し、完了後の値や可変slotから基準を取り直さない。各native作用の直前には同じ元期限と元正本を再照合する。native作用自体の例外や完了通知の喪失は、番号・全bytes・終了PIDだけで補わない。

元closeの正常復帰観測を保持した後、外側の返却を失って第59のhandle UNKNOWNとなった同じPは、内部入口`RuntimeControl._offline_observe_worker_close_completion`から新しい照合IDを記録できる。元packetの固定callback、同じ資源と正常復帰観測、元意図・有効token・固定rowを短いRegistry write内でも純比較し、`offline-observe-worker-close/COMPLETED`と`OFFLINE_WORKER_CLOSE_OBSERVED`を返す。正常renewのexpiresだけは固定列に含めず、今回の入口期限は延長しない。

この結果はprocess資源群の正常close観測であり、元操作の成功、pipeの解放、全WのRELEASED又は元予約終端を意味しない。元offline-open PENDING・pending1・予約1、handle UNKNOWN・pipe NOT_STARTED、元C/W・原文・識別を保持する。同じ照合IDのreadbackは再closeせず、元解放操作の再実行も拒否する。失敗・期限到達・不一致では追加解放せず保全する。P再起動・真のnative close不明・残る解放段階と公開recoverは後続とし、通常start/stop、固定Mod/DataとゲームDBの契約は変更しない。最後の外部/Registry観測の後は保持値の純比較と期限確認のみで返し、観測以後の外部不変は保証しない。

### 元pipe生成時の所有pin（第65スライス）

新しいProxyPipeは、元os.pipeの4端点を外部へ渡す前に非継承duplicateを取得し、元端点と同じ所有枠へ固定する。WindowsはCompareObjectHandles、POSIXは保持中のFIFO実体とアクセス方向を照合する。照合API不在・実体情報不足・pinのない保持枠は、FD番号だけで代替せず保全拒否する。Windowsの当該照合にはWindows 10 / Server 2016以降のAPIが必要であり、他の既存機能のOS要件は変更しない。

同じpipeの所有照合・通常closeは専用lockで排他する。child_input・child_output・input・outputの順に、各元FDのclose正常復帰後にそのpinをcloseする。child側pinもclose_child_endsで解放し、追加pinによるEOF遅延を残さない。close例外・正常復帰喪失・途中の照合失敗や期限到達では、元識別とpin・UNKNOWN又はPIN_PENDING進捗を保持し、残る作用と再closeを開始しない。生成途中の未公開資源の整理が失敗した場合は、例外のpipe属性から元objectを保持する。

通常のstdio入力・frame形式とMod/Data契約は維持する。このpinは生成時から保持する所有証拠であり、既存のpinなし保持枠や解放後の番号から採り直さない。第64の独立観測packetをpipeの未開始作用へ接続する明示recoverは次スライスであり、本スライスでは実装しない。既存の最終gateの観測時点保証と、最後の実読後の変更を検出できない残余窓は維持する。

pinは生成時の元ProxyPipe owner参照も保持し、ProxyPipe自体の浅いcopyへ元FDとpin群を載せても新しい所有枠として採用しない。verify指定時は、追加した実体観測の前後と各close直前へ同じ検査を渡す。前のpin解放中に期限へ達した場合も次の端点の観測を開始せず、期限を延長しない。

生成途中の整理不明では、OfflineProcessとDatabaseProxyも例外.pipeの同じ元objectをpipe欄へ保持する。OfflineProcessは元processとのcreation_resourcesも保全する。これは未確認資源の参照保持であり、完成packetの基準を作用後に採り直さない。追加記録・再生成・再closeをせず元例外を返す。

今回の通常closeは現在の保持pin群を対象とする。生成時pin群の原本性は、生成時に外部保持した各pin objectとの同一性（is）を後続recover入口で照合し、完成基準へ結ぶ。入口前のprivate _pinsのcopy群差替えを今回の通常closeで拒否する保証は含めない。現行製品の_pins書込みはconstructor内だけであり、任意のprivate改変への一般的な防御は追加しない。

### 最初Wの未開始pipeの明示解放（第66スライス）

元process closeの全正常復帰を第64の`offline-observe-worker-close/COMPLETED`で照合済みの同じPは、内部入口`RuntimeControl._offline_recover_worker_pipe`から、その最初Wの未開始pipeだけを一度解放できる。元open・解放・観測IDと異なるrecover ID、同じtokenと今回の絶対deadlineを指定する。元生成時にOfflineProcessへ外部保持した各pin objectを作用前packetへ束縛し、入口で同一性を照合する。pinなし旧枠・pipe側だけの各pin copy群・packet固定後の差替え・別実体は採用しない。

元packetの全保持群と正本を継承し、元親FD→対応pinの正常解放遷移だけを明示的に照合する。排他は既存のruntime lockとpipe専用lockを使い、I/Oを長いRegistry writeで囲まない。元processの再close・旧番号への作用・Data再送・追加同期はない。今回の要求原文と単発開始権は同じPの作用前保持枠へ一度固定する。開始後の例外・UNKNOWN/PIN_PENDING・期限到達では元資源/原文を保持し、同じID/別IDとも再実行を拒否する。

正常解放後は短いRegistry writeで`offline-recover-worker-pipe/COMPLETED`を独立記録し、`OFFLINE_WORKER_PIPE_RELEASED`を返す。新たな永続PENDINGは作らず、元open PENDING・pending1・予約1、元解放意図のhandle UNKNOWN・pipe NOT_STARTEDと他Wを維持する。元意図の進捗と今回の別作用の完了証拠は区別する。この結果は全W完了・元終端ではない。正常完了後も再実行/readbackは今回の入口では行わず、解放後の元観測の再照合も旧FD状態から推測しない。最後のRegistry照合と資源調停退出後は保持値の純比較と同一期限を確認して返し、観測後の外部不変は保証しない。残るW/pipe不明・全体終端・P再起動・公開recoverは後続とする。

生成時pinの原本性は、constructor由来の保持欄が元packet固定前に書き換えられないという製品内の前提に依拠する。現行製品のpipe_pins書込みは初期Noneとconstructor正常復帰直後だけである。packet固定後の差替え、又はpipe側/外部保持欄の片方だけの差替えは拒否する。元packet固定前に両参照を同じ浅いcopy群へ差し替える合成条件への改変耐性は保証しない。後続でconstructor由来のpipe_pins保持欄を元packet固定前に書き換える経路を追加する場合は、この前提を再確認して固定元を強める必要がある。現行の保持欄を無条件に生成時原本として扱わない。任意のprivate改変への一般的防御は追加しない。

# 停止中controller Cの固定入口

.github #64の第1スライスと第2スライス先行部分（CのAssigned登録）。停止中writerの公開受付は有効化していません。現在のwriter-ioはRunning限定です。

## 責務と固定配布

`offline_controller_bundle.offline_controller_command(bundle)`は完成bundleの全hash、同版DataModel Host配布、source・compiler・専用JAR・作成OSを照合し、固定JDK21と`OfflineControllerMain`の起動配列だけを返します。公開Host/CLIの管理root拒否は維持します。ローカルJDK、任意classpath、実行時のsource探索へ切り替えません。

新しいsource-lockでbundleを生成すると、`adapter/offline-controller.json`と`adapter/offline-controller/`を収録します。旧bundleの検証は維持しますが、helperのない旧bundleからこの入口を呼べません。既存bundleへ後付けせず、新bundleを生成してください。DB・run-stateの保存形式変更はありません。

Cは自分のData121 `openManagedOfflineWriter`／writer `assignWorker`／writer `close`だけを呼びます。ゲームのbeginRun/observeExit、119 Editの実行、receiptのcomplete、snapshot操作は受け付けません。元sealと不変chain・Ready・DBcloseの照合はData121へ委ねます。

## P側の前提

このmainはprivate inherited stdin/stdout専用です。ネットワークendpoint・任意SQL・Host許可flag・root常駐daemonを追加しません。Pは同じinstance/world境界でstart排他を取得し、元M/全childの終了、最新game sealと既存receipt、他writer受付遮断・accepted drainを確認した後にだけCを起動します。PがCの固定artifact・実PID/start/host/bootを観測し、C専用OwnedProcessと私設pipeを所有する必要があります。

PによるC/Wの独立OwnedProcess生成・認証済private IPCの接続は後続スライスです。Cのopenに渡すdrain hashはPの照合値であり、CがRegistry台帳の完全性を証明するものではありません。C側でもPの完全native identityの生存、実parent PID、C本人を照合します。起動引数は受け付けず、root・秘密・本文をcommand lineへ載せません。

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

`assign`はCが開いた同じownerの初回`OfflineIdle`からだけ受け付けます。PはDB未openで待機するWを固定bundleから独立Job/groupとして所有し、私設IPCの認証と実PID/start/host/bootの観測を完了してから送る必要があります。この起動・認証連携とW自体は未実装です。C側の生存照合だけでWのOwnedProcess所属を証明したとは扱いません。

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

一度登録するとこの入口では再登録・worker差替・recoverできません。Assignedのままcloseを要求してもDataが拒否し、ownerを解放しません。Wのadmission・Edit・候補公開・実終了・C completeは後続のため、この先行部分だけではAssignedを完了できません。製品のwriter受付からの呼出元は追加しておらず、通常利用へ公開しません。

## 失敗時

owner取得を試みる前の拒否だけを`REJECTED`とします。Data取得開始後は公開済みの可能性を含め`UNKNOWN`と非正常exitで保全します。取得後の不正assign/close・二重open、Assigned公開後の例外・応答喪失、close後の応答喪失、EOFで自動close・rollback・再登録・再取得しません。UNKNOWNには元ID・本文・processとstate証拠を保持します。新Cで本人APIを代行しません。

同要求と不変終了証拠を照合する正規recover、C/Wの実終了とRegistry CLOSED、start gateは後続スライスです。今回のhelperだけで停止中編集や復旧を完了扱いにしません。

## 検証

Python3.12の合成bundle fixtureで収録・改変・外部classpath・欠落を確認します。`SUSHIERIC_OFFLINE_TEST_BUNDLE`に固定Data121 bundleを明示すると、同bundleのJDK21/Host依存でJava compileと非native protocol vectorsを実行します。fake Data/本人APIによるopen/assign/close/EOF/UNKNOWN試験はDBや実worldを作りません。固定Data artifactへのcompile試験とfakeによる状態機械試験を分けて評価してください。

実owner取得、実root、両OSの本人・Job/group・私設pipe、実bundleビルド、Mod/Manager結合、#54のDB snapshotは別途受入が必要です。

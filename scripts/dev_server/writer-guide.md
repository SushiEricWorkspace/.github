# 管理Managerのwriterと保存仲介

RUNNINGの固定世代へManagerを接続し、停止・保存・切断の境界を扱うガイドです。
貸出は[監督・貸出](supervisor-guide.md)、JVM保存・終了は[runtime制御](runtime-control-guide.md)を参照してください。
この接続だけでsnapshot cleanを証明しません。正常停止したデータの保存は[snapshotガイド](snapshot-guide.md)に従います。

固定設定・短命貸出の共通契約と責任分担は[接続プロファイルガイド](connection-profile-guide.md)を参照してください。
別PCからの仲介は[SSH仲介ガイド](ssh-guide.md)を参照してください。
開発用Managerのプロファイル選択・借用・引継ぎ操作は[Manager接続ガイド](../../../SushiEricServerManager/docs/managed/connection-guide.md)を参照してください。

## 接続と識別

Managerは登録済みの非秘密プロファイルと候補枠を選び、明示の借用または一回限りの引継ぎで接続します。
Manager取得分だけrenewし、引継ぎでは発行元が貸出更新・停止・返却を担当します。
ローカルは同一ユーザーCLI、SSHは対象ホスト上の仲介を通します。管理rootを直接LocalStore/SFTPで開かず、非管理接続へfallbackしません。
statusからgeneration/runId/nonceとloopback Management endpointを固定し、接続中は追従しません。

CLIの`writer-open`/`writer-io`/`writer-close`はoperationIdと以下の共通payloadを必要とします。

| 項目 | 固定する値 |
| --- | --- |
| instanceId/leaseId/epoch/expectedGeneration | 対象枠と貸出・世代 |
| sessionId | 接続ごとに新規のID |
| runId/nonce | statusで取得した起動識別 |

`writer-check`は共通payloadだけでOPEN登録・native識別・RUNNINGを読み取り専用で照合します。
登録や完了証跡を追加せず、終了済み・結果不明のwriterを監視の根拠にしません。

openにはpid/startMillis/cwdを加えます。監督はnative PID・開始識別・boot・cwd・実行ファイル・command hash・OS ownerを記録し、
操作ごとに再照合します。instance OS lock→短いDB transactionの順で予約・確定し、native照合、FS、proxy通信・終了待機は両方の外で行います。
I/O受付は既writer操作と同じtransactionに内部`writer-call:<fingerprint>`予約を記録し、別processを含む同じinstanceのcallと競合を検出します。
同IDの再提示も実行中の予約を引き継ぎません。INSERTのcommitが成功したcallerだけが、call終端時にlease/epoch/世代/run/操作と予約全列を再照合して解除します。
call予約の解除はUNKNOWN proxyの終端やwriterのpending解除ではなく、別sessionの既受付条件も変更しません。解除失敗・crashでは予約を保全し、期限で回収しません。
DB closeは元IOの永続operationId/fingerprintを照合し、現在のclose自身のcall予約下で受理済みIOを完結します。元IOのcall予約は再生成しません。
`read`/`list`/`server-read`もI/O中は予約しますが、成功時は読取操作とcall予約を同じtransactionで削除し、本文を永続化しません。同IDの再読取りは新しい結果を返します。
stop/startは未完了writerまたはcall予約を作用前に拒否します。
管理rootのprivate IPC/ACLは既存契約を維持します。

常用のstatus/API/ファイル読取りはRegistryとactive.json・generation.json、journal/staging不在、
metadata/proof/保存rootのpath chain、native JVM識別・nonce・実socketを照合し、無関係なworld/runtime treeを列挙しません。
無関係な深いlink・特殊ファイルの検出はstatus/APIごとではなく、開始停止境界またはその操作対象時へ移ります。
start/stopの全世代検査、対象ファイル・move先の祖先、list各要素、recursive deleteの下降前検査は維持します。
書込みのquota/空き容量確認は引き続き管理root全体の実サイズを集計します。
rootのpath chainと各entryのlink/reparse/型を下降前に検査し、祖先をentryごとに再走査しません。
quota/reserveと実空き容量を毎回確認し、使用量cacheや開始時の値で代用しません。

Managerは本文を`--payload-stdin`で渡し、YAMLやAPI本文をOSコマンドラインへ載せません。
init/superviseへのstdin指定、他payload入力との併用は拒否します。
IPCは要求envelope・応答全体とも64KiBまでです。JSON escapeや共通識別にも領域が必要で、64KiBのファイルを必ず転送できる意味ではありません。
大きいファイルは拒否し、chunk転送やSFTPへ切り替えません。

## 編集と管理API

`writer-io`のrequestはactionに応じてpath/text/destinationまたはmessage/timeoutSecondsを指定します。
編集対象はMod config rootのconfig.yml、item_data、ore_data、shop_data、recipe_dataです。
manager-compat.json、既知UUIDのplayer_data配下のmoney_history/achievements、shop_historyの日別ファイル、serverのusercache.jsonは読取り専用です。
秘密設定、任意のserver root、JVM保存データへのFS writeは拒否します。
moveのtextはSushiEricDataModelで新IDへ再出力した本文です。移動先を排他的に作成・同期してから旧ファイルを削除し、
途中失敗では両ファイルとpendingを保全します。recipeのようなパス由来IDでは本文を渡しません。
削除は各階層のlink/reparse検査を列挙・下降より先に行います。

Managerのcommand/money-history-save/achievement-saveは監督の単発WebSocket仲介へ送ります。
既存のcodec・nonce・`success`を保持し、`completed:true`は通信とpeer Closeの完了だけを意味します。
`success:false`を保存成功へ書き換えません。監視専用socketにはmutationを送らず、古いcallbackはsession境界で無視します。
管理接続は自動reconnectしません。

## DB専用writer

`writer-io`の`action: database`は型付きManagement要求を1本の専用socketへ送ります。`message`と1〜30秒の`timeoutSeconds`だけを指定します。対象はcontext、履歴Start/Page/End、商品Products/DirectoryManifests/Edit、money-history-saveです。任意command・SQL・pathは扱いません。公開world/runと、監督側のinstance/generation/private nonceを区別して照合します。

固定bundleのHost CLIは非管理DB用です。稼働管理DBへHost CLIを直接接続せず、固定Java proxyから所有Mod JVMの公開codec/APIへ仲介します。Host ZIP/POM、Java source・生成JAR・Mod codec classdir・依存GAV・全classpath・JDKは作成OSのbundle hashへ含まれます。未同梱bundleの新DB入口だけを拒否し、既存API/FS操作は維持します。

監督はDB proxyを最大4本まで、各writerで未完要求1件と保持応答1件に制限します。私設frameはprefix込み64KiB、Management wireはUTF-8で60KiB、内側の商品要求は56KiBまでです。満杯は受付前に拒否し、UNKNOWNの証拠を期限や容量都合で破棄しません。本文・秘密を監督DB、生成command、診断logへ保存せず、既存writer正本へ状態とhashだけを記録します。

正本commit後だけ保持応答をackします。PENDINGは同じsocketの既送要求の遅延応答だけを照合し、UNKNOWNや部分送信を別socketへ再送しません。監督再起動で失われた本文を再生成せず、既知終端の状態/hashだけを返します。全UNKNOWNが読取りで解消する保証はありません。同本人・同operationId/hashでbootstrapのOPENと元DB要求未送信を確認できる中断だけは、`completed:true`・`rejectedBeforeEffect:true`・`code: DB_BOOTSTRAP_INTERRUPTED`として作用前拒否を確定します。UNKNOWNや証拠欠落へこの分類を適用しません。

close受付後は新規要求を遮断し、受理済みStart/Pageの終端、全履歴End、peer Close、helperと全childの実終了を確認して既存writer正本をCLOSEDにします。これは当該writerの終了証拠であり、Mod全体のdrain、main-thread cache更新、game/player保存や正常停止証拠の代わりではありません。全体停止統合は後続#64/#597の境界です。

固定Data b157/Mod472では起動admissionが未接続です。codec・固定helper・自己所有loopback試験の成功を実管理DB/サーバーの統合成功として扱いません。

## 拒否と終了証明

保存受付前の形式/対象パス拒否、FILE_NOT_FOUND、ALREADY_EXISTS、検査時のpermission拒否等には`error.rejectedBeforeEffect:true`を返します。
Managerはこの明示情報がある失敗だけOPENを維持できます。error codeだけから作用前拒否を推測しません。
保存受付後のOS/DB障害、API応答喪失/Close未完、CLI応答不明はUNKNOWNとし、未完了操作とwriter登録を保持します。
検査時点と実操作の間にOS状態が変わった場合も、受付後の失敗は保留です。
同じoperationIdの完了済み再送は識別とsession状態を再確認します。UNKNOWN/PENDINGを再実行しません。
`action: api`の本文はDBへ記録せず応答hashだけを残すため、再送結果からAPI本文を再取得できません。DB専用writerは同じhelperが保持する応答だけを返し、保持本文を失った場合は`responseAvailable:false`と状態/hashを返します。完了済みDB操作のdry-run再提示は状態/hashだけを返し、保持応答の受信・ACK・call予約を行いません。

切断は新規受付を閉じ、受理済みAPIキューを結果確定まで実行します。
executor待機はI/O lock外、writer-closeは実行中I/Oが終わった後です。
read-only socketもpeer Closeを確認し、drained/disconnectedが揃ったときだけ登録をCLOSEDにします。
遅延handshakeやtimeoutを完了へ変換せず、プロセスkill・強制queue破棄・自動writer解除は行いません。
アプリ異常終了、lease期限切れ、監督再起動でwriter不在を推測しません。
supervisor再起動時には枠をRECOVERY_REQUIREDにし、復旧機能が接続されるまで保留します。

statusのwriters/writerSessionsは登録正本です。runtimeStopProofとwriter不在の両方が揃ったときだけcleanEligibleを返します。
これはsnapshot作成へ進める候補判定です。snapshotCleanはsnapshot作成の全検証が成功した後だけtrueになります。
同一OSユーザーの非協調的な直接編集、登録外writer、FS停電耐久性の証明は含みません。
Registry DB/INITIALIZEDはversion=5です。旧version=4も拒否・保全し、自動移行しません。旧開発DBは拒否・保全し、移行や既存rootの上書きは行いません。
試験には新しい空rootを用意してください。

## 自動検証

```bash
python3.12 -B -m unittest discover -s scripts -p test_writer_control.py -v
python3.12 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

合成runtime、native自己プロセス、使い捨てTCP/WebSocketの失敗注入と実Minecraft保存往復は別の証拠です。
Windowsでの成功をmacOS native・JavaFX実画面の成功として扱いません。

実Modと開発用ManagerのOS・方向別の結果は[統合検証記録](../../../SushiEricServerMod/docs/core/manager-integration-results-guide.md)を参照してください。

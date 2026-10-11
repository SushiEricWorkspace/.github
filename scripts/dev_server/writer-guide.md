# 管理Managerのwriterと保存仲介

RUNNINGの固定世代へManagerを接続し、停止・保存・切断の境界を扱うガイドです。
貸出は[監督・貸出](supervisor-guide.md)、JVM保存・終了は[runtime制御](runtime-control-guide.md)を参照してください。
この接続だけでsnapshot cleanを証明しません。正常停止したデータの保存は[snapshotガイド](snapshot-guide.md)に従います。

## 接続と識別

Managerの管理接続では管理root、Python 3.12、`scripts/dev-server.py`の絶対パス、instanceId、取得済みleaseId/epochを入力します。
自動acquire/renew、SSH/SFTP、管理rootを直接開くLocalStore、失敗時の非管理接続へのfallbackは使用しません。
statusからgeneration/runId/nonceとloopback Management endpointを固定し、接続中は追従しません。

CLIの`writer-open`/`writer-io`/`writer-close`はoperationIdと以下の共通payloadを必要とします。

| 項目 | 固定する値 |
| --- | --- |
| instanceId/leaseId/epoch/expectedGeneration | 対象枠と貸出・世代 |
| sessionId | 接続ごとに新規のID |
| runId/nonce | statusで取得した起動識別 |

openにはpid/startMillis/cwdを加えます。監督はnative PID・開始識別・boot・cwd・実行ファイル・command hash・OS ownerを記録し、
操作ごとに再照合します。native照合、FS、通信はSQL write transaction外で行い、
枠のinstance lockは保存/APIの応答とCloseまで保持します。stop/startと同時に書き込みません。
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
moveのtextはCommonで新IDへ再出力した本文です。移動先を排他的に作成・同期してから旧ファイルを削除し、
途中失敗では両ファイルとpendingを保全します。recipeのようなパス由来IDでは本文を渡しません。
削除は各階層のlink/reparse検査を列挙・下降より先に行います。

Managerのcommand/money-history-save/achievement-saveは監督の単発WebSocket仲介へ送ります。
既存のcodec・nonce・`success`を保持し、`completed:true`は通信とpeer Closeの完了だけを意味します。
`success:false`を保存成功へ書き換えません。監視専用socketにはmutationを送らず、古いcallbackはsession境界で無視します。
管理接続は自動reconnectしません。

## 拒否と終了証明

保存受付前の形式/対象パス拒否、FILE_NOT_FOUND、ALREADY_EXISTS、検査時のpermission拒否等には`error.rejectedBeforeEffect:true`を返します。
Managerはこの明示情報がある失敗だけOPENを維持できます。error codeだけから作用前拒否を推測しません。
保存受付後のOS/DB障害、API応答喪失/Close未完、CLI応答不明はUNKNOWNとし、未完了操作とwriter登録を保持します。
検査時点と実操作の間にOS状態が変わった場合も、受付後の失敗は保留です。
同じoperationIdの完了済み再送は識別とsession状態を再確認します。UNKNOWN/PENDINGを再実行しません。
API本文はDBへ記録せず応答hashだけを残すため、再送結果からAPI本文を再取得できません。

切断は新規受付を閉じ、受理済みAPIキューを結果確定まで実行します。
executor待機はI/O lock外、writer-closeは実行中I/Oが終わった後です。
read-only socketもpeer Closeを確認し、drained/disconnectedが揃ったときだけ登録をCLOSEDにします。
遅延handshakeやtimeoutを完了へ変換せず、プロセスkill・強制queue破棄・自動writer解除は行いません。
アプリ異常終了、lease期限切れ、監督再起動でwriter不在を推測しません。
supervisor再起動時には枠をRECOVERY_REQUIREDにし、復旧機能が接続されるまで保留します。

statusのwriters/writerSessionsは登録正本です。runtimeStopProofとwriter不在の両方が揃ったときだけcleanEligibleを返します。
これはsnapshot作成へ進める候補判定です。snapshotCleanはsnapshot作成の全検証が成功した後だけtrueになります。
同一OSユーザーの非協調的な直接編集、登録外writer、FS停電耐久性の証明は含みません。
Registry DB/INITIALIZEDはversion=4です。旧開発DBは拒否・保全し、移行や既存rootの上書きは行いません。
試験には新しい空rootを用意してください。

## 自動検証

```bash
python3.12 -B -m unittest discover -s scripts -p test_writer_control.py -v
python3.12 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

合成runtime、native自己プロセス、使い捨てTCP/WebSocketの失敗注入と実Minecraft保存往復は別の証拠です。
Windowsでの成功をmacOS native・JavaFX実画面の成功として扱いません。

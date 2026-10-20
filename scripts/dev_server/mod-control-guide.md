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
start/stop待機が保持するinstance OS lockと4 worker poolへこのcontrolを共有すると
handshakeが進まないため、別bounded受付を用意する必要があります。
未接続の予約table・新daemon・汎用SQL/path・認証fallbackはこの先行成果に含めません。

## 原bytes正本とUUIDの先行接続

既runtime_operationsを使う未接続のMod専用readbackと、
検証済みData runtimeだけに適用するUUID境界は
[正本・UUIDガイド](mod-control-operations-guide.md)を参照してください。
native受付・台帳最新性・停止成功は引き続き未接続です。

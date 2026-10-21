# Mod control原bytes正本とData UUID境界（先行実装）

Refs #64。mod_control_operations.pyはMod専用wire v2の要求/応答原文を、
既runtime_operations内へ保存する未接続の先行成果です。
既start/stop/writerのfingerprint・replayed応答契約を横断変更していません。
native endpoint、peer認証、instance/run/lease/世代の受付排他、
writer台帳・native作用・停止成功・observer起動へは接続していません。

## 原bytesと同要求readback

既wire2 parser/correlate_response_v2を使用し、schemaやData型を再定義しません。
commandはmod-control/<元request phase>、fingerprintは元request bodyのSHA-256です。
既response TEXTのJSON内部へrequestBodyUtf8/responseBodyUtf8を保持し、
schema/table/columnは追加していません。
prefixはUTF-8 bodyのbyte長から同じ4byte BEへ復元します。

呼出元のRegistry.transaction内でのみ使用します。
本moduleはtransaction開始/commit、OS lock、native effectのcallbackを受け持ちません。

- reserve: 未登録の元要求をPENDINGへ記録。既存ならreadbackだけを返す。
- readback: instance/command/operationId、hashと原要求bytes全体を照合して保存応答を返す。
- complete: 元PENDINGへ相関済みの終端応答を固定。完了済みは同response bytesだけを許可。
- unknown: 元未完了を既runtimeのRECOVERY_REQUIREDへ保全。確定済みCOMPLETEDは格下げしない。

runtime_operationsの既statusはPENDING/COMPLETED/RECOVERY_REQUIREDです。
新DB status UNKNOWNは導入せず、RECOVERY_REQUIREDのwire outcomeをUNKNOWNへ対応づけます。
PENDING/UNKNOWNの同要求は保存error応答を返し、予約/作用を再生成しません。
completeをRECOVERY_REQUIREDから呼んで結果不明を成功へ変換する経路もありません。
有効な作用前拒否はCOMPLETEDのerror/REJECTED_BEFORE_EFFECTとして固定できます。

同構造でも元bodyの再整形、異nonce/descriptor hash/instance、
異なる元request bytes、確定responseの再整形は拒否します。
完成結果へreplayed等を追加して新bodyへ変えません。
他既operation tableの同IDも予約競合として拒否します。

新予約がNoneを返してもnative admissionの許可ではありません。
実P受付は将来、認証と同instance/run/lease/epoch/世代の正本照合を別途行う必要があります。
この局所試験はSQLite上のtransaction・原文保存・再提示での変更0を確認するだけで、
実FSの停電耐久、supervisor再起動時の本人継承、native再作用の実結合保証ではありません。

## UUIDを検査する境界

新しいRegistry acquireのleaseIdとHumanServerの自動generationIdは
canonicalなstr(uuid.uuid4())を使用します。
既operationのcached tokenや既rootの値は変換しません。

runtime_bundle.verify_bundleはvalidate(runtime)を必須とし、
runtime v2はdataModelCoordinateとDataModelソース記録を要求します。
v1は旧Common座標/ソースの契約です（contracts.py、contracts-v1.json）。

この検証済みv2 manifestにだけrequire_data_generationを適用します。

- GenerationControl._create: verify_bundle直後、世代予約・journal・コピー前
- RuntimeControl.execute(start): verify_bundle直後、runtime予約・起動前

v1の旧Common世代名、templateId、artifactId、operationId、runtimeRunIdなどには
UUID制約を追加しません。UUIDへ別名投影したり32桁hexを暗黙に直したりしません。

Data129のrequireManagementはleaseId/generationにUUID.fromStringを要求するため、
新規生成と管理Data入口をこの形式へ揃えます。
未リリース機能の旧32桁hex lease/任意名generationが残る開発rootは原本を保全し、
Data管理runへ使う場合は新しい貸出/UUID世代または許可された明示再作成が必要です。
既root・Data state・旧receiptへの自動移行、手編集、再baselineは行いません。

この先行変更ではRegistry schemaVersion/INITIALIZED markerを変更していません。
v5の受付門/revision/native台帳は設計段階であり、実装済みと扱いません。

## 局所検証

SQLite memoryとMockで、原request/response bytes、PENDING/UNKNOWNで変更0、
COMPLETEDの元response再提示、異要求競合、rollback、
Data世代拒否の予約/コピー/起動前境界、新UUID・旧token不変・template名非制限を検証します。
既wire/契約/localIPC mockのpure回帰も合わせて確認します。
native/server/writer実結合、Java observer、実root、SSH、公開は未実行です。

## 後続GRANTの結合条件

既Registry._tokenは旧32桁hexのleaseも既値として受理します。今回の新規UUID生成は、その旧値をData管理に使用できる保証ではありません。
後続の実GRANT受付では作用前にgenerationとleaseIdの両方がDataのUUID条件を満たすことを確認し、満たさない旧値は保全して拒否する必要があります。
既tokenの受理だけからDataManagementBindingを発行せず、別名投影や自動変換も行いません。このGRANT境界は未接続・未実装です。


## M/server exact launchのpure検証（未接続）

runtime_bundle.validate_managed_launch(profile, context, observed)は、承認済みM/server列とP生成context11値からargv/cwd/envと3補助file原bytesの完全一致を確認します。versionはintの1だけでboolを拒否し、未知field/token/slot、必須property欠落、重複、encoding不一致をContractErrorで拒否します。

profileはversion/purpose/role/platform/argfileEncoding/mainClass/classpath/jvmArgs/args/environment/dliText/remapClasspath、observedはargv/cwd/environment/auxiliaryFilesのexact objectです。contextはbundle/run/auxiliary/privateTmp/controlDescriptor/runtimeManifest/proofPath/instanceId/generationId/startNonce/controlDescriptorSha256だけです。observedPidやproxy/O/C/Wは対象外です。

argfileの共有encoding名はutf-8/windows-31jだけ。DLI/remapはUTF-8で、argfileは指定codecのroundtripを必須とします。共有静的期待bytesはmanaged-argfile-vectors-v1.jsonです。

この関数はfile/OS/環境を読まずprocessも起動しません。通常launch_commandへは未接続です。profile自体の承認、実contextの本人/descriptor実体、native origin、wire2、台帳、Mac artifact/運用受入を検証した扱いにしません。実接続時はP正本/承認profileを先に確定する必要があります。

引数・context・env値のASCII control（U+0000–U+001FとDEL U+007F）も拒否します。DLIだけは仕様上のLF/TABを許可し、DELは許可しません。


## wire2保存入口と旧行の保全

保存入口はwire2専用です。wire1 codecと原vector自体は維持しますが、このmoduleでは版の自動選択やv1 fallbackをしません。旧v1要求は更新前に拒否し、同IDの旧行はfingerprint/原要求bytes不一致で拒否します。保存応答がv1ならwire2相関検証で拒否し、旧行を変換・削除・再生成しません。未リリースの旧開発記録は保全対象であり、新要求への移行処理はありません。

API引数・戻り値、runtime_operationsの6列、command/status、requestBodyUtf8/responseBodyUtf8の保存形式は変更しません。原bodyのversionもhashとbytes全体へ含まれ、同構造でも別rawの要求は同要求readbackではありません。HELLO/BOUNDの証拠hash echo等は既wire2へ委譲し、Data全モデルやP正本contextの新validatorを増やしません。

P正本context/native本人/run/lease/世代の確認は将来callerの責務です。保存入口が確認するinstance/command/operationId/元body SHA/raw一致だけで認証済みと扱いません。現在の製品callerは未接続で、start/stop・writer close・別phase間の最新台帳排他は今回の範囲外です。

同operationのPENDING/UNKNOWN再提示は再作用0。unknown先行後のcompleteは拒否し、complete先行後のunknownは確定応答を保持します。異なる終端bytesは拒否します。新IDによる自動再送を許可するものではありません。SQLite接続close時の未commit予約rollback、commit済みPENDING/UNKNOWNの再open後readbackは局所試験で確認しますが、writer実終了・実FS停電耐久・OS/native受入は証明しません。

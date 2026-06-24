# Google Sheetsスキーマ

正確な列順と型は `../assets/google-sheets-schema.json` を正とする。

## Tabs

### Metadata

schema version、ID sequence、協調ロック、最新run、最終同期commitを保持する。key-value形式。

### Configuration

秘密情報を含まない実行設定の要約を保持する。ローカルconfigの秘密値や接続生値は書かない。

### Inventory

コード／ブラウザから発見した機能候補。全項目を `MAPPED` または `EXCLUDED_WITH_REASON` に収束させる。

### Scenarios

Feature、Story、Scenarioを1行に正規化して保持する。最新状態を更新する。

人間編集可能:

- review_status
- notes
- manual_override
- manual_expected_behavior
- manual_exclusion_reason
- manual_priority

その他の列はSkillだけが更新する。

### Test Runs

attempt単位の追記専用履歴。過去行を更新・削除しない。

### Defects

同一原因の継続追跡。first seen／latest seen／occurrence countを保持する。

### Evidence

artifactのpath、URL、hash、redaction、retentionを保持する。秘密情報実体は置かない。

## 同期

- 書き込みはbatch単位で行う。
- 同一batch内の関連セルをまとめる。
- v1は単一ライター。
- lockは協調ロックであり、強いtransaction lockではない。
- active lockがある場合は停止する。
- collaborator変更を検出した場合は自動上書きせず `SYNC_CONFLICT` とする。
- 実行途中の接続障害ではローカルWALへappendし、runを `PAUSED_SYNC_REQUIRED` にする。

## ID allocation

Metadataのsequenceを読み、単一ライター前提で次IDを割り当てる。割り当て後は再利用しない。

## Manual override

`manual_override` がtruthyの場合:

- `manual_expected_behavior` をテスト期待値に使用する。
- `manual_exclusion_reason` があるScenarioは除外候補とする。
- `manual_priority` でrisk depthを上書きする。
- 自動生成値を消さず保持する。

# DB安全仕様

## 分類

- `dedicated`: テスト専用DB
- `shared`: 複数人または複数環境が利用
- `unknown`: 分類不能

初期値は `unknown`。

## 接続前ガード

静的コード解析以外でDB接続を伴う操作をする前に、次を検証する。

1. `expected_host_pattern` が空でない。
2. 実ホストがpatternと一致する。
3. `expected_database_pattern` が空でない。
4. 実DB名がpatternと一致する。
5. sentinel queryが空でない。
6. sentinel結果が `expected_value` と一致する。

実ホスト、DB名、認証情報の生値は会話、Sheets、artifactへ出力しない。結果は `MATCHED`／`MISMATCHED` と理由コードだけにする。

## Reset

初期値:

```yaml
destructive_reset: false
reset_scope: none
```

破壊的resetを許可する全条件:

- classificationが `dedicated`
- `destructive_reset: true`
- host／database／sentinelが一致
- `reset_scope: suite` または外部人間操作の `manual`
- 実行時に明示許可
- reset commandが設定済み

`manual` はSkillがreset commandを実行する意味ではない。人間が外部で準備し、Skillは状態を再検証するだけとする。

共有／不明DB:

- snapshot restore: deny
- TRUNCATE: deny
- 広範囲DELETE: deny
- `reset_scope: none` または `manual`

## Seed

Scenario seedは次を満たす。

- non-destructive
- idempotent
- 同一 `test_run_id` と同一Scenarioで再実行しても重複しない
- 既存業務データを上書きしない
- 作成データを `test_run_id` と関連付ける

関連付け優先順位:

1. 対象テーブルの `test_run_id` 列
2. 安全な一意列への識別子埋め込み
3. テスト専用追跡テーブル
4. ローカルManifest

Manifest例:

```json
{"test_run_id":"WAD-...","scenario_id":"SCN-000001","table":"users","primary_key":"123","created_at":"..."}
```

Manifestへ秘密値や行全体を保存しない。

## 所有権境界

共有DBでは:

- create: 許可されたseedまたはUI操作のみ
- update: 現run所有データのみ
- delete: 現run所有データのみ
- 既存データread: 許可
- 既存データupdate／delete: 禁止
- 既存master: 原則read-only

所有権を証明できない場合は操作しない。

## Cleanup

既定:

```yaml
cleanup: always
cleanup_scope: current_test_run_only
retain_on_failure: false
```

順序:

1. failure evidenceを取得
2. 追跡情報から現run所有行を確定
3. 外部キーを考慮して子から親へ削除
4. 削除件数と結果だけを記録
5. 所有不明行は残し `CLEANUP_BLOCKED`

PASS／FAILを問わずcleanupする。明示されたrunだけretain overrideを許可する。overrideはSheetsへ記録する。

## Secret handling

`local.php`、`app_local.php`、database config等は読むことができるが、次を外部出力しない。

- username／password
- DSN／connection string
- host／databaseの生値
- API key／token
- private key

子プロセスへ渡す場合はcommand line argumentを避け、環境変数または標準入力を使う。

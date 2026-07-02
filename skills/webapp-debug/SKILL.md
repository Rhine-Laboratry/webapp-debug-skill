---
name: webapp-debug
description: コードベースとブラウザからWebアプリの機能を棚卸しし、日本語Scenario、Playwrightテスト、Google Sheetsの進捗・不具合記録を生成する。init、discover、test、full、resume、reportを明示指定した場合に使用する。
---

# Webapp Debug

## 目的

定義済み探索面から機能インベントリを作り、コード由来の暫定期待動作をFeature／User Story／Scenarioへ変換し、Google Sheetsを正準状態としてPlaywrightテストと不具合分析を管理する。

## 起動条件

1. 呼び出し引数から最初のトークンをモードとして読む。
2. 有効なモードは `init`、`discover`、`test`、`full`、`resume`、`report` のみ。
3. モードがない、または不明な場合は使用方法を表示して終了する。調査、ブラウザ起動、DB操作、Sheets更新を開始しない。
4. このSkillを暗黙起動しない。
5. `report` 以外は単一ライターの協調ロックを取得する。既存の有効ロックがあれば停止する。

## 絶対条件

- Google SheetsをInventory、Scenario、実行履歴、不具合、証跡メタデータの正準データとする。
- アプリケーション実装を暫定仕様として扱う。コードと既存テストが矛盾したらコードを優先し、競合を記録する。
- 既存テストとアプリケーションコードを編集しない。
- 自動コミットしない。
- 秘密情報、Cookie、Authorization Header、セッション、接続文字列、サービスアカウント秘密鍵を外部出力しない。
- `discover` は非破壊とし、変更系フォーム、削除、メール、外部連携を実行しない。
- 共有または不明DBでsnapshot restore、TRUNCATE、広範囲DELETEを実行しない。
- 所有権を証明できないデータを更新または削除しない。
- 不具合を修正しない。原因候補、証拠、確信度、追加調査だけを出力する。
- 「全機能を網羅した」と断定するのは、完了条件を満たした場合だけとする。動的機能や解析不能箇所は `DISCOVERY_GAP` として残す。

## 最初に読むファイル

1. `.webapp-debug/config.yml`
2. `references/workflow.md`
3. 実行モードに必要な参照:
   - discovery: `references/discovery.md`
   - Scenario: `references/scenario-model.md`
   - Sheets: `references/sheets-schema.md`
   - DB: `references/database-safety.md`
   - test: `references/test-policy.md`
   - failure: `references/error-and-rca.md`
   - secrets: `references/security-redaction.md`
4. 設定がなければ `init` 以外を実行せず、`init` を案内する。

## 実装済みローカルCLI

現在このrepoに含まれる決定的な補助CLIは次のとおり。

- `scripts/validate_skill.py --root .`
- `scripts/validate_config.py --config <path> --mode init|discover|test|full|resume|report`
- `scripts/validate_sheets_schema.py --schema skills/webapp-debug/assets/google-sheets-schema.json`
- `scripts/init_sheets.py --config .webapp-debug/config.yml --dry-run`
- `scripts/redact_artifact.py --input <path> --output <path>`
- `scripts/evaluate_coverage.py --config <path> --inventory-json <path>`
- `scripts/export_sheets_snapshot.py --config <path> --output <path>`
- `scripts/discover_cakephp_inventory.py --root <repo-root> --output <path>`
- `scripts/plan_inventory_sync.py --discovery-json <path> --snapshot-json <path> --output <path>`
- `scripts/apply_inventory_sync.py --plan <path> --confirm-spreadsheet-id <id>`
- `scripts/plan_scenario_sync.py --discovery-json <path> --snapshot-json <path> --output <path>`
- `scripts/apply_scenario_sync.py --plan <path> --confirm-spreadsheet-id <id>`
- `scripts/bootstrap_playwright_project.py --dry-run`
- `scripts/generate_playwright_tests.py --scenario-json <path> --dry-run`

Google Sheets初期化は `scripts/init_sheets.py` を使う。CakePHP静的Inventory discoveryは `scripts/discover_cakephp_inventory.py` を使い、ローカルJSONへ出力してからcoverage gateや同期計画へ渡す。Sheetsの現在状態を評価または同期計画へ使う場合は、`scripts/export_sheets_snapshot.py` でread-only snapshotを作成する。`scripts/plan_inventory_sync.py` と `scripts/plan_scenario_sync.py` はローカルsync plan JSONだけを生成する。snapshot exportとsync planningはSheets write、lock、WAL、bootstrap、config更新を行わない。Inventory計画を適用する場合は `scripts/apply_inventory_sync.py` を使い、Scenario計画を適用する場合は `scripts/apply_scenario_sync.py` を使う。どちらもSpreadsheet ID完全一致確認、cooperative lock、WAL、read-backを必須にする。Playwright生成先のskeleton準備は `scripts/bootstrap_playwright_project.py`、構造化Scenarioからの静的test skeleton生成は `scripts/generate_playwright_tests.py` を使う。これらのCLIはPlaywright、npm、Composer、PHP、DB、ブラウザ、Google APIを実行しない。unsupported action、DB/seed/mailbox/upload等のruntime safety gate未実装Scenario、locator source不足Scenarioはrunnable specではなくBLOCKED status planにする。実Google Sheets API統合テストは明示envが揃った場合だけ実行する。Playwright runner orchestration、ブラウザ実行を伴う動的discovery、Test Runs/DefectsのSheets適用は後続実装であり、現時点で実装済みと扱わない。

## 共通Preflight

以下を順に実施し、結果を記録する。

1. リポジトリルート、Git commit、branch、dirty状態を取得する。
2. `composer.json`、`composer.lock`、`package.json`、lockfile、README、Makefile、Docker設定からスタックと起動候補を検出する。
3. CakePHP、PHP、Node、JavaScript構成を検出する。CakePHP 2.xはgeneric解析へ切り替える。
4. Google Sheets接続、schema version、必要タブ、列、単一ライターロックを検査する。
5. アプリ起動方法、base URL、readiness URLを解決する。
6. 認証情報はseed、環境変数、storageState、手動ログインの順に解決する。取得できなければ関連Scenarioを `AUTH_BLOCKED` とする。
7. ブラウザまたはDB接続を伴う実行前にDBガードを検証する。静的解析だけはDBガード未成立でも継続できる。
8. operation profileと実行時overrideを解決する。hard safety ruleはoverride不可。
9. `.webapp-debug/state/` の未同期WALがあれば、`resume` 以外では停止する。

## DBガード

ブラウザ起動、seed、DB読み取り、テスト実行の前に次をすべて満たす。

- 実接続先ホストを秘密値を出力せず取得できる。
- `expected_host_pattern` が空でなく、実ホストと一致する。
- 実DB名を秘密値を出力せず取得できる。
- `expected_database_pattern` が空でなく、実DB名と一致する。
- sentinel queryが設定され、結果が期待値と一致する。

破壊的resetにはさらに次を要求する。

- `classification: dedicated`
- `destructive_reset: true`
- `reset_scope: suite` または明示された `manual`
- 共有／不明DBではない。
- 実行時にも明示許可がある。

ガード不成立時は実行せず、秘密値を含まない理由コードを記録する。

## モード

### init

1. リポジトリを静的に検査する。
2. `.webapp-debug/config.yml` がなければexampleから作成する。既存ファイルは上書きしない。
3. `.webapp-debug/state/`、`.webapp-debug/artifacts/`、認証state用ディレクトリを作成する。
4. Google Sheets初期化は `scripts/init_sheets.py` で実行する。config検証、Sheets schema検証、credential検証、dry-run、WAL、cooperative lock、read-backを通過させる。
5. Spreadsheet IDがなければ、init時だけ `scripts/init_sheets.py --create` を補助的に許可する。標準導線は、人間がテスト専用Spreadsheetを作成し、サービスアカウントへ編集権限を付与してから既存Spreadsheetを初期化する方法とする。
6. `assets/google-sheets-schema.json` に従ってタブとヘッダーを作る。既存の未知列は削除しない。
7. `scripts/bootstrap_playwright_project.py --dry-run` でPlaywright生成先のskeleton計画を確認する。構造化Scenarioの静的test skeletonは `scripts/generate_playwright_tests.py --dry-run` で計画できるが、runner orchestrationは未実装なので実行済みとして扱わない。
8. アプリケーション側の `package.json`、テスト、コードは変更しない。
9. DBガード項目が未設定なら、未設定キーだけを具体的に報告する。秘密値を表示しない。
10. init結果を表示して終了する。discover/testへ自動移行しない。

### discover

1. 共通Preflightとロック取得を行う。
2. `references/discovery.md` と `references/discovery-rules.md` に従って静的探索を実施する。CakePHP静的Inventory候補は `scripts/discover_cakephp_inventory.py` でローカルJSON snapshotとして生成できる。Sheets反映前には `scripts/export_sheets_snapshot.py` のread-only snapshotと `scripts/plan_inventory_sync.py` のローカルsync planを使い、人間編集列を壊さない計画だけを確認する。適用は `scripts/apply_inventory_sync.py --dry-run` の後、専用Spreadsheet IDの明示confirmation付きで行う。
3. DBガードとアプリ起動が成立した場合だけ、GET中心の受動的ブラウザ探索を行う。
4. Inventoryを作成・更新し、各項目をE2E対象、別テスト方式、除外候補へ分類する。
5. コード根拠から日本語Feature／User Story／Scenarioを生成する。
6. Scenario深度はBを標準とし、HIGHはC、CRITICALはDへ引き上げる。
7. 既存行をfingerprintとsource anchorで照合し、重複を作らない。内容変更時はversionを上げる。
8. 消えた項目は削除せず、確認後 `RETIRED` または `MERGED` とする。
9. 25件程度ごとにSheetsへbatch同期し、ローカルcheckpointも更新する。
10. `coverage.max_discovery_passes` まで反復し、各pass後にcoverage gateを評価する。未達でも無制限に継続しない。
11. strict modeでは全有効Inventoryが `MAPPED` または `EXCLUDED_WITH_REASON` になった場合だけ完了とする。risk-gated modeは明示設定時だけthresholdでtest移行可否を判定する。
12. 解析不能は `DISCOVERY_GAP` として未完了に残し、risk-gatedで許容されても削除しない。
13. 調査結果を要約し、ロックを解放する。

### test

1. 共通Preflight、DBガード、認証、ロックを確認する。
2. Sheetsから有効Scenarioを読み、`manual_override` を自動生成値より優先する。
3. `references/test-policy.md` に従ってPlaywright環境と生成先を準備する。
4. `PENDING` Scenarioのテストを生成する。既存手書きテストは編集しない。
5. `test_run_id` を発行し、seed、追跡テーブル、Manifestのいずれかで作成データを関連付ける。
6. workers 1、retries 1、Chromium 1440×900、ja-JP、Asia/Tokyoで全有効Scenarioを実行する。
7. 各attemptを `Test Runs` に追記する。再試行で成功したものは `FLAKY` とする。
8. FAIL／BLOCKED時は証跡を取得し、原因候補を分析してDefectsへ記録する。
9. 証跡取得後、現在の `test_run_id` 所有データだけをcleanupする。所有不明データは削除しない。
10. cleanup失敗を隠さず `CLEANUP_BLOCKED` または `CLEANUP_FAILED` として記録する。
11. 全有効Scenarioに終端結果があること、FAIL／BLOCKEDに必須文書があることを検証する。
12. Sheets同期を完了し、ロックを解放する。

### full

1. `discover` を最大pass数まで実行し、各pass後にcoverage gateを評価する。
2. strict gate達成、または明示的risk-gated gate達成時だけ `test` へ切り替える。
3. gate未達時はtestへ進まず、理由付きで終了する。risk-gated達成を100% coverageや全機能網羅と表現しない。
4. 最後にcoverage、生成、実行、文書化の4完了条件を検証する。

### resume

1. Sheetsを正としてロック、Metadata、最新run、ローカルWAL、checkpointを照合する。
2. 同期前のローカル変更があれば、競合がないことを確認してbatch同期する。
3. 最後の未完了phaseまたはScenarioから再開する。
4. 完了済みScenarioを無条件に再実行しない。
5. 状態の整合性を証明できなければ `RESUME_BLOCKED` として停止する。

### report

1. 読み取り専用でSheetsとローカル証跡メタデータを集計する。
2. Sheets集計には `scripts/export_sheets_snapshot.py` のread-only JSON snapshotを使える。snapshot取得はSheets更新、lock、WAL、bootstrap、config更新を行わない。
3. Inventory closure、coverage policy達成、risk別open gap、Scenario生成率、PASS／FAIL／BLOCKED／FLAKY、Defect severity、cleanup異常、stale項目を表示する。
4. `DISCOVERY_GAP` がある場合、coverageを100%と表示しない。risk-gatedでtest移行可能な場合も残存gapはreportから消さない。
5. ブラウザ起動、DB操作、テスト生成、Sheets更新を行わない。

## IDと履歴

- `test_run_id`: `WAD-<UTC timestamp>-<random suffix>`
- Feature、Story、Scenario IDはSheetsの連番から発行し、再利用しない。
- source fingerprintは照合用であり、公開IDではない。
- Scenario内容変更時は同じIDのversionを上げる。
- Test Runsは追記専用で上書きしない。
- Evidence実体をSheetsに埋め込まず、path／URL／hashだけを記録する。

## 完了条件

- discovery: 全Inventoryが `MAPPED` または `EXCLUDED_WITH_REASON`
- generation: 全テスト可能Scenarioが `GENERATED` または `BLOCKED`
- execution: 全有効Scenarioが `PASS`、`FAIL`、`BLOCKED`、`FLAKY`
- documentation: 全FAIL／BLOCKEDに証跡、再現手順、原因候補がある

いずれかを満たさなければ、完了と宣言せず不足項目を列挙する。

## 最終応答

- 実行モード、run ID、commit、対象環境を示す。
- 完了条件ごとの成立／不成立を示す。
- 主要なFAIL／BLOCKED／FLAKY、cleanup異常、DISCOVERY_GAPを示す。
- Sheetsとartifactの保存先を示す。ただし秘密値を含めない。
- 推測と確定事実を区別する。

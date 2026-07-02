# webapp-debug skill v0.2

Webアプリケーションのコードベースとブラウザ動作から機能を棚卸しし、日本語のFeature／User Story／Scenario、Playwrightテスト、Google Sheets上の進捗・不具合記録を生成するAgent Skillです。

現在の実装は、v0.2 Runtime Hardeningとして安全な初期化、検証基盤、bounded discovery coverage gate、Google Sheetsからのread-only snapshot export、CakePHP静的Inventory discovery、Inventory同期計画のローカル生成と明示確認付き適用を追加した状態です。Playwright生成器は後続実装です。

Phase 6C以降の長期計画とsubagent orchestration方針は、`docs/MASTER_IMPLEMENTATION_PLAN.md`、`docs/ORCHESTRATION_RUNBOOK.md`、`docs/PHASE_ACCEPTANCE_CRITERIA.md` にまとめています。これらは将来計画であり、未実装機能を実装済みと示すものではありません。

## 現在の状態

実装済み:

- GitHub Actions CIによるunit test、integration skip確認、lint、validator、CLI help、pip check
- `scripts/release_check.py` によるv0.2.0 release readiness自己診断
- Codex／Claude wrapperのfrontmatter互換と `scripts/validate_skill.py`
- `scripts/validate_config.py` によるconfig validator
- `scripts/validate_sheets_schema.py` によるSheets schema validator
- `scripts/redact_artifact.py` によるtext／JSON／YAML／HAR redaction
- append-only WAL
- Google SDKに依存しないSheets backend abstraction
- 単一ライター前提のcooperative lock
- `scripts/init_sheets.py` による安全なGoogle Sheets初期化
- Google Sheets API adapter
- 実Google Sheets API統合テスト。ただし明示opt-in時だけ実行
- `scripts/evaluate_coverage.py` によるローカルInventory JSON向けcoverage gate evaluator
- `scripts/export_sheets_snapshot.py` によるGoogle Sheets read-only snapshot export
- `scripts/discover_cakephp_inventory.py` によるCakePHP静的Inventory JSON生成
- `scripts/plan_inventory_sync.py` によるInventory sync plan JSON生成。Sheets writeは行わない
- `scripts/apply_inventory_sync.py` によるInventory sync planのGoogle Sheets適用。実行時はSpreadsheet IDの完全一致確認、cooperative lock、WAL、read-backを要求する

未実装:

- Playwright Scenario生成器／runner orchestration
- ブラウザ実行を伴う動的discovery
- Drive APIによる共有、削除、権限設定
- OAuthユーザーフロー、domain-wide delegation
- 強い分散ロック
- screenshot／video／trace zip内画像の自動PII redaction

## 配置

このパッケージは正準Skillと各エージェント用ラッパーをrepo内に含みます。

- Codex: `.agents/skills/webapp-debug/SKILL.md`
- Claude Code: `.claude/skills/webapp-debug/SKILL.md`
- 共通の正準Skill: `skills/webapp-debug/SKILL.md`

dot-directoryもGit追跡対象です。

```bash
git ls-files .agents .claude
```

## 呼び出し

Codex:

```text
$webapp-debug init
$webapp-debug discover
$webapp-debug test
$webapp-debug full
$webapp-debug resume
$webapp-debug report
```

Claude Code:

```text
/webapp-debug init
/webapp-debug discover
/webapp-debug test
/webapp-debug full
/webapp-debug resume
/webapp-debug report
```

モードを省略した場合はヘルプだけを返し、副作用のある処理を開始しません。

## 初回推奨導線

1. Python 3.11以上のvenvを作成し、依存関係を入れる。
2. `python scripts/validate_skill.py --root .` を実行する。
3. `.webapp-debug/config.yml` を作成する。
4. `python scripts/validate_config.py --config .webapp-debug/config.yml --mode init` を実行する。
5. `python scripts/validate_sheets_schema.py --schema skills/webapp-debug/assets/google-sheets-schema.json` を実行する。
6. Google Spreadsheetを手動で作成する。
7. サービスアカウントへそのSpreadsheetの編集権限を付与する。
8. `.webapp-debug/config.yml` にSpreadsheet IDとcredential env名を設定する。
9. `python scripts/init_sheets.py --config .webapp-debug/config.yml --dry-run` を実行する。
10. `python scripts/init_sheets.py --config .webapp-debug/config.yml` を実行する。
11. 必要な場合だけopt-in統合テストを実行する。

`--create` は補助機能です。標準導線は、手動でテスト専用Spreadsheetを作成し、サービスアカウントへ編集権限を付与してから既存Spreadsheetを初期化する方法です。サービスアカウントが作成したSpreadsheetはユーザーのDriveへ自動共有されません。このSkillはDrive APIによる共有を行いません。

## 主要CLI

```bash
python scripts/validate_skill.py --root .
python scripts/validate_config.py \
  --config skills/webapp-debug/assets/webapp-debug.config.example.yml \
  --mode init
python scripts/validate_sheets_schema.py \
  --schema skills/webapp-debug/assets/google-sheets-schema.json
python scripts/init_sheets.py --help
python scripts/redact_artifact.py --help
python scripts/evaluate_coverage.py --help
python scripts/export_sheets_snapshot.py --help
python scripts/discover_cakephp_inventory.py --help
python scripts/plan_inventory_sync.py --help
python scripts/apply_inventory_sync.py --help
python scripts/release_check.py --version 0.2.0
```

## CIとローカル検証

GitHub Actionsの `CI` workflowはPython 3.11、3.12、3.13で次を実行します。

```bash
python -m pip check
python -m pytest -q
python -m pytest tests/integration -q
python -m ruff check .
python -m ruff format --check .
python scripts/validate_skill.py --root .
python scripts/validate_sheets_schema.py \
  --schema skills/webapp-debug/assets/google-sheets-schema.json
python scripts/validate_config.py \
  --config skills/webapp-debug/assets/webapp-debug.config.example.yml \
  --mode init
python scripts/init_sheets.py --help
python scripts/evaluate_coverage.py --help
python scripts/export_sheets_snapshot.py --help
python scripts/discover_cakephp_inventory.py --help
python scripts/plan_inventory_sync.py --help
python scripts/apply_inventory_sync.py --help
```

CIではGoogle credential env、実Spreadsheet ID、Drive API、DB、Playwright、CakePHP parserを設定または実行しません。`tests/integration` はopt-in env未設定によりskipされることを正常として確認します。

## CakePHP static discovery

CakePHP 3.x〜5.xのroutes、controller action、template hint、plugin/prefixを静的に読み取り、Google Sheetsへ直接書かずローカルJSON snapshotを作成できます。CakePHP 2.xはgeneric PHP解析として扱います。PHP、Composer、DB、ブラウザ、Google APIは実行しません。

```bash
python scripts/discover_cakephp_inventory.py \
  --root /path/to/cakephp-app \
  --output .webapp-debug/state/discovery/inventory.json \
  --include-plugins

python scripts/evaluate_coverage.py \
  --config .webapp-debug/config.yml \
  --inventory-json .webapp-debug/state/discovery/inventory.json \
  --current-pass 1
```

出力Inventoryは `DISCOVERED` または `DISCOVERY_GAP` を基本状態とします。`MAPPED` や `EXCLUDED_WITH_REASON` はScenario化や人間確認後の後続工程で扱います。

## Inventory sync plan

CakePHP static discoveryのローカルJSONと、`scripts/export_sheets_snapshot.py` のread-only snapshot JSONを比較し、Inventoryタブへ反映するための同期計画をローカルJSONとして作成できます。この段階ではGoogle Sheetsへ書き込みません。

```bash
python scripts/discover_cakephp_inventory.py \
  --root /path/to/cakephp-app \
  --output .webapp-debug/state/discovery/inventory.json

python scripts/export_sheets_snapshot.py \
  --config .webapp-debug/config.yml \
  --output .webapp-debug/state/snapshots/snapshot.json \
  --tabs Inventory

python scripts/plan_inventory_sync.py \
  --discovery-json .webapp-debug/state/discovery/inventory.json \
  --snapshot-json .webapp-debug/state/snapshots/snapshot.json \
  --schema skills/webapp-debug/assets/google-sheets-schema.json \
  --output .webapp-debug/state/sync/inventory-sync-plan.json
```

同期計画は `APPEND_INVENTORY`、`UPDATE_INVENTORY_FIELDS`、`MARK_INVENTORY_RETIRED`、`APPEND_DISCOVERY_GAP` を表現します。人間編集列と未知列は自動更新対象にしません。

適用前にはdry-runでfresh snapshotとの整合を確認できます。通常実行ではSpreadsheet IDの完全一致確認が必須で、lock取得、WAL pending、batch update、read-back、WAL ack、lock releaseの順に進みます。

```bash
python scripts/apply_inventory_sync.py \
  --config .webapp-debug/config.yml \
  --schema skills/webapp-debug/assets/google-sheets-schema.json \
  --plan .webapp-debug/state/sync/inventory-sync-plan.json \
  --dry-run

python scripts/apply_inventory_sync.py \
  --config .webapp-debug/config.yml \
  --schema skills/webapp-debug/assets/google-sheets-schema.json \
  --plan .webapp-debug/state/sync/inventory-sync-plan.json \
  --confirm-spreadsheet-id <spreadsheet-id> \
  --wal .webapp-debug/state/wal/inventory-apply.jsonl
```

## v0.2.0 release readiness

package versionは `pyproject.toml` の `project.version` で管理し、`src/webapp_debug_skill/__init__.py` の `__version__` と一致させます。v0.2.0のtag表記は `v0.2.0` です。

release準備の自己診断:

```bash
python scripts/release_check.py --version 0.2.0
python scripts/release_check.py --version 0.2.0 --format json
```

release note草案は `docs/RELEASE_NOTES_v0.2.0.md`、手順確認は `docs/RELEASE_CHECKLIST.md` にあります。このrepoはtag作成、GitHub Release作成、PyPI publish、Docker publishを自動化しません。

Google Sheets初期化:

```bash
python scripts/init_sheets.py \
  --config .webapp-debug/config.yml \
  --schema skills/webapp-debug/assets/google-sheets-schema.json \
  --dry-run

python scripts/init_sheets.py \
  --config .webapp-debug/config.yml \
  --schema skills/webapp-debug/assets/google-sheets-schema.json
```

Metadata storageがない既存Spreadsheetをbootstrapする場合は、明示確認が必要です。

```bash
python scripts/init_sheets.py \
  --config .webapp-debug/config.yml \
  --schema skills/webapp-debug/assets/google-sheets-schema.json \
  --bootstrap-lock-storage \
  --confirm-spreadsheet-id <spreadsheet-id>
```

Coverage gate評価はローカルJSON入力を使います。Google Sheetsから直接評価する場合は、まずread-only snapshotをJSONへexportし、そのJSONを `evaluate_coverage.py` に渡します。snapshot exportはSheetsへのwrite、lock、WAL、bootstrap、config更新を行いません。

```bash
python scripts/export_sheets_snapshot.py \
  --config .webapp-debug/config.yml \
  --schema skills/webapp-debug/assets/google-sheets-schema.json \
  --output .webapp-debug/state/sheets-snapshot.json \
  --tabs Inventory,Scenarios,Defects

python scripts/evaluate_coverage.py \
  --config .webapp-debug/config.yml \
  --inventory-json .webapp-debug/state/sheets-snapshot.json \
  --current-pass 1 \
  --format json
```

ローカルJSONを直接評価する場合:

```bash
python scripts/evaluate_coverage.py \
  --config .webapp-debug/config.yml \
  --inventory-json .webapp-debug/state/inventory.json \
  --current-pass 1 \
  --format json
```

入力は次の形式を受け付けます。

```json
{
  "Inventory": [
    {"inventory_id": "INV-001", "status": "MAPPED", "risk": "HIGH"}
  ]
}
```

`coverage.mode: strict` では全有効Inventoryが `MAPPED` または `EXCLUDED_WITH_REASON` の場合だけtestへ進めます。`risk-gated` は明示設定時だけthresholdで判定しますが、残存gapを100% coverageや全機能網羅とは表現しません。

## Google認証

サービスアカウントJSONはリポジトリ外へ置き、`.webapp-debug/config.yml` の `sheets.service_account_credentials_env` に環境変数名だけを設定します。credential path、private key、client email、access tokenをissue、log、Sheets、WALへ貼らないでください。

例:

```bash
export WEBAPP_DEBUG_GOOGLE_SERVICE_ACCOUNT=/secure/path/service-account.json
```

```yaml
sheets:
  service_account_credentials_env: WEBAPP_DEBUG_GOOGLE_SERVICE_ACCOUNT
```

## Opt-in統合テスト

既定の `python -m pytest -q` では、実Google統合テストはskipされます。実行する場合は、テスト専用Spreadsheetだけを使ってください。本番または共有業務Spreadsheetを対象にしないでください。

```bash
WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION=1 \
WEBAPP_DEBUG_GOOGLE_CREDENTIALS_FILE=/path/outside/repo/service-account.json \
WEBAPP_DEBUG_GOOGLE_SPREADSHEET_ID=... \
WEBAPP_DEBUG_GOOGLE_CONFIRM_SPREADSHEET_ID=... \
python -m pytest tests/integration -q
```

create flowはさらに明示許可が必要です。作成したSpreadsheetは削除も共有もされません。

```bash
WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION=1 \
WEBAPP_DEBUG_GOOGLE_CREDENTIALS_FILE=/path/outside/repo/service-account.json \
WEBAPP_DEBUG_GOOGLE_SPREADSHEET_ID=... \
WEBAPP_DEBUG_GOOGLE_CONFIRM_SPREADSHEET_ID=... \
WEBAPP_DEBUG_GOOGLE_ALLOW_CREATE=1 \
WEBAPP_DEBUG_GOOGLE_CREATE_TITLE="webapp-debug integration test" \
python -m pytest tests/integration -q
```

## 安全上の注意

- service account keyをリポジトリへ置かない。
- `.webapp-debug/`、WAL、artifact、auth state、credentialをGit追跡しない。
- `discover` の静的解析以外でDBやブラウザ実行を伴う場合、DBガードを成立させる。
- v0.2のlockは単一ライター前提のcooperative lockであり、完全な分散排他ではない。
- redaction不能なbinary artifactを安全化済みと扱わない。

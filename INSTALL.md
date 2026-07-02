# 導入手順

## 1. 要件

- Python 3.11以上
- `pip`
- Google Sheets APIを使う場合はサービスアカウントJSON
- 実Google統合テストを使う場合は、専用Spreadsheetと明示env

`python` コマンドがない環境では、明示Pythonパスまたはvenv内のPythonを使ってください。

## 2. 依存関係

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pip check
```

このrepoは `requirements.lock` を含む場合があります。固定依存で検証したい場合は、既存のlock方式を維持して次を使います。

```bash
python -m pip install -r requirements.lock
python -m pip install -e .
```

## 3. 配置確認

CodexとClaude Codeのラッパーも実ファイルとして含めます。

```text
.agents/skills/webapp-debug/SKILL.md
.agents/skills/webapp-debug/agents/openai.yaml
.claude/skills/webapp-debug/SKILL.md
skills/webapp-debug/SKILL.md
skills/webapp-debug/agents/openai.yaml
```

dot-directoryがGitで追跡されていることを確認します。

```bash
git ls-files .agents .claude
python scripts/validate_skill.py --root .
```

## 4. 設定作成

```bash
mkdir -p .webapp-debug
cp skills/webapp-debug/assets/webapp-debug.config.example.yml .webapp-debug/config.yml
```

`.webapp-debug/config.yml` で最低限、次を設定します。

- project ID／name
- app base URL／start command／readiness URL
- Google Spreadsheet ID
- `sheets.service_account_credentials_env`
- allowed hosts
- DB classification
- expected host pattern
- expected database pattern
- sentinel query／expected value
- seed command

接続文字列やpasswordをconfigへ複製しないでください。

```bash
python scripts/validate_config.py \
  --config .webapp-debug/config.yml \
  --mode init
```

## 5. Google認証

サービスアカウントJSONはリポジトリ外へ置き、owner-only permissionにします。

```bash
chmod 600 /secure/path/service-account.json
export WEBAPP_DEBUG_GOOGLE_SERVICE_ACCOUNT=/secure/path/service-account.json
```

configには環境変数名だけを入れます。

```yaml
sheets:
  service_account_credentials_env: WEBAPP_DEBUG_GOOGLE_SERVICE_ACCOUNT
```

Spreadsheetを手動作成し、サービスアカウントへ編集権限を付与します。Drive APIによる共有はこのSkillでは行いません。

## 6. Sheets schema検証

```bash
python scripts/validate_sheets_schema.py \
  --schema skills/webapp-debug/assets/google-sheets-schema.json
```

## 7. Google Sheets初期化

最初にdry-runを実行します。

```bash
python scripts/init_sheets.py \
  --config .webapp-debug/config.yml \
  --schema skills/webapp-debug/assets/google-sheets-schema.json \
  --dry-run
```

問題がなければ初期化します。

```bash
python scripts/init_sheets.py \
  --config .webapp-debug/config.yml \
  --schema skills/webapp-debug/assets/google-sheets-schema.json
```

既存SpreadsheetにMetadata storageがない場合だけ、明示確認付きでbootstrapします。

```bash
python scripts/init_sheets.py \
  --config .webapp-debug/config.yml \
  --schema skills/webapp-debug/assets/google-sheets-schema.json \
  --bootstrap-lock-storage \
  --confirm-spreadsheet-id <spreadsheet-id>
```

`--create` は補助機能です。サービスアカウントが作成したSpreadsheetはユーザーのDriveへ自動共有されず、このSkillはDrive API共有を行いません。

## 8. Sheets snapshot exportとcoverage評価

Google Sheetsの現在状態をcoverage evaluatorへ渡す場合は、read-only snapshotをJSONへexportします。このCLIはSheetsへのwrite、lock、WAL、bootstrap、config更新を行いません。

```bash
python scripts/export_sheets_snapshot.py \
  --config .webapp-debug/config.yml \
  --schema skills/webapp-debug/assets/google-sheets-schema.json \
  --output .webapp-debug/state/sheets-snapshot.json

python scripts/evaluate_coverage.py \
  --config .webapp-debug/config.yml \
  --inventory-json .webapp-debug/state/sheets-snapshot.json \
  --current-pass 1
```

## 9. CIと同じローカル検証

release前、またはCI failureの再現時は、venvを有効化して次を実行します。`python` コマンドがない環境では、明示Pythonパスまたはvenv内のPythonを使ってください。

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
python scripts/redact_artifact.py --help
python scripts/evaluate_coverage.py --help
python scripts/export_sheets_snapshot.py --help
python scripts/discover_cakephp_inventory.py --help
python scripts/plan_inventory_sync.py --help
python scripts/release_check.py --version 0.2.0
python scripts/release_check.py --version 0.2.0 --format json
```

CIにはGoogle credential env、実Spreadsheet ID、DB接続情報、Playwright実行、CakePHP parserを設定しません。実Google統合テストはCIでは自動実行せず、env未設定でskipされることを確認します。

## 10. CakePHP static discovery

CakePHP Inventory候補を静的解析だけでローカルJSONへ出力できます。対象アプリのファイルは変更せず、PHP、Composer、DB、ブラウザ、Google APIを実行しません。

```bash
python scripts/discover_cakephp_inventory.py \
  --root /path/to/cakephp-app \
  --output .webapp-debug/state/discovery/inventory.json \
  --include-plugins
```

生成JSONはcoverage evaluatorへ渡せます。

```bash
python scripts/evaluate_coverage.py \
  --config .webapp-debug/config.yml \
  --inventory-json .webapp-debug/state/discovery/inventory.json \
  --current-pass 1
```

CakePHP 3.x〜5.xを主対象とし、CakePHP 2.xはgeneric解析です。動的routeや解析不能箇所は `DISCOVERY_GAP` として残します。

## 11. Inventory sync plan

Discovery JSONとread-only Sheets snapshot JSONから、Sheetsへ適用する前のローカル同期計画を生成できます。このCLIはGoogle Sheets API、DB、ブラウザ、Playwright、PHP、Composerを実行しません。

```bash
python scripts/plan_inventory_sync.py \
  --discovery-json .webapp-debug/state/discovery/inventory.json \
  --snapshot-json .webapp-debug/state/snapshots/snapshot.json \
  --schema skills/webapp-debug/assets/google-sheets-schema.json \
  --output .webapp-debug/state/sync/inventory-sync-plan.json
```

既存出力は `--force` なしでは拒否します。`--allow-retire-missing` を付けた場合だけ、discoveryから消えた管理対象Inventoryを `RETIRED` にする計画を作れます。

適用前にはdry-runでfresh snapshotとの整合を確認してください。実行時はSpreadsheet IDの完全一致確認、cooperative lock、WAL、read-back verificationを要求します。

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

## 12. Release readiness

v0.2.0の準備状態は次で確認します。

```bash
python scripts/release_check.py --version 0.2.0
```

version sourceは `pyproject.toml` の `project.version` です。`src/webapp_debug_skill/__init__.py` にある `__version__` も同じ値にします。tag表記は `v0.2.0` ですが、この手順ではtag、GitHub Release、PyPI publish、Docker publishを作成しません。

release前に `docs/RELEASE_CHECKLIST.md` と `docs/RELEASE_NOTES_v0.2.0.md` を確認してください。

## 13. Opt-in統合テスト

既定ではskipされます。

```bash
python -m pytest tests/integration -q
```

実Google Sheets APIを使う場合:

```bash
WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION=1 \
WEBAPP_DEBUG_GOOGLE_CREDENTIALS_FILE=/path/outside/repo/service-account.json \
WEBAPP_DEBUG_GOOGLE_SPREADSHEET_ID=... \
WEBAPP_DEBUG_GOOGLE_CONFIRM_SPREADSHEET_ID=... \
python -m pytest tests/integration -q
```

create flowも確認する場合:

```bash
WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION=1 \
WEBAPP_DEBUG_GOOGLE_CREDENTIALS_FILE=/path/outside/repo/service-account.json \
WEBAPP_DEBUG_GOOGLE_SPREADSHEET_ID=... \
WEBAPP_DEBUG_GOOGLE_CONFIRM_SPREADSHEET_ID=... \
WEBAPP_DEBUG_GOOGLE_ALLOW_CREATE=1 \
WEBAPP_DEBUG_GOOGLE_CREATE_TITLE="webapp-debug integration test" \
python -m pytest tests/integration -q
```

Inventory applyも実Spreadsheetへ書いて確認する場合は、専用Spreadsheetに対して追加opt-inを設定します。

```bash
WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION=1 \
WEBAPP_DEBUG_GOOGLE_CREDENTIALS_FILE=/path/outside/repo/service-account.json \
WEBAPP_DEBUG_GOOGLE_SPREADSHEET_ID=... \
WEBAPP_DEBUG_GOOGLE_CONFIRM_SPREADSHEET_ID=... \
WEBAPP_DEBUG_GOOGLE_ALLOW_INVENTORY_APPLY=1 \
python -m pytest tests/integration -q
```

作成されたSpreadsheetは削除されません。共有設定も変更されません。

## 13. Git除外

```bash
cat skills/webapp-debug/assets/gitignore.fragment >> .gitignore
```

重複行は整理します。credential、`.webapp-debug/`、WAL、artifact、Playwright auth stateをGitに追加しないでください。

## 14. トラブルシューティング

- `GOOGLE_CREDENTIAL_ENV_MISSING`: configのcredential env名が空、または環境変数が未設定です。
- `GOOGLE_CREDENTIAL_FILE_UNSAFE`: credential fileがsymlink、リポジトリ内、またはpermission不安全です。
- `SHEETS_INIT_BOOTSTRAP_REQUIRED`: 既存SpreadsheetにMetadata storageがありません。専用Spreadsheetであることを確認し、明示confirmation付きでbootstrapしてください。
- `SHEETS_BOOTSTRAP_CONFIRMATION_REQUIRED`: bootstrapには`--confirm-spreadsheet-id`が必要です。
- `SHEETS_LOCK_HELD`: 別実行のcooperative lockが有効です。単一ライター運用を確認してください。
- `SHEETS_WRITE_OUTCOME_UNKNOWN`: 外部writeの結果が不明です。WAL resumeやread-backで状態確認してください。
- `CONFIG_TARGET_UNSAFE`: config write対象が存在しない、symlink、directory、またはcanonical assetです。
- `SHEETS_SNAPSHOT_TAB_MISSING`: snapshot対象のcanonical tabがSpreadsheetにありません。初期化状態を確認してください。
- `SHEETS_SNAPSHOT_HEADER_CONFLICT`: snapshot対象tabのheaderがcanonical schemaと完全一致していません。未知の末尾列は許可されますが、既定列の順序、大小文字、空列、重複、formula-like headerは拒否されます。
- `DISCOVERY_NO_CAKEPHP_APP`: 指定rootにCakePHP構造を検出できません。
- `DISCOVERY_TOO_MANY_FILES`: `--max-files` 上限を超えたため静的解析を停止しました。

## 15. 次の実行

初回導線は `init` です。`discover` は非破壊の静的解析から始まり、DBガード未成立の場合はブラウザ探索をblockします。

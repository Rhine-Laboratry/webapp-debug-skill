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

## 8. Opt-in統合テスト

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

作成されたSpreadsheetは削除されません。共有設定も変更されません。

## 9. Git除外

```bash
cat skills/webapp-debug/assets/gitignore.fragment >> .gitignore
```

重複行は整理します。credential、`.webapp-debug/`、WAL、artifact、Playwright auth stateをGitに追加しないでください。

## 10. トラブルシューティング

- `GOOGLE_CREDENTIAL_ENV_MISSING`: configのcredential env名が空、または環境変数が未設定です。
- `GOOGLE_CREDENTIAL_FILE_UNSAFE`: credential fileがsymlink、リポジトリ内、またはpermission不安全です。
- `SHEETS_INIT_BOOTSTRAP_REQUIRED`: 既存SpreadsheetにMetadata storageがありません。専用Spreadsheetであることを確認し、明示confirmation付きでbootstrapしてください。
- `SHEETS_BOOTSTRAP_CONFIRMATION_REQUIRED`: bootstrapには`--confirm-spreadsheet-id`が必要です。
- `SHEETS_LOCK_HELD`: 別実行のcooperative lockが有効です。単一ライター運用を確認してください。
- `SHEETS_WRITE_OUTCOME_UNKNOWN`: 外部writeの結果が不明です。WAL resumeやread-backで状態確認してください。
- `CONFIG_TARGET_UNSAFE`: config write対象が存在しない、symlink、directory、またはcanonical assetです。

## 11. 次の実行

初回導線は `init` です。`discover` は非破壊の静的解析から始まり、DBガード未成立の場合はブラウザ探索をblockします。

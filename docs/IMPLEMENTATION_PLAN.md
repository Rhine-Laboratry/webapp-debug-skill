# webapp-debug-skill v0.2 Runtime Hardening Implementation Plan

## 1. 目的

v0.1は、機能探索、Scenario生成、Playwright実行、Google Sheets管理、DB安全条件を詳細に定義している。一方、frontmatter互換性、設定／Sheets schema検証、Google Sheets初期化、協調ロック、WAL、redaction、coverage gateが決定的なスクリプトとして実装されていない。

v0.2では、次を達成する。

1. CodexとClaude CodeのSkillメタデータを明確に分離する。
2. 危険な処理の前提条件を機械的に検証する。
3. Google Sheets初期化をidempotentかつfail-closedにする。
4. 協調ロック、WAL、redactionを再利用可能な実装へ落とす。
5. discoveryを無限反復させず、strictまたはrisk-gatedな完了判定を行う。
6. CIで構造・schema・安全条件の退行を検出する。

## 2. 実装原則

- 作業はフェーズ順に行い、1回のCodexタスクで1フェーズだけ実装する。
- Phase 2のvalidatorが完成する前に、Phase 3のGoogle Sheets書き込み処理を実装しない。
- 外部状態を変更する処理は、検証、dry-run、協調ロック、WALの順で通過させる。
- Google Sheetsは正準データだが、v0.2のロックは単一ライター運用を補助する協調ロックであり、分散トランザクションや完全なcompare-and-swapを保証しない。
- unknownな構成、schema競合、redaction不能、所有権不明は、推測で継続せず停止する。

## 3. 今回の非対象

以下はv0.2 Runtime Hardeningの非対象とする。

- CakePHP AST解析器の本実装
- Playwright Scenario生成エンジンの完成
- DB reset／seedコマンドそのものの自動生成
- UIを持たないAPI、cron、queue、外部連携の専用テストランナー
- Google Drive／S3へのartifact upload
- screenshot、video、Playwright trace内画像の自動PIIマスク
- 複数ライターに対する強い排他保証
- アプリケーションコードへの `data-testid` 追加

これらを実装済みとREADMEへ記載しない。

## 4. 目標ディレクトリ構成

```text
.
├── AGENTS.md
├── CHANGELOG.md
├── DECISIONS.md
├── INSTALL.md
├── README.md
├── pyproject.toml
├── docs/
│   └── IMPLEMENTATION_PLAN.md
├── src/
│   └── webapp_debug_skill/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── errors.py
│       ├── redaction.py
│       ├── sheets_schema.py
│       ├── sheets_client.py
│       ├── sheets_lock.py
│       ├── wal.py
│       └── coverage.py
├── scripts/
│   ├── validate_skill.py
│   ├── validate_config.py
│   ├── validate_sheets_schema.py
│   ├── init_sheets.py
│   ├── redact_artifact.py
│   └── evaluate_coverage.py
├── tests/
│   ├── fixtures/
│   ├── unit/
│   └── integration/
├── skills/webapp-debug/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── assets/
│   └── references/
├── .agents/skills/webapp-debug/
│   ├── SKILL.md
│   └── agents/openai.yaml
├── .claude/skills/webapp-debug/
│   └── SKILL.md
└── .github/workflows/ci.yml
```

`src/` の内部構成は、責務を保てる範囲で調整可能。ただし `scripts/` の6つのCLI名は固定する。

## 5. 共通CLI契約

全Python CLIへ次を適用する。

### 5.1 出力

- 既定は人間向けtext。
- `--format json` では、最低限次の形を返す。

```json
{
  "ok": false,
  "code": "CONFIG_DB_GUARD_INCOMPLETE",
  "message": "Database-backed execution is blocked.",
  "details": [
    {"path": "database.expected_host_pattern", "reason": "empty"}
  ]
}
```

- JSON出力にも秘密値を含めない。
- エラー時は安全なreason codeと設定キーだけを表示し、実接続値を表示しない。

### 5.2 終了コード

```text
0  成功、または要求された検査がready
2  引数、YAML／JSON、schema、frontmatterの不正
3  安全ポリシーまたは実行前提によりBLOCKED
4  外部サービス、認証、I/Oの失敗
5  協調ロック競合または状態競合
10  予期しない内部エラー
```

### 5.3 副作用

- 外部状態を変更するCLIは `--dry-run` を必須実装する。
- dry-runはGoogle APIのwrite method、ローカルWAL作成、設定ファイル更新を行わない。
- 既存ファイル更新には明示的な `--write-config` または `--force` を要求する。

---

# Phase 0: Baselineと作業境界

## 目的

現行状態を記録し、後続フェーズで無関係な差分を混ぜない。

## 作業

1. `feat/v0.2-runtime-hardening` ブランチを使用する。
2. `git status --short --branch`、ファイル一覧、現行テスト有無を確認する。
3. 現行の以下を記録する。
   - `skills/webapp-debug/SKILL.md` frontmatter
   - `.agents/` と `.claude/` の有無
   - `agents/openai.yaml`
   - config exampleとconfig schemaの差分
   - Google Sheets schemaのtab／column数
4. baselineでは機能変更をしない。

## 受け入れ条件

- 現行差分を上書きしていない。
- Phase 1で修正する対象が列挙されている。

---

# Phase 1: Skill metadataと配置整合

## 目的

Codex validatorで落ちるfrontmatterを修正し、READMEと実ファイルを一致させる。

## 変更対象

```text
skills/webapp-debug/SKILL.md
skills/webapp-debug/agents/openai.yaml
.agents/skills/webapp-debug/SKILL.md
.agents/skills/webapp-debug/agents/openai.yaml
.claude/skills/webapp-debug/SKILL.md
README.md
INSTALL.md
scripts/validate_skill.py
src/webapp_debug_skill/errors.py
src/webapp_debug_skill/cli.py
pyproject.toml
```

## 1.1 正準SKILL frontmatter

`skills/webapp-debug/SKILL.md` のfrontmatterを次の2キーだけにする。

```yaml
---
name: webapp-debug
description: コードベースとブラウザからWebアプリの機能を棚卸しし、日本語Scenario、Playwrightテスト、Google Sheetsの進捗・不具合記録を生成する。init、discover、test、full、resume、reportを明示指定した場合に使用する。
---
```

- `disable-model-invocation` を削除する。
- `argument-hint` を削除する。
- 引数構文は本文の「起動条件」または「使用方法」に残す。
- 本文の「暗黙起動しない」はポリシー説明として残してよいが、Codexの実制御は `openai.yaml` に置く。

## 1.2 Codexラッパー

`.agents/skills/webapp-debug/SKILL.md` を実ファイルとして追跡する。

- frontmatterは `name` と `description` のみ。
- `../../../skills/webapp-debug/SKILL.md` を正準Skillとして読むことを本文に明記する。
- 引数がない場合はhelpのみ返す。
- `.agents/skills/webapp-debug/agents/openai.yaml` を配置する。

Codex用 `openai.yaml` は最低限次を含む。

```yaml
interface:
  display_name: "Webapp Debug"
  short_description: "コード探索、Scenario化、Playwright実行、Sheets記録"
  default_prompt: "$webapp-debug init"
policy:
  allow_implicit_invocation: false
```

`skills/webapp-debug/agents/openai.yaml` も、直接インストール時のため同じ意味内容にする。2ファイルの意味内容が一致するテストを追加する。

## 1.3 Claude Codeラッパー

`.claude/skills/webapp-debug/SKILL.md` だけに次を許可する。

```yaml
disable-model-invocation: true
argument-hint: "init|discover|test|full|resume|report [--config <path>] [--profile <name>]"
```

- `$ARGUMENTS` を正準Skillへ渡す説明を残す。
- 共通SKILLへClaude固有キーを戻さない。

## 1.4 README／INSTALL

- `.agents/skills/...` と `.claude/skills/...` が実際にrepoへ含まれる説明へ修正する。
- 初回導線は `init` とする。
- `report` は既存状態を読むモードであり、初回導線ではないことを明記する。
- dot-directoryがGitに追跡されていることを確認するコマンドを記載する。

```bash
git ls-files .agents .claude
```

## 1.5 `validate_skill.py`

CLI:

```bash
python scripts/validate_skill.py --root . [--format text|json]
```

検査内容:

- YAML frontmatterの構文。
- `name` とディレクトリ名の一致。
- 正準SKILLとCodexラッパーは `name`／`description` 以外を拒否。
- Claudeラッパーでは、少なくとも `name`、`description`、`disable-model-invocation`、`argument-hint` を許可。
- Codexの `allow_implicit_invocation` がfalse。
- Codexの `default_prompt` が `$webapp-debug init`。
- READMEに記載したwrapper pathが実在。
- ラッパーの正準Skill相対パスが解決可能。

## Phase 1テスト

- 正常な3種類のSkillが成功する。
- Codex SKILLへ `argument-hint` を追加したfixtureが失敗する。
- 正準SKILLへ `disable-model-invocation` を追加したfixtureが失敗する。
- wrapper欠落、相対パス破損、openai policy欠落が失敗する。
- YAML parserが未導入の場合に曖昧なstack traceではなく依存不足を報告する。

## Phase 1受け入れ条件

- Codex用frontmatterに許可外キーがない。
- Claude固有キーがClaudeラッパーだけにある。
- `default_prompt` がinit。
- READMEと実配置が一致する。
- `validate_skill.py` の正常／拒否テストが成功する。

推奨commit境界:

```text
fix: align skill metadata and repository wrappers
```

---

# Phase 2: ConfigとSheets schemaの決定的validator

## 目的

テンプレートに存在する重要項目をschemaで検証し、mode／capability固有の安全条件をPython validatorで判定する。

## 変更対象

```text
skills/webapp-debug/assets/config.schema.json
skills/webapp-debug/assets/webapp-debug.config.example.yml
skills/webapp-debug/assets/google-sheets-schema.schema.json
scripts/validate_config.py
scripts/validate_sheets_schema.py
src/webapp_debug_skill/config.py
src/webapp_debug_skill/sheets_schema.py
tests/unit/test_config_validation.py
tests/unit/test_sheets_schema_validation.py
tests/fixtures/config/
tests/fixtures/sheets_schema/
```

## 2.1 Python依存と品質基盤

`pyproject.toml` に最低限次を定義する。

Runtime:

- PyYAML
- jsonschema
- google-auth
- google-api-python-client

Development:

- pytest
- pytest-cov
- ruff

バージョンは実装時点でPython 3.11以上と互換な範囲を選び、再現可能なlockfileを追加する。パッケージ管理方式は1つに統一する。

## 2.2 config schema拡張

`config.schema.json` はexampleに存在する次のtop-level sectionをすべて検証する。

```text
schema_version
project
runtime
stack
scope
operations
database
authentication
sheets
playwright
risk
coverage
artifacts
state
```

各objectでは既知キーの型、enum、必須項目を定義する。typoを見逃さないため、原則 `additionalProperties: false` とする。将来拡張が必要な場合は `x-` prefixだけを明示的に許可する。

最低限の静的制約:

- `schema_version` は1。
- `runtime.app.base_url` はURI形式。
- timeout、retention、batch size、lock TTLは正の整数。
- `operations.update`／`delete` は所有権制約を表す既定enumを外れない。
- `database.seed.destructive` はfalse。
- `database.seed.idempotent` はtrue。
- `database.seed.attach_test_run_id` はtrue。
- `database.cleanup.mode` はalways。
- `database.cleanup.scope` はcurrent-test-run-only。
- `database.cleanup.delete_if_ownership_unknown` はfalse。
- shared／unknown DBでは `destructive_reset: false`。
- shared／unknown DBでは `reset_scope` はnoneまたはmanual。
- shared DBではsnapshot restore、既存データ更新、既存データ削除はdeny。
- PlaywrightはChromium、workers 1、retries 1を既定契約として検証する。
- human editable columnsは許可リスト内だけ。
- secret値そのものをconfig exampleへ置かない。

## 2.3 mode／capability semantic validator

CLI:

```bash
python scripts/validate_config.py \
  --config <path> \
  --mode init|discover|test|full|resume|report \
  [--capability base|sheets-read|sheets-write|browser|seed|cleanup|destructive-reset] \
  [--format text|json]
```

既定capability:

```text
init      base
discover  base
test      browser + seed + cleanup
full      browser + seed + cleanup
resume    base。実際のresume phaseを呼ぶ側がcapability指定する
report    sheets-read
```

重要: `discover` のbase検証は、静的解析を許可する。DBガードが不完全な場合は成功結果内に `browser_discovery: BLOCKED` と理由を返す。browser phaseへ移る直前に `--capability browser` を別途通す。

DB-backed capabilityで必須:

- `expected_host_pattern` が空でない。
- `expected_database_pattern` が空でない。
- 2つのpatternが有効な正規表現。
- `sentinel.required` がtrue。
- `sentinel.query` が空でない。
- `sentinel.expected_value` が未設定または空文字ではない。
- local config candidateが1件以上ある。

このvalidatorは秘密値を読む必要がない。実host／DB名との照合は、後続のruntime preflightが秘密値を出さずに行う。validatorは「安全条件が設定済みか」を検査する。

`destructive-reset` capabilityでは追加で必須:

- classificationがdedicated。
- destructive_resetがtrue。
- reset_scopeがsuiteまたはmanual。
- reset_commandが空でない。
- runtime explicit confirmationを別レイヤーで要求することを結果へ示す。

## 2.4 Google Sheets schema meta-validation

`google-sheets-schema.schema.json` を新設し、`google-sheets-schema.json` 自体を検証する。

追加のsemantic check:

- tab名が一意。
- 必須tabが存在する。
- 各tabのcolumn名が一意。
- column tupleが厳密に4要素である。
- tupleは `[name, type, required, human_editable]`。
- nameは空でない文字列。
- typeは定義済みの型集合だけ。
- requiredとhuman_editableはboolean。
- append-only tabの主キー／attempt識別列が存在する。
- human editable列がconfig exampleの許可リストと矛盾しない。
- Metadataにschema versionとlock情報を保持できる列がある。
- unknownなwrite policy、row policyを拒否する。

v0.2ではtuple形式を維持し、validatorで破損を防ぐ。object形式へのmigrationは別変更とする。

CLI:

```bash
python scripts/validate_sheets_schema.py \
  --schema skills/webapp-debug/assets/google-sheets-schema.json \
  [--format text|json]
```

## Phase 2テスト

Config fixture:

- example configが成功。
- project、runtime.app、operations、authentication、artifacts、stateの欠落が失敗。
- unknown keyが失敗。
- shared DB + destructive resetが失敗。
- empty host pattern、empty database pattern、empty sentinel queryがbaseでは診断され、browser/testではBLOCKED。
- invalid regexが失敗。
- discover baseは静的解析ready、browser blockedを返す。
- reportはDBガード未設定でも成功。
- JSON出力にfixtureのsecret markerが残らない。

Sheets schema fixture:

- 正常schemaが成功。
- duplicate tab、duplicate column、tuple長不正、型不正が失敗。
- 必須tab欠落、append-only識別列欠落が失敗。
- human editable列の不整合が失敗。

## Phase 2受け入れ条件

- config example全体がschema対象。
- mode／capability固有のDBガードが決定的に判定される。
- Sheets schemaの壊れた変更をCIで検出できる。
- validatorはネットワークへアクセスしない。
- 全エラー出力がredaction済み。

推奨commit境界:

```text
feat: add deterministic config and sheets schema validation
```

---

# Phase 3: Google Sheets init、協調ロック、WAL、redaction

## 目的

失敗時の影響が大きいSheets初期化と状態管理を、dry-run、idempotency、redaction付きのコードへ落とす。

## 変更対象

```text
scripts/init_sheets.py
scripts/redact_artifact.py
src/webapp_debug_skill/sheets_client.py
src/webapp_debug_skill/sheets_lock.py
src/webapp_debug_skill/wal.py
src/webapp_debug_skill/redaction.py
tests/unit/test_init_sheets.py
tests/unit/test_sheets_lock.py
tests/unit/test_wal.py
tests/unit/test_redaction.py
tests/integration/test_google_sheets_opt_in.py
```

## 3.1 `init_sheets.py`

CLI:

```bash
python scripts/init_sheets.py \
  --config .webapp-debug/config.yml \
  --schema skills/webapp-debug/assets/google-sheets-schema.json \
  [--create] \
  [--write-config] \
  [--dry-run] \
  [--format text|json]
```

処理順序:

1. config schemaとSheets schemaを検証する。
2. service account credentialの環境変数名だけを確認する。credential pathや内容を表示しない。
3. dry-runでは予定操作を計算して終了する。API write、WAL作成、config更新をしない。
4. spreadsheet IDが空の場合、`--create` がなければBLOCKED。
5. `--create` はinitモードでのみ許可する。
6. 既存Spreadsheetでは協調ロックを取得する。
7. 現在のtab、header、Metadata schema versionを読む。
8. mutation planを作る。
9. redaction済みmutationをWALへappendしfsyncする。
10. Google Sheets APIのbatch updateを実行する。
11. read-backで適用結果を検証する。
12. WAL entryをacknowledgedにする。
13. owner tokenが一致する場合だけlockを解放する。

Idempotency:

- 同じschemaで2回実行した場合、2回目のmutationは0件。
- 不足tabは作成する。
- 不足headerは、既存canonical orderを壊さない場合だけ追加する。
- unknown tabを削除しない。
- unknown columnを削除しない。
- canonical columnの順序が競合する場合は、黙って並べ替えず `SHEETS_SCHEMA_CONFLICT` で停止する。
- human editable cellの値を上書きしない。
- schema version downgradeを自動適用しない。

新規Spreadsheetを作成した場合:

- IDをstdoutへ表示できるが、credential情報は表示しない。
- `--write-config` がない限りconfigを変更しない。
- config更新時はbackupを作り、atomic replaceする。

## 3.2 協調ロック

Metadata tabに次を保持する。

```text
writer_lock_owner
writer_lock_run_id
writer_lock_acquired_at
writer_lock_expires_at
writer_lock_commit_sha
```

取得手順:

1. 現在lockを読む。
2. 未期限切れlockがあればexit 5。
3. ランダムowner tokenと期限を書き込む。
4. read-backし、自分のtokenと一致しなければexit 5。

解放手順:

- owner tokenが一致する場合だけclearする。
- 不一致なら他実行のlockを消さない。
- reportモードはlockを取得しない。
- lock TTLはconfigから取得する。

READMEとコードコメントでは、これを「単一ライター前提の協調ロック」と表現し、完全な分散排他を保証すると記載しない。

## 3.3 WAL

保存先:

```text
.webapp-debug/state/wal/<run_id>.jsonl
```

各entryの最低項目:

```json
{
  "schema_version": 1,
  "sequence": 1,
  "operation_id": "uuid",
  "run_id": "...",
  "operation": "sheets.batch_update",
  "payload_hash": "sha256:...",
  "payload": {},
  "created_at": "...",
  "status": "pending"
}
```

規則:

- payloadはGoogle Sheetsへ送る最終的なredaction済み値だけを含む。
- raw secret、cookie、authorization、DB接続情報を保存しない。
- append後にflush／fsyncしてから外部mutationを行う。
- ackは同一operation_idに対する追記entryとして記録し、過去行を書き換えない。
- 不完全な最終entry、重複operation、hash不一致を検出する。
- replayはidempotency keyとread-back結果を使い、適用済みmutationを無条件に再送しない。
- WAL自体をGoogle Sheetsより正準としない。

## 3.4 `redact_artifact.py`

CLI:

```bash
python scripts/redact_artifact.py \
  --input <path> \
  --output <path> \
  [--secret-env NAME ...] \
  [--format auto|text|json|jsonl|yaml|har] \
  [--force] \
  [--report <path>] \
  [--format-output text|json]
```

対応範囲:

- UTF-8 text、JSON、JSONL、YAML、HAR、HTTP header/log。
- key名ベースでpassword、passwd、secret、token、authorization、cookie、set-cookie、api_key、private_key、client_secret、dsnをredact。
- URL userinfoと既知のsecret query parameterをredact。
- `--secret-env` で指定された環境変数の値をメモリ内で置換する。値を表示しない。
- 置換表現は `<REDACTED:TYPE>` のように種類だけを示す。
- 入力を既定でin-place更新しない。
- 出力先が存在する場合は `--force` がなければ停止する。

fail-closed対象:

- screenshot、video、PDF、任意binary、Playwright trace zipはv0.2で安全にredactできると扱わない。
- unsupported formatでは出力を作らずexit 3。
- binary artifactはローカル保持のみとし、外部出力可能と表示しない。

## 3.5 Google APIテスト境界

- 単体テストではfake Sheets backendを使う。
- 実Google Sheets統合テストは `WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION=1` がある場合だけ実行する。
- 統合テスト用Spreadsheet IDとcredentialは環境変数から取得する。
- CI既定では統合テストをskipする。

## Phase 3テスト

- dry-runでAPI write、WAL、config変更が0件。
- 新規作成は `--create` なしでBLOCKED。
- 同じschemaを2回適用して2回目mutation 0件。
- unknown tab／unknown trailing columnを保持。
- canonical header順序競合で停止。
- human editable valueを保持。
- lock競合、期限切れlock、read-back token不一致、owner不一致releaseを検証。
- WAL途中失敗、hash不一致、重複operation、ack済み再開を検証。
- redaction後にsecret markerがstdout、stderr、output、reportへ残らない。
- unsupported binaryで出力が作られない。

## Phase 3受け入れ条件

- Sheets initがdry-run可能でidempotent。
- 未知列と人間編集列を破壊しない。
- lock／WALがコード化され、失敗時に安全停止する。
- redaction不能artifactを安全済みと誤表示しない。
- 実credentialなしで全unit testが成功する。

推奨commit境界:

```text
feat: add safe sheets initialization and state controls
```

---

# Phase 4: Bounded discoveryとcoverage gate

## 目的

「全InventoryがMAPPEDになるまで反復」を上限なしのloopにせず、strictまたは明示的risk thresholdでtest phaseへの移行を判定する。

## 変更対象

```text
skills/webapp-debug/SKILL.md
skills/webapp-debug/references/workflow.md
skills/webapp-debug/references/discovery.md
skills/webapp-debug/references/status-model.md
skills/webapp-debug/assets/webapp-debug.config.example.yml
skills/webapp-debug/assets/config.schema.json
scripts/evaluate_coverage.py
src/webapp_debug_skill/coverage.py
tests/unit/test_coverage.py
README.md
DECISIONS.md
```

## 4.1 設定

exampleへ次を追加する。

```yaml
coverage:
  mode: strict # strict | risk-gated
  max_discovery_passes: 3
  minimum_inventory_closure_percent: 100
  maximum_open_discovery_gaps: 0
  block_open_gap_risks:
    - CRITICAL
    - HIGH
    - MEDIUM
    - LOW
```

strictは従来の完了条件を維持する。

risk-gatedの例:

```yaml
coverage:
  mode: risk-gated
  max_discovery_passes: 3
  minimum_inventory_closure_percent: 90
  maximum_open_discovery_gaps: 100
  block_open_gap_risks:
    - CRITICAL
    - HIGH
```

risk-gatedは明示設定時だけ有効とし、暗黙にstrictから緩和しない。

## 4.2 指標定義

```text
inventory_total = 除外前の全Inventory行
closed = MAPPED + EXCLUDED_WITH_REASON
closure_percent = closed / inventory_total * 100
open_gaps = DISCOVERY_GAPまたは未終端status
```

- totalが0の場合は100%とせず `NO_INVENTORY` でBLOCKED。
- RETIRED／MERGEDの扱いを明示し、二重計上しない。
- risk別にtotal、closed、open gapsを集計する。

## 4.3 移行判定

strict:

- closure 100%。
- open gap 0件。
- すべて `MAPPED` または `EXCLUDED_WITH_REASON`。

risk-gated:

- closureがminimum以上。
- open gap件数がmaximum以下。
- `block_open_gap_risks` に該当するopen gapが0件。

共通:

- discovery passは `max_discovery_passes` まで。
- 各passで新規／更新Inventory件数を記録する。
- gate未達でも無限に続けず、未達理由を出して停止する。
- risk-gatedでtestへ移行しても、残存gapを消さない。
- risk-gated達成を「全機能網羅」「coverage 100%」と表示しない。

## 4.4 `evaluate_coverage.py`

CLI:

```bash
python scripts/evaluate_coverage.py \
  --config .webapp-debug/config.yml \
  (--inventory-json <path> | --from-sheets) \
  [--format text|json]
```

出力:

- transition_allowed
- policy mode
- total、closed、closure percent
- gap count by risk
- blocking reason codes
- current pass／max pass

純粋関数 `evaluate_inventory(rows, policy)` を分離し、Sheetsなしでunit testする。

## 4.5 SKILLの修正

`discover`:

- 「全Inventoryが閉じるまで反復」を「最大pass数まで反復し、各pass後にcoverage gateを評価」へ変更。

`full`:

- strict gate達成、または明示的risk-gated達成時にtestへ移行。
- gate未達時はtestへ移らず、理由付きで終了。

`report`:

- raw closure、policy達成、risk別gapを別表示。
- threshold達成と100% coverageを混同しない。

## Phase 4テスト

- strict 100%で移行可。
- strictで1件gapがあれば不可。
- risk-gatedでLOW gapだけ残り、blocking riskがHIGH以上なら移行可。
- CRITICAL／HIGH gapが残れば不可。
- closure percent不足、gap件数超過、inventory 0件が不可。
- RETIRED／MERGEDを二重計上しない。
- max pass到達時にloopせず理由を返す。

## Phase 4受け入れ条件

- discovery loopが有界。
- strictの従来契約を維持。
- risk-gatedは明示設定時だけ使用。
- 残存gapがreportとSheetsから消えない。
- threshold達成を100%と表現しない。

推奨commit境界:

```text
feat: add bounded discovery and risk-based coverage gates
```

---

# Phase 5: CI、ドキュメント、v0.2 release readiness

## 目的

新しい決定的処理を継続的に検証し、READMEの実装状況を現実と一致させる。

## 変更対象

```text
.github/workflows/ci.yml
README.md
INSTALL.md
DECISIONS.md
CHANGELOG.md
skills/webapp-debug/references/*.md
```

## 5.1 CI

pull requestとmain pushで、少なくとも次を実行する。

```bash
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python scripts/validate_skill.py --root .
python scripts/validate_sheets_schema.py \
  --schema skills/webapp-debug/assets/google-sheets-schema.json
python scripts/validate_config.py \
  --config skills/webapp-debug/assets/webapp-debug.config.example.yml \
  --mode init
```

CIでは実Google Sheets、実DB、ブラウザE2Eを実行しない。

## 5.2 README／INSTALL

READMEから「同期CLIを同梱していない」という記述を削除し、実装済みと未実装を正確に分ける。

実装済みとして記載可能:

- Skill metadata validator
- config validator
- Sheets schema validator
- safe Sheets initializer
- cooperative lock
- WAL
- textual artifact redaction
- coverage evaluator

未実装として明記:

- CakePHP AST discovery engine
- complete test generator／runner orchestration
- binary artifact PII redaction
- strong distributed locking

INSTALLには次を含める。

- Python version
- dependency installation
- service account environment variable
- dry-run firstの手順
- config validation
- Sheets schema validation
- init dry-run
- integration test opt-in

## 5.3 DECISIONS／CHANGELOG

DECISIONSへ次を追記する。

- Codex canonical frontmatterはname／descriptionのみ。
- Claude固有frontmatterはClaude wrapperだけ。
- v0.2 lockはcooperative。
- WALはredaction済みSheets payloadだけを保持。
- binary artifactはfail-closed。
- coverageはstrict既定、risk-gatedは明示設定。

CHANGELOGにv0.2のAdded／Changed／Security／Known limitationsを記載する。リリース日が未確定なら `Unreleased` とする。

## Phase 5受け入れ条件

- CIが全unit testとvalidatorを実行する。
- READMEと実ファイルが一致する。
- 未実装機能を実装済みと表現していない。
- fresh cloneからINSTALL手順を再現できる。
- secret fixtureを使ったテストがCIで成功する。

推奨commit境界:

```text
docs: finalize v0.2 hardening workflow and CI
```

---

# 6. Codexへ渡すフェーズ別プロンプト

## Phase 1

```text
AGENTS.mdとdocs/IMPLEMENTATION_PLAN.mdを読み、Phase 1だけを実装してください。
Phase 2以降を先行実装しないでください。
実装後、Phase 1に定義されたvalidatorとtestを実行し、変更ファイル、結果、未解決事項を報告してください。
commit／pushは行わないでください。
```

## Phase 2

```text
AGENTS.mdとdocs/IMPLEMENTATION_PLAN.mdを読み、Phase 2だけを実装してください。
Phase 1が受け入れ条件を満たしていることを最初に確認してください。
実Google SheetsやDBへ接続せず、config／Sheets schema validatorとunit testを実装してください。
実装後、Phase 2の全検証を実行してください。commit／pushは行わないでください。
```

## Phase 3

```text
AGENTS.mdとdocs/IMPLEMENTATION_PLAN.mdを読み、Phase 3だけを実装してください。
Phase 2のvalidatorを必ず再利用し、検証前にGoogle API writeを行わないでください。
Google APIはfake clientでunit testし、実integration testはopt-inにしてください。
実装後、dry-run、idempotency、lock、WAL、redactionの拒否系を含む全テストを実行してください。
commit／pushは行わないでください。
```

## Phase 4

```text
AGENTS.mdとdocs/IMPLEMENTATION_PLAN.mdを読み、Phase 4だけを実装してください。
strictの既存完了条件を維持し、risk-gatedは明示設定時だけ有効にしてください。
DISCOVERY_GAPを削除・隠蔽せず、loopをmax_discovery_passesで必ず終了させてください。
実装後、coverage判定のunit testを実行してください。commit／pushは行わないでください。
```

## Phase 5

```text
AGENTS.mdとdocs/IMPLEMENTATION_PLAN.mdを読み、Phase 5だけを実装してください。
README、INSTALL、DECISIONS、CHANGELOG、CIを実装状況と一致させてください。
実Google Sheets、DB、E2EはCIで実行しないでください。
全validator、unit test、lintを実行し、最終結果を報告してください。commit／pushは行わないでください。
```

# 7. 最終受け入れマトリクス

| 要求 | 検証方法 | 期待結果 |
|---|---|---|
| Codex frontmatter互換 | `validate_skill.py` | canonical／Codexは2キーのみ |
| Claude手動起動 | Claude wrapper検査 | `disable-model-invocation: true` |
| 初回導線 | openai.yaml検査 | default promptがinit |
| config全体検証 | `validate_config.py` | template全sectionがschema対象 |
| DBガード | capability test | 空pattern／sentinelでBLOCKED |
| Sheets schema破損検出 | `validate_sheets_schema.py` | duplicate／tuple破損を拒否 |
| Sheets init安全性 | fake backend test | dry-run、idempotent、unknown保持 |
| lock | unit test | conflict／owner不一致を拒否 |
| WAL | unit test | fsync前提、hash／resume検査 |
| redaction | secret fixture test | どの出力にもsecretなし |
| binary fail-closed | artifact test | outputなし、exit 3 |
| bounded discovery | coverage test | max passで終了 |
| risk gate | coverage test | blocking risk gapを拒否 |
| 継続的検証 | GitHub Actions | PRで全validator／unit test成功 |

# 8. 参照仕様

- Codex Agent Skills: `https://developers.openai.com/codex/skills`
- Codex AGENTS.md: `https://developers.openai.com/codex/guides/agents-md`
- Claude Code Skills: `https://docs.anthropic.com/en/docs/claude-code/skills`
- JSON Schema Draft 2020-12: `https://json-schema.org/draft/2020-12/schema`

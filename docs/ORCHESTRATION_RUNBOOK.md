# Orchestration Runbook for Phase 6C+

このrunbookは、親CodexがPhase 6C以降をsubagentレビュー付きで進めるための運用手順である。実装Phaseでは常に1Phaseだけを対象にする。

## Start-of-run Protocol

1. `git status --short --branch` を確認する。
2. 作業ツリーがdirtyなら、差分概要を報告して停止する。既存差分を上書きしない。
3. `git rev-parse HEAD origin/main` または対象branchの期待HEADを確認する。
4. 次を読む。
   - `AGENTS.md`
   - `docs/IMPLEMENTATION_PLAN.md`
   - `docs/MASTER_IMPLEMENTATION_PLAN.md`
   - `docs/PHASE_ACCEPTANCE_CRITERIA.md`
   - `README.md`
   - `skills/webapp-debug/SKILL.md`
   - 対象Phaseに対応する `skills/webapp-debug/references/` と `assets/`
5. 対象Phaseのobjective、non-goals、safety constraints、acceptance criteriaを要約してから作業する。

## Subagent Policy

Subagent使用は許可するが、役割を明確にする。

- 計画・レビューrunでは、subagentはファイルを変更しない。
- 実装runでは、subagentに実装を任せる場合でも1Phase内に限定し、write scopeを分離する。
- 親Codexはsubagent結果を統合し、重複・矛盾・scope外提案を整理する。
- subagentの提案はそのまま採用しない。`AGENTS.md`、implementation plan、既存コードの契約と照合する。

推奨レビュー役割:

- Architecture reviewer: module境界、依存順序、重複実装、既存APIとの接続点。
- Safety reviewer: destructive operation、secret leakage、credential、DB、Google Sheets write、WAL、lock。
- Sheets / WAL / lock reviewer: row mutation、payload、read-back、resume、ambiguous write。
- Playwright / Scenario reviewer: Scenario schema、generated code、runner、artifact handling。
- Docs / CI / release reviewer: docs consistency、version scope、CI、release readiness。

## Parent Codex Workflow

1. 対象Phaseを1つ選ぶ。
2. common blockerを確認する。
3. 必要なら5役割のsubagent reviewを並列実行する。
4. 親Codexは直接、対象Phaseのcritical pathを調査する。
5. subagent結果を待つ。
6. Phase範囲内の実装または文書更新だけを行う。
7. 対象外の提案は「未解決事項」へ回す。
8. Phase固有test、全pytest、ruff、validator、CLI helpを実行する。
9. `git status --short` で変更ファイルを確認する。
10. commit/push/tag/releaseは、ユーザーが明示しない限り行わない。

## Clean Tree Gate

実装Phaseはclean working treeから開始する。dirtyの場合は以下だけ報告して停止する。

- branch
- changed files summary
- untracked files summary
- 実装に進まない理由

計画作成やレビューでも、ユーザーがdirtyなら停止を要求している場合は同じ扱いにする。

## Safety Gates by External System

Google Sheets:

- default unit testsは実Google APIへアクセスしない。
- 実Google API integrationは明示opt-in envを要求する。
- writeはSpreadsheet IDのexact confirmationを要求する。
- lock取得、WAL pending fsync、batch write、read-back、WAL ackの順序をテストする。
- ambiguous writeは自動再送しない。

Database:

- Playwright実行前でもDB接続は原則禁止する。
- runner Phaseでruntime DB safety gateが実装されるまで、mutation ScenarioはBLOCKED扱いにする。
- `reset_scope: manual` は自動reset command実行を意味しない。

Browser / Playwright:

- generator PhaseではPlaywrightを起動しない。
- runner Phaseまでbrowser launch、network、auth state利用を禁止する。
- runner Phaseでもdry-runではbrowser launchしない。
- non-GET、upload/download、email送信、external networkはpolicyでfail-closedにする。

Artifacts:

- text/JSON/YAML/HARはredactionを通す。
- screenshot、video、trace zip、PDF、binaryは自動safe扱いにしない。
- unsafe artifactはlocal-onlyまたは`REDACTION_REVIEW_REQUIRED`にする。

## WAL and Lock Ordering

Sheets writeを行うPhaseは、少なくとも次の順序を守る。

1. config validation
2. Sheets schema validation
3. credential validation
4. Spreadsheet ID confirmation
5. lock acquire
6. fresh state read
7. operation-specific conflict validation
8. WAL pending append and fsync
9. backend mutation
10. read-back / postcondition verification
11. WAL ack append and fsync
12. lock release

禁止順序:

- WAL pending前のGoogle write
- read-back前のWAL ack
- lock未取得のapply write
- unresolved WALがある状態で後続operationへ進むこと
- human editable columnsの上書き

## Verification Command Set

標準:

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

Markdown-only planning runで可能なら実行する:

```bash
python scripts/release_check.py --version 0.2.0
python scripts/validate_skill.py --root .
```

`python` がない環境では、repoの既存venvまたは明示したPython実行ファイルを使い、最終報告にpathとversionを含める。

## Reporting Template

最終報告には次を含める。

- start-of-run git status
- HEAD / origin comparison
- subagentごとの要約
- 実装済み範囲と未実装範囲の変化
- 変更ファイル
- 実行した検証、終了コード、失敗理由
- secret / credential / owner token非漏えい確認
- 実Google API、DB、Playwright、network未使用またはopt-in使用の確認
- open questions
- commit/push/tag/releaseを行っていないこと

## Stop Conditions

次のいずれかがあれば実装を停止する。

- dirty working treeでユーザーが停止を要求している。
- 対象Phase外の実装が必要になる。
- credential、DB接続情報、secret markerが出力に混入する。
- Google write前提が満たされない。
- lock、WAL、read-backの順序を守れない。
- test/validatorが存在しないのに成功扱いで完了しようとしている。

# Phase 6C以降 Master Implementation Plan

この文書は、Phase 6C以降を親Codexがsubagentレビューを使って安全に進めるための長期実装計画である。実装そのものは行わない。各実装runは必ずclean working treeから開始し、1回のCodex runで実装するPhaseは1つだけに限定する。

関連文書:

- [ORCHESTRATION_RUNBOOK.md](ORCHESTRATION_RUNBOOK.md)
- [PHASE_ACCEPTANCE_CRITERIA.md](PHASE_ACCEPTANCE_CRITERIA.md)
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)

## 現在の実装範囲

実装済み:

- Skill metadata validator、config validator、Sheets schema validator
- redaction、append-only WAL、Sheets backend abstraction、cooperative lock
- `init_sheets.py`、Google Sheets adapter、opt-in Google integration tests
- coverage evaluator、read-only Sheets snapshot export
- CakePHP static Inventory discovery
- read-only discovery JSONとsnapshot JSONからのInventory sync plan生成
- Inventory sync planのGoogle Sheets適用CLI
- typed Feature / Story / Scenario domain contract

未実装:

- Feature / Story / Scenario生成とScenario Sheets sync
- Playwright project bootstrap、test generator、runner orchestration
- Test Runs / Evidence / DefectsのSheets反映
- trace/log/sourceからのroot cause analysis
- end-to-end dry-run workflowとv0.3.0 release readiness

## 実装前に解消または明示判断すべきblocker

次はsubagentレビューで見つかった、書き込みPhaseに入る前の停止条件である。今回の計画作成では実装しないが、Phase 6C2以降の実Google Sheets write前に解消、または明示的にscope外として記録する。

- `init_sheets` / bootstrap / resumeで、WAL ack失敗やlock release失敗が成功扱いに見える経路がある可能性。
- ambient `GOOGLE_APPLICATION_CREDENTIALS` を使う例とproject-specific env名の方針が混在している。
- output CLIが `--force` で任意regular fileを上書きできる可能性。
- static discoveryがsymlink経由でrepo外sourceを読む可能性。
- DB guardはconfig completeness中心で、runtime host/database/sentinel照合は未実装。
- text redactionはsecret中心で、email、phone、customer IDなどのPII全般の安全性は未完成。
- future Sheets applyはlocal sync planをそのまま信用せず、lock下でfresh snapshotを再取得して再判定する必要がある。

## 共通方針

- 1回のCodex runで複数Phaseを実装しない。
- subagentはレビュー、調査、または明確に分離された将来の作業単位にのみ使う。今回の計画ではsubagentはファイルを変更しない。
- 実装Phaseは `git status --short --branch` がcleanで、`HEAD == origin/main` または対象ブランチの期待HEADであることを確認して開始する。
- 各Phase終了時は、そのPhase固有testに加え、可能な範囲で全pytest、ruff、formatter check、validator、変更CLIの `--help` を実行する。
- 実Google APIを使うPhaseは必ず明示opt-in envを要求する。
- 実Google Sheets writeを行うPhaseは必ずSpreadsheet IDのexact confirmationを要求する。
- DB接続はPlaywright実行前でも原則禁止する。必要なPhaseでruntime DB safety gateを別途実装するまで実行しない。
- Playwright生成前にScenario schemaとScenario syncを完了させる。
- Playwright実行前に生成コードのdry-run、lint相当、generated file conflict checkを行う。
- Test Runs / DefectsをSheetsへ反映する前にartifact redactionとbinary evidence policyを通す。
- WALはGoogle Sheetsより正準ではない。ambiguous writeではread-back / postconditionで判断し、証明不能ならmanual reconciliationへ止める。

## Phase Dependency Map

```text
6B done
  -> 6C1 row mutation and fake apply substrate
  -> 6C2 opt-in real Inventory apply
  -> 7A Scenario schema/domain model
  -> 7B Scenario sync plan generation
  -> 7C Scenario sync apply
  -> 8A Playwright project bootstrap
  -> 8B Scenario-to-Playwright generator
  -> 8C locator/page object support
  -> 9A runner orchestration
  -> 9B artifact collection and redaction
  -> 9C flaky/retry/status classification
  -> 10A Test Runs sync plan
  -> 10B Defects sync plan
  -> 10C Test Runs/Defects apply
  -> 11A root cause analysis
  -> 11B report aggregation
  -> 11C end-to-end dry-run workflow
  -> 12 v0.3.0 release readiness
```

## Phase 6C1: Inventory Sync Plan Apply Engine, Fake/Unit Only

Objective: Inventory sync planを安全に適用するためのrow mutation基盤、stale snapshot guard、WAL/read-back契約をfake backend上で定義する。

Inputs: Phase 6B sync plan JSON、read-only Sheets snapshot JSON、canonical Sheets schema、Inventory domain model。

Outputs: Google APIに依存しないInventory apply plan executor、row append/update/retire mutation型、fake backend tests、operation-specific reconciliation contract。

Files likely to change: `src/webapp_debug_skill/sheets_client.py`, `src/webapp_debug_skill/inventory_sync.py`, `src/webapp_debug_skill/sheets_snapshot.py`, `src/webapp_debug_skill/wal.py`, `tests/fakes/sheets_backend.py`, unit tests。

Explicit non-goals: 実Google API、credential読込、実Spreadsheet write、新CLIからの外部mutation、Scenario生成、Playwright。

Safety constraints: conflictを含むplanは拒否する。fresh snapshot fingerprint、row coordinate、expected old valuesを検証する。人間編集列と未知列は変更しない。WAL payloadにはsecret、credential、DB情報を含めない。

Required tests: fake backendでappend/update/retire、stale snapshot拒否、expected value mismatch拒否、conflicted plan拒否、unknown column保持、human editable column保持、WAL pending before mutation、read-back before ack、ambiguous write reconciliation、secret marker非漏えい。

Required validators: `python -m pytest -q tests/unit/...`、全pytest、ruff check、ruff format check、既存validator、関連CLI help。

Acceptance criteria: fake/unitのみでInventory applyがidempotentに証明できる。Google writeは0件。read-back不能またはWAL ack不能は成功扱いにしない。

Rollback / stop conditions: planにconflictがある、snapshotがstale、row identityが曖昧、operation-specific reconciliationが未定義、WALにraw payloadが入る。

Dependencies: Phase 6B。

Estimated risk: HIGH。後続の全Sheets writeの基盤になる。

## Phase 6C2: Opt-in Real Google Inventory Apply Integration

Objective: Phase 6C1のexecutorを、明示opt-inされた実Google Sheets integrationでInventory tabへ適用する。

Inputs: conflict-free Inventory sync plan、fresh Sheets snapshot、config、credential env、Spreadsheet ID confirmation。

Outputs: `apply_inventory_sync.py`相当のCLI、opt-in integration tests、read-back verified result。

Files likely to change: `scripts/apply_inventory_sync.py`, `src/webapp_debug_skill/inventory_sync_cli.py`, Google backend row mutation support, integration tests, README/INSTALLの最小更新。

Explicit non-goals: Scenario生成、Playwright、DB接続、Drive API、共有権限変更、batch再送の自動推測。

Safety constraints: opt-in env必須。Spreadsheet ID exact confirmation必須。lock取得後にfresh stateを再読込する。WAL pending fsync前にGoogle writeしない。read-back前にWAL ackしない。ambiguous outcomeは自動再送しない。

Required tests: fake integration、opt-in skip確認、real integrationはenvなしでskip、confirmation不一致、lock競合、WAL failure、batch before/after failure、read-back mismatch、secret marker非漏えい。

Required validators: 全pytest、ruff、format check、validator、`apply_inventory_sync.py --help`、pip check相当。

Acceptance criteria: defaultでは実Google APIへアクセスしない。opt-in時だけInventory applyがread-back verifiedになる。人間編集列、未知列、未知tabを保持する。

Rollback / stop conditions: blocker未解消、credentialがambient、confirmationなし、fresh snapshot未取得、WAL pending失敗、lock未取得、read-back mismatch。

Dependencies: Phase 6C1。

Estimated risk: HIGH。初のPhase 6系実Sheets write。

## Phase 7A: Feature / Story / Scenario Schema and Local Generator Model

Objective: Feature / User Story / Scenarioを機械生成・同期できるdomain schemaへ拡張し、Playwright生成に必要な構造化Scenario contractを定義する。

Inputs: canonical Sheets schema、status model、Scenario reference docs、Inventory rows。

Outputs: Scenario domain dataclasses、status enum validation、Inventory mapping fields、structured steps/assertions/data requirements、local generator model tests。

Files likely to change: `skills/webapp-debug/assets/google-sheets-schema.json`, `src/webapp_debug_skill/scenario_model.py`, `src/webapp_debug_skill/status_model.py`, schema validators, tests。

Explicit non-goals: Sheets write、Playwright code生成、browser実行、DB seed実行。

Safety constraints: proseだけをPlaywright生成の唯一入力にしない。manual override列を保護する。既存Sheets schemaのhuman editable列を壊さない。

Required tests: enum validation、Inventory-to-Scenario many-to-many mapping、structured action/assertion schema、manual field protection、invalid status拒否、secret marker非漏えい。

Required validators: Sheets schema validator、config validator、全pytest、ruff、format check。

Acceptance criteria: Scenario生成とPlaywright生成が依存できる安定したtyped modelがある。既存example schemaがmeta-schemaとdomain validationを通る。

Rollback / stop conditions: Scenario rowsがInventoryへ逆参照できない、statusが自由文字列のまま、structured stepsが未定義。

Dependencies: Phase 6C1。Phase 6C2は必須ではないが、Sheets apply設計と整合させる。

Estimated risk: HIGH。後続生成器の契約を決める。

## Phase 7B: Scenario Sync Plan Generation

Objective: Inventory snapshotとdiscovery outputから、Feature / Story / Scenarioのローカルsync planを生成する。

Inputs: Inventory rows、Scenario rows、Feature/Story rows、Phase 7A model、CakePHP discovery JSON。

Outputs: Scenario sync plan JSON、conflict report、coverage impact summary。

Files likely to change: `scripts/plan_scenario_sync.py`, `src/webapp_debug_skill/scenario_sync.py`, tests, fixtures。

Explicit non-goals: Sheets write、Playwright file生成、browser実行。

Safety constraints: planはローカルartifactのみ。conflictがある場合はexit 3。manual fieldsは更新対象にしない。unknown columnsは保持対象として扱う。

Required tests: new Feature/Story/Scenario plan、existing update、manual override preservation、ambiguous mapping conflict、retire policy、deterministic IDs/fingerprints、text/json output、secret marker非漏えい。

Required validators: 全pytest、ruff、format check、validator、`plan_scenario_sync.py --help`。

Acceptance criteria: Google writeなしでScenario sync planを決定的に生成でき、conflict時はapply不能なplanとして扱う。

Rollback / stop conditions: Inventory mappingが曖昧、Scenario ID allocationが未定義、manual fields更新がplanに含まれる。

Dependencies: Phase 7A。

Estimated risk: MEDIUM。

## Phase 7C: Scenario Sync Apply

Objective: Scenario sync planをSheetsへ安全に適用し、Inventoryのmapped scenario状態を更新する。

Inputs: conflict-free Scenario sync plan、fresh Sheets snapshot、lock、WAL、Spreadsheet ID confirmation。

Outputs: Feature/Stories/Scenarios/Inventoryのread-back verified mutation、resume/reconcile support。

Files likely to change: `scripts/apply_scenario_sync.py`, `src/webapp_debug_skill/scenario_sync.py`, row mutation executor, tests。

Explicit non-goals: Playwright generation、runner、Test Runs/Defects。

Safety constraints: Feature/Story/Scenario更新とInventory mapping更新はWALで順序管理する。manual columnsは保護する。read-back mismatchはexit 5相当で停止する。

Required tests: fake apply、opt-in integration skip、multi-tab atomic batch or ordered batches with WAL、ambiguous outcome、resume applied/unapplied/conflict、manual field preservation、secret marker非漏えい。

Required validators: 全pytest、ruff、format check、validator、CLI help。

Acceptance criteria: Scenario sync後にcoverage evaluatorがInventory mappingを読める。WAL pending/ack順序がテストされる。

Rollback / stop conditions: lock未取得、fresh state mismatch、WAL unresolved、partial mapping、human editable column mutation。

Dependencies: Phase 7B、Phase 6C1。Phase 6C2の実Google integration patternを再利用する。

Estimated risk: HIGH。

## Phase 8A: Playwright Project Bootstrap / Static Test Skeleton Generator

Objective: Playwright用project layout、generated file policy、config/template、dry-run validationを実装する。

Inputs: Scenario model、test policy、existing repo files、Playwright asset examples。

Outputs: bootstrap plan、static skeleton generator、generated manifest/checksum policy、no-run validation。

Files likely to change: `scripts/bootstrap_playwright_project.py`, `src/webapp_debug_skill/playwright_project.py`, assets/templates, tests, docs。

Explicit non-goals: Playwright起動、npm install、browser launch、DB接続、Scenario-to-test full generation。

Safety constraints: 既存app/test fileを暗黙に上書きしない。generated markerとmanifest/checksumで所有権を確認する。package managerとlockfile policyを尊重し、複数lock形式を導入しない。

Required tests: dry-run、existing file conflict、generated file safe overwrite、manifest mismatch拒否、package boundary detection、text/json output、secret marker非漏えい。

Required validators: 全pytest、ruff、format check、validator、CLI help。npm/composer/Playwrightは実行しない。

Acceptance criteria: Playwrightを実行せず、生成予定ファイルと安全性を決定的に検証できる。

Rollback / stop conditions: project boundary不明、lockfile競合、non-generated file上書き、auth state path混入。

Dependencies: Phase 7A。

Estimated risk: MEDIUM。

## Phase 8B: Scenario-to-Playwright Test Generation

Objective: 構造化ScenarioからPlaywright test skeletonを生成し、実行前のstatic validationまで行う。

Inputs: Phase 7A Scenario model、Phase 8A project contract、locator candidates、test policy。

Outputs: generated `.spec` files、fixtures usage、generation manifest、Scenario generation status plan。

Files likely to change: `scripts/generate_playwright_tests.py`, `src/webapp_debug_skill/playwright_generator.py`, templates, tests。

Explicit non-goals: browser実行、DB mutation、Sheets apply、trace collection。

Safety constraints: prose再解釈に依存しない。unsafe/mutation ScenarioはDB safety gate未実装ならBLOCKED生成にする。generated file ownershipを検証する。

Required tests: deterministic generation、structured steps/actions/assertions、unsupported action BLOCKED、manifest conflict、secret marker非漏えい、lint-like static checks。

Required validators: 全pytest、ruff、format check、validator、CLI help。Playwright起動は禁止。

Acceptance criteria: 生成コードはdry-run/static validationを通り、実行前に危険ScenarioをBLOCKEDにできる。

Rollback / stop conditions: locator source不足、structured steps不足、generated file conflict、unsafe DB requirement。

Dependencies: Phase 8A、Phase 7A。

Estimated risk: HIGH。

## Phase 8C: Locator Strategy / Page Object Support

Objective: locator候補、page object候補、安定性score、手動overrideを扱う支援層を追加する。

Inputs: Scenario model、test policy、static discovery hints、将来のpassive browser discovery output。

Outputs: locator candidate model、page object template、manual override preservation、generation status integration。

Files likely to change: `src/webapp_debug_skill/locator_model.py`, `src/webapp_debug_skill/playwright_generator.py`, Scenario schema/tests。

Explicit non-goals: dynamic browser discovery実行、Playwright runner、network capture。

Safety constraints: locator不確実性を隠さない。低confidence locatorはreview requiredにする。manual locator overrideを上書きしない。

Required tests: role/label/testid priority、low-confidence blocking、manual override preservation、page object generation conflicts、secret marker非漏えい。

Required validators: 全pytest、ruff、format check、validator、CLI help。

Acceptance criteria: generated testがlocator confidenceを明示し、不安定な候補で自動実行可能と誤表示しない。

Rollback / stop conditions: locator候補がないのに実行可能扱い、manual override破壊、page object ownership不明。

Dependencies: Phase 8B。

Estimated risk: MEDIUM。

## Phase 9A: Playwright Runner Orchestration

Objective: Playwright runnerを安全に起動するためのpreflight、DB/runtime guard、auth state policy、network/browser safety gateを実装する。

Inputs: generated tests、config、DB safety references、test policy、auth state settings。

Outputs: runner preflight CLI、execution plan、BLOCKED reason codes、no-execution dry-run。

Files likely to change: `scripts/run_playwright_tests.py`, `src/webapp_debug_skill/playwright_runner.py`, config validation, tests。

Explicit non-goals: artifact upload、Sheets Test Runs write、root cause analysis。

Safety constraints: DB runtime guard未通過なら実行しない。non-GET, email, upload/download, auth state expiry, external networkをpolicyで制御する。dry-runではPlaywrightを起動しない。

Required tests: dry-run no browser、DB guard missing BLOCKED、auth state unsafe BLOCKED、network policy BLOCKED、generated code validation、secret marker非漏えい。

Required validators: 全pytest、ruff、format check、validator、CLI help。実Playwright起動は専用opt-in integrationだけ。

Acceptance criteria: runnerは危険条件を実行前にfail-closedできる。default unit testsではbrowser/network/DBに触れない。

Rollback / stop conditions: DB sentinel未確認、auth state不明、external network不明、generated code validation失敗。

Dependencies: Phase 8B、Phase 8C。

Estimated risk: HIGH。

## Phase 9B: Artifact Collection and Redaction

Objective: Playwright result、stdout/stderr、trace/video/screenshot等のartifactを収集し、redaction可否と外部出力可否を分類する。

Inputs: Playwright runner output、existing redaction module、security redaction policy。

Outputs: artifact manifest、redacted text artifacts、binary artifact classification、Evidence-ready local references。

Files likely to change: `src/webapp_debug_skill/artifacts.py`, `scripts/redact_artifact.py` extensions if needed, tests。

Explicit non-goals: binary image/video/traceの完全PIIマスク、artifact upload、Sheets write。

Safety constraints: unsupported binaryは外部共有不可。PII redaction不能なら`REDACTION_REVIEW_REQUIRED`。credential、cookie、authorization、DB情報を出さない。

Required tests: text/json/html/log redaction、unsupported screenshot/video/trace zip local-only、manifest safe paths、secret marker非漏えい、partial outputなし。

Required validators: 全pytest、ruff、format check、validator、CLI help。

Acceptance criteria: Test Runs/Defects sync前にartifactの安全状態を判定できる。

Rollback / stop conditions: binaryをsafe扱い、raw traceを外部URL化、redaction failure後にpartial output残存。

Dependencies: Phase 9A。

Estimated risk: HIGH。

## Phase 9C: Flaky / Retry / Status Classification

Objective: Playwright resultをattempt/retry単位で分類し、Scenario latest statusやTest Runs planに渡せるstatus modelを作る。

Inputs: Playwright JSON result、artifact manifest、status model。

Outputs: run classification JSON、FLAKY/BLOCKED/FAILED/PASSED分類、Evidence linking model。

Files likely to change: `src/webapp_debug_skill/test_result_model.py`, `src/webapp_debug_skill/playwright_results.py`, tests。

Explicit non-goals: Sheets write、Defect creation、root cause analysis。

Safety constraints: retryで最終passした失敗を隠さない。artifact redaction未完了ならSheets反映可能にしない。

Required tests: pass/fail/flaky/blocked/timeout、retry attempts、evidence IDs、malformed result拒否、secret marker非漏えい。

Required validators: 全pytest、ruff、format check、validator。

Acceptance criteria: Sheets writeなしでTest Runs/Defects plannerに渡せる安全なclassificationを生成できる。

Rollback / stop conditions: retry failureを捨てる、artifact unsafeでもsync可能扱い、unknown statusをpass扱い。

Dependencies: Phase 9B。

Estimated risk: MEDIUM。

## Phase 10A: Test Runs Sync Plan

Objective: classified test resultからTest Runs / Evidence append planを生成する。

Inputs: run classification、artifact manifest、Sheets snapshot、Scenario rows。

Outputs: Test Runs sync plan、Evidence append plan、Scenario latest status update plan。

Files likely to change: `scripts/plan_test_runs_sync.py`, `src/webapp_debug_skill/test_runs_sync.py`, tests。

Explicit non-goals: Sheets write、Defects upsert、artifact upload。

Safety constraints: Test Runs/Evidenceはappend-only idempotency keyを持つ。unsafe artifactはexternal linkにしない。Scenario manual fieldsを変更しない。

Required tests: append plan、idempotency duplicate detection、Evidence link safety、Scenario status plan、redaction required blocking、secret marker非漏えい。

Required validators: 全pytest、ruff、format check、validator、CLI help。

Acceptance criteria: Google writeなしでTest Runs/Evidence sync planが生成できる。

Rollback / stop conditions: attempt_idなし、unsafe artifact、Scenario identity mismatch、duplicate idempotency key。

Dependencies: Phase 9C。

Estimated risk: MEDIUM。

## Phase 10B: Defects Sync Plan

Objective: failed/blocked resultsからDefects upsert planを生成し、manual fieldsを保護する。

Inputs: run classification、existing Defects snapshot、Scenario rows、error/root cause hints if available。

Outputs: Defects upsert plan、conflict report、manual field protection report。

Files likely to change: `scripts/plan_defects_sync.py`, `src/webapp_debug_skill/defects_sync.py`, tests。

Explicit non-goals: Sheets write、RCA生成、issue tracker連携。

Safety constraints: defect ID allocationとdedupe keyを明示する。manual priority/owner/notesを上書きしない。raw error bodyを出力しない。

Required tests: new defect、existing update、manual field preservation、duplicate detection、redacted error details、secret marker非漏えい。

Required validators: 全pytest、ruff、format check、validator、CLI help。

Acceptance criteria: Defects apply前に安全なupsert planとconflictを区別できる。

Rollback / stop conditions: manual field mutation、raw stack/body leakage、dedupe key不明。

Dependencies: Phase 9C。

Estimated risk: MEDIUM。

## Phase 10C: Apply Test Runs / Defects to Sheets

Objective: Test Runs、Evidence、Defects、Scenario latest statusをWAL/lock/read-back付きでSheetsへ反映する。

Inputs: Phase 10A/B plans、fresh Sheets snapshot、Spreadsheet ID confirmation、lock、WAL。

Outputs: read-back verified Sheets mutation、resume/reconcile support。

Files likely to change: `scripts/apply_test_results_sync.py`, sync executors, Google backend row mutations, tests/integration opt-in。

Explicit non-goals: Playwright execution、artifact upload、RCA generation。

Safety constraints: Test Runs/Evidence append-only、Defects upsert、Scenario latest status updateをoperation-specific reconciliationで扱う。WAL pending前にwriteしない。read-back前にackしない。

Required tests: fake apply、append idempotency、upsert read-back、ambiguous outcome、resume applied/unapplied/conflict、lock conflict、secret marker非漏えい、opt-in integration skip。

Required validators: 全pytest、ruff、format check、validator、CLI help、pip check相当。

Acceptance criteria: defaultでは実Google APIなし。opt-in時だけsafe confirmed Spreadsheetへwriteし、read-back verified resultを返す。

Rollback / stop conditions: unsafe artifact、WAL unresolved、lock conflict、read-back mismatch、duplicate append identity。

Dependencies: Phase 10A、Phase 10B、Phase 6C2。

Estimated risk: HIGH。

## Phase 11A: Root Cause Analysis from Traces / Logs / Source

Objective: redacted artifacts、logs、source referencesからread-only RCA候補を生成する。

Inputs: artifact manifest、redacted logs、Playwright result classification、source references。

Outputs: RCA candidate JSON、confidence、evidence references。

Files likely to change: `scripts/analyze_root_cause.py`, `src/webapp_debug_skill/root_cause.py`, tests。

Explicit non-goals: source code mutation、DB接続、external API、Sheets write。

Safety constraints: raw artifactやsecretをRCAへ含めない。binary artifactはreview済み参照だけを使う。confidenceを過大表示しない。

Required tests: console/network/source hint analysis、redaction gate、low-confidence output、secret marker非漏えい。

Required validators: 全pytest、ruff、format check、validator、CLI help。

Acceptance criteria: RCAはread-only local artifactとして生成され、Defects updateへ渡せる安全な要約だけを持つ。

Rollback / stop conditions: raw logs leakage、unsupported binary parsing、source mutation、external service use。

Dependencies: Phase 9B、Phase 9C。

Estimated risk: MEDIUM。

## Phase 11B: Report Aggregation

Objective: Inventory coverage、Scenario status、Test Runs、Defects、RCA候補を統合したread-only reportを生成する。

Inputs: snapshots、coverage evaluator output、test result classification、Defects/RCA local artifacts。

Outputs: local report JSON/Markdown、gap summary、release readiness hints。

Files likely to change: `scripts/generate_report.py`, `src/webapp_debug_skill/reporting.py`, tests, docs。

Explicit non-goals: Sheets write、browser execution、artifact upload。

Safety constraints: reportはsafe summariesのみ。credential path、raw config、raw error body、secret markerを含めない。

Required tests: report aggregation、missing data handling、DISCOVERY_GAP visibility、redaction review visibility、secret marker非漏えい。

Required validators: 全pytest、ruff、format check、validator、CLI help。

Acceptance criteria: 未実装/未検証/blocked状態を隠さず、coverageを100%と誤表示しない。

Rollback / stop conditions: gapをpass扱い、unsafe artifact link、raw sensitive details。

Dependencies: Phase 10A/B、Phase 11A。

Estimated risk: MEDIUM。

## Phase 11C: End-to-End Dry-run Workflow

Objective: discoveryからplan、generation、runner preflight、sync plan、reportまでを外部writeなしで通す統合dry-runを実装する。

Inputs: example config、fixtures、fake Sheets snapshot、local artifacts。

Outputs: end-to-end dry-run CLI/result、ordering tests、safety regression suite。

Files likely to change: `scripts/webapp_debug_workflow.py` or existing CLI wrapper, integration-style unit tests, docs。

Explicit non-goals: 実Google write、DB接続、Playwright起動、network。

Safety constraints: dry-runはlocal artifact作成も必要最小限にし、writeするときはtmp/state配下のみ。外部mutationは0件。

Required tests: full dry-run ordering、no Google write、no DB、no browser、no network、secret marker非漏えい、blocked condition propagation。

Required validators: 全pytest、ruff、format check、validator、all changed CLI help、release check dry-run if available。

Acceptance criteria: 人間がv0.3 workflowを外部副作用なしで検証できる。

Rollback / stop conditions: dry-runでcredential refresh、Google write、DB/browser/network、unsafe local overwrite。

Dependencies: Phase 11B。

Estimated risk: HIGH。

## Phase 12: v0.3.0 Release Readiness

Objective: Phase 6C-11Cの実装範囲をv0.3.0として文書、CI、release check、known limitationsへ正確に反映する。

Inputs: implemented CLIs、tests、docs、release checklist、CHANGELOG。

Outputs: v0.3.0 release checklist、release notes、version-scope-aware release check、README/INSTALL updates。

Files likely to change: `CHANGELOG.md`, `README.md`, `INSTALL.md`, `docs/RELEASE_CHECKLIST.md`, `docs/RELEASE_NOTES_v0.3.0.md`, `scripts/release_check.py`, tests。

Explicit non-goals: tag作成、push、GitHub Release、PyPI/Docker publish、new runtime feature。

Safety constraints: 未実装機能を実装済みと書かない。release_checkはversionごとのscopeを区別する。credential/cache/artifactを追跡しない。

Required tests: release_check v0.3.0、doc consistency、CLI help matrix、integration skip、secret/cache file detection。

Required validators: 全pytest、ruff、format check、validator、pip check、release_check、CI equivalent commands。

Acceptance criteria: docs、tests、release checkが実装範囲と一致し、v0.2.0向けcheckをv0.3.0に誤用しない。

Rollback / stop conditions: READMEが未実装を実装済み扱い、CHANGELOG scope不一致、release_check hard-code、tracked secret/cache。

Dependencies: Phase 11C。

Estimated risk: MEDIUM。

## Open Questions

- Phase 6A/6Bをv0.2.0に含めるのか、v0.3.0先行実装として扱うのか。
- Phase 6C2前に、init/bootstrap/resumeのfail-open疑義を独立patchとして必須にするか。
- row mutation基盤を既存`SheetsBackend`へ直接追加するか、schema-aware executor層として分離するか。
- Scenario schemaに構造化stepsを追加する場合、既存Google Sheets schemaの列追加をどのPhaseでmigration扱いにするか。
- Playwright生成/runnerのpackage manager方針を、対象アプリのlockfileに従うだけで足りるか。
- PII redactionの対象範囲をどこまでv0.3.0に含めるか。

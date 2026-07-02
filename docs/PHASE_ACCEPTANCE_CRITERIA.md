# Phase 6C+ Acceptance Criteria

この文書は、Phase 6C以降の完了判定をまとめる。詳細な実装計画は [MASTER_IMPLEMENTATION_PLAN.md](MASTER_IMPLEMENTATION_PLAN.md) を正とする。

## Common Definition of Done

各Phaseは次を満たすまで完了扱いにしない。

- 対象Phaseのobjectiveを満たしている。
- 明示されたnon-goalsを実装していない。
- new behaviorに正常系、境界値、拒否系、秘密情報非漏えいtestがある。
- unit testsはnetwork、実DB、実Google credential、実Google Sheetsに依存しない。
- 外部write Phaseはdry-run、lock、WAL、read-back、resume/reconcileをテストする。
- raw exception、raw config、credential path全文、private key、client email、access token、Cookie、Authorization、DB接続情報を出力しない。
- `pytest`、`ruff check`、`ruff format --check`、validator、変更CLIの `--help` を実行し、結果を報告している。
- README、INSTALL、SKILL、release docsで未実装機能を実装済みと書いていない。
- `docs/IMPLEMENTATION_PLAN.md` や本計画の完了表示は、検証後にだけ更新する。

## Cross-phase Gates

Inventory write gate:

- Phase 6C1がrow mutation、stale snapshot guard、operation-specific reconciliationをfake/unitで証明済み。
- conflictを含むplanをapplyしない。
- fresh snapshotをlock下で再取得してからapplyする。

Scenario gate:

- Phase 7AでScenario schemaがtyped model、status enum、Inventory mapping、structured stepsを持つ。
- proseだけをPlaywright生成入力にしない。

Playwright generation gate:

- Phase 8Aでgenerated file ownership、manifest/checksum、project boundaryが定義済み。
- Phase 8B/8Cでlocator confidenceとunsupported action blockingが実装済み。

Playwright execution gate:

- Phase 9AでDB runtime guard、auth state、network/browser policyがfail-closed。
- dry-runでbrowser launchしない。

Test result Sheets gate:

- Phase 9Bでartifact redaction/classificationが完了。
- Phase 9Cでattempt/retry/status classificationが安全に生成済み。

Release gate:

- Phase 12でversion-scope-aware release check、README/INSTALL/CHANGELOG/release notesが一致。
- tag、push、release publishはユーザー明示なしに行わない。

## Phase Checklist

### Phase 6C1

- Row append/update/retire mutation型がGoogle SDK raw dictなしで表現される。
- Fake backendでall-or-nothing、read-back、ambiguous outcomeを再現できる。
- Inventory sync planにrow coordinate、expected old values、snapshot fingerprintが含まれる。
- WAL pendingがmutation前にfsyncされ、read-back後だけackされる。
- Secret markerがWAL、stdout、stderr、JSON、例外に残らない。

### Phase 6C2

- `apply_inventory_sync.py`または同等CLIに`--dry-run`、`--confirm-spreadsheet-id`、text/JSON出力がある。
- Default testで実Google APIを呼ばない。
- Opt-in integrationだけが実Spreadsheetへwriteする。
- 人間編集列、未知列、未知tabを保持する。
- Lock conflict、WAL failure、read-back mismatchは成功扱いにしない。

### Phase 7A

- Feature / Story / Scenario modelがtypedで、status enumとvalidationを持つ。
- ScenarioにInventory mappingとstructured action/assertion/data requirementsがある。
- Manual override列を自動更新対象にしない。
- Sheets schema validatorとdomain validationが通る。

### Phase 7B

- Scenario sync planをGoogle writeなしで生成できる。
- Conflict時はexit 3相当でapply不能になる。
- ID allocationとfingerprintが決定的。
- Manual fields、unknown columnsを保持するplanになる。

### Phase 7C

- Feature / Stories / Scenarios / Inventory mappingのapplyがWAL/lock/read-back付き。
- Partial/ambiguous stateはresume/reconcileで扱う。
- Inventory coverage evaluatorがmapping後の状態を読める。
- Manual fieldsを破壊しない。

### Phase 8A

- Playwright project bootstrapはdry-runで安全にplanできる。
- Existing non-generated fileを上書きしない。
- Generated filesはmanifest/checksumで所有権確認する。
- npm/composer/Playwrightを実行しない。

### Phase 8B

- Structured Scenarioからdeterministicなtest skeletonを生成する。
- Unsupported actionやunsafe DB requirementはBLOCKED扱い。
- Static validationがあり、実browserは起動しない。
- Generated statusをSheetsへ直接書かない。

### Phase 8C

- Locator candidate modelがconfidenceを持つ。
- Manual locator overrideを保護する。
- Low-confidence locatorはreview requiredまたはBLOCKED。
- Page object generationはownership conflictを検出する。

### Phase 9A

- Runner preflightがDB、auth state、network、browser policyを検査する。
- Dry-runではPlaywrightを起動しない。
- Runtime DB guard未通過なら実行しない。
- External networkやnon-GETのpolicy違反をfail-closedにする。

### Phase 9B

- Artifact manifestがsafe pathとredaction statusを持つ。
- Text artifactsはredaction済み。
- Binary artifactsはunsupported/local-only/review-requiredとして扱う。
- Unsafe artifactをTest Runs/Defectsへ渡さない。

### Phase 9C

- Playwright JSON resultをattempt/retry単位で分類する。
- FLAKY、BLOCKED、FAILED、PASSEDを安全に区別する。
- Evidence IDsとattempt IDsが対応する。
- Unknown statusを成功扱いにしない。

### Phase 10A

- Test Runs/Evidence append planがidempotency keyを持つ。
- Unsafe artifactはexternal link化しない。
- Scenario latest status update planはmanual fieldsを変更しない。
- Duplicate attemptを検出する。

### Phase 10B

- Defects upsert planがdedupe keyとmanual field protectionを持つ。
- Raw error bodyやsecretを出力しない。
- Existing defect updateとnew defect作成を区別する。
- Conflict時はapply不能にする。

### Phase 10C

- Test Runs/Evidence append、Defects upsert、Scenario latest status updateをSheetsへ反映する。
- WAL pending before write、read-back before ackをテストする。
- Ambiguous writeはoperation-specific postconditionで判定する。
- Default testは実Google APIを呼ばない。

### Phase 11A

- RCAはread-only local analysisだけを行う。
- Redacted artifactsだけを入力にする。
- Confidenceとassumptionsを明示する。
- Source、DB、external APIを変更しない。

### Phase 11B

- Reportはcoverage gap、DISCOVERY_GAP、blocked、redaction review requiredを隠さない。
- Raw config、credential、secret、DB情報を含めない。
- 100% coverageとthreshold achievedを混同しない。
- Local-only reportとして生成できる。

### Phase 11C

- End-to-end dry-runでGoogle write、DB、browser、networkが0件。
- Orderingとblocked propagationをテストする。
- Local artifactsは許可されたstate/tmp配下だけ。
- Full workflowを実装済みの範囲でだけ表示する。

### Phase 12

- v0.3.0 docs、CHANGELOG、release notes、release checkが実装範囲と一致する。
- `release_check.py`はversion scopeを区別する。
- CI equivalent commandsが通る。
- tag、push、release publishを自動で行わない。

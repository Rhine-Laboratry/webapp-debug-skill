# 状態モデル

## Inventory discovery_status

```text
NEW
  -> DISCOVERING
  -> MAPPED
  -> EXCLUDED_WITH_REASON
  -> DISCOVERY_GAP
  -> UNREACHABLE
```

完了状態は `MAPPED` と `EXCLUDED_WITH_REASON` のみ。`DISCOVERY_GAP` と `UNREACHABLE` は解消または理由付き除外が必要。

## Scenario lifecycle_status

- `DRAFT`
- `ACTIVE`
- `RETIRED`
- `MERGED`
- `EXCLUDED`

## generation_status

- `PENDING`
- `GENERATING`
- `GENERATED`
- `BLOCKED`
- `NOT_REQUIRED`

## latest_test_status

- `NOT_RUN`
- `RUNNING`
- `PASS`
- `FAIL`
- `BLOCKED`
- `FLAKY`

再試行前に失敗し、再試行で成功した場合は `FLAKY`。両attemptはTest Runsに残す。

## cleanup_status

- `NOT_REQUIRED`
- `PENDING`
- `SUCCEEDED`
- `CLEANUP_BLOCKED`
- `CLEANUP_FAILED`

cleanup異常はテスト結果と別に保持する。ScenarioがPASSでもcleanupが失敗した事実を失わない。

## review_status

- `UNREVIEWED`
- `REVIEWED`
- `CHANGES_REQUESTED`
- `APPROVED`

人間レビューは実行の必須条件ではない。`manual_override` がある場合は自動値より優先する。

## Run phase

- `PREFLIGHT`
- `DISCOVERY_STATIC`
- `DISCOVERY_BROWSER`
- `RECONCILIATION`
- `SCENARIO_GENERATION`
- `TEST_GENERATION`
- `TEST_EXECUTION`
- `ROOT_CAUSE_ANALYSIS`
- `CLEANUP`
- `SYNC`
- `COMPLETED`
- `PAUSED_SYNC_REQUIRED`
- `BLOCKED`

## Reason codes

- `PREFLIGHT_BLOCKED`
- `BLOCKED_UNSUPPORTED_STACK`
- `BLOCKED_DB_GUARD`
- `AUTH_BLOCKED`
- `DISCOVERY_GAP`
- `TEST_GENERATION_BLOCKED`
- `ENVIRONMENT_BLOCKED`
- `RESUME_BLOCKED`
- `PAUSED_SYNC_REQUIRED`
- `CLEANUP_BLOCKED`
- `CLEANUP_FAILED`

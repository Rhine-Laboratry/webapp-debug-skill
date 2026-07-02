# Release Notes: v0.2.0

Target tag: `v0.2.0`

## Positioning

v0.2.0 is a runtime hardening release for the `webapp-debug` Agent Skill. It makes the helper scripts, validation flow, Google Sheets initialization, recovery primitives, coverage gate, CakePHP static discovery, local Inventory sync planning/apply, and CI checks deterministic enough to support later test-generation work.

This release implements read-only CakePHP static Inventory discovery, explicit Inventory sync plan application, and local Scenario sync planning. It does not implement dynamic browser exploration, Scenario/Test Runs/Defects apply, or Playwright Scenario generation.

## Implemented

- Codex / Claude wrapper alignment and Skill metadata validation.
- Config validation for all top-level sections in the example config.
- Google Sheets schema validation.
- Textual artifact redaction for supported UTF-8 text, JSON, JSONL, YAML, and HAR inputs.
- Append-only WAL for redacted Sheets mutations.
- Single-writer cooperative lock for Sheets writes.
- Safe Sheets initialization CLI with dry-run, bootstrap confirmation, WAL, lock, read-back, and config write safeguards.
- Google Sheets adapter with fake-backed unit coverage.
- Opt-in Google integration tests that are skipped unless explicit environment variables are set.
- Bounded coverage gate evaluator with strict and explicit risk-gated modes.
- Read-only Sheets snapshot export for coverage/report JSON input.
- CakePHP static Inventory discovery that writes local JSON snapshots without running PHP, Composer, DB, browser, or Google Sheets operations.
- Local Inventory sync planning from discovery JSON and read-only Sheets snapshot JSON without applying writes.
- Inventory sync plan application to Google Sheets with Spreadsheet ID confirmation, cooperative lock, WAL, and read-back verification.
- Local Scenario sync planning from Inventory/Scenario snapshot JSON without applying writes.
- GitHub Actions CI workflow.
- Release checklist and release readiness self-check.

## Not Implemented

- Dynamic browser discovery and Scenario/Test Runs/Defects Sheets apply are not implemented.
- High-precision CakePHP AST adapters are not implemented.
- JavaScript discovery is not implemented.
- Playwright scenario generation is not implemented.
- Playwright runner orchestration is not implemented.
- Automatic root cause analysis is not implemented.
- Drive API sharing, Spreadsheet deletion, OAuth user flow, and domain-wide delegation are not implemented.
- Screenshot, video, PDF, and trace image PII redaction are not implemented.

## Breaking Changes

None.

## Security Notes

- Do not store service account keys in this repository.
- Real Google integration tests are opt-in only.
- CI does not configure Google credential environment variables or real Spreadsheet IDs.
- This project does not call Drive API sharing flows.
- Spreadsheets created by the service account are not automatically deleted by this project.
- The v0.2 Sheets lock is a single-writer cooperative lock, not a strong distributed lock.

## Verification Commands

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
python scripts/plan_scenario_sync.py --help
python scripts/release_check.py --version 0.2.0
python scripts/release_check.py --version 0.2.0 --format json
```

`python -m pytest tests/integration -q` is expected to skip real Google integration tests unless the opt-in environment variables are set.

## Known Limitations

- Dynamic discovery, Scenario/Test Runs/Defects Sheets apply, JavaScript discovery, and Scenario generation remain future work.
- CI proves the deterministic helper scripts and safety boundaries; it does not run browser E2E or real Google Sheets integration.
- Release automation, PyPI publishing, Docker publishing, and GitHub Release creation are not implemented.

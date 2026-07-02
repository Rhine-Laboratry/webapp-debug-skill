# v0.2 Release Checklist

Use this checklist before tagging or publishing a v0.2 release. It is intentionally local and manual; Phase 5A does not add release automation.

## Local Verification

Run these commands from the repository root with Python 3.11 or newer:

```bash
python -m pip install -e ".[dev]"
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
python scripts/apply_scenario_sync.py --help
python scripts/bootstrap_playwright_project.py --help
python scripts/generate_playwright_tests.py --help
python scripts/release_check.py --version 0.2.0
python scripts/release_check.py --version 0.2.0 --format json
```

`python -m pytest tests/integration -q` should skip real Google integration tests unless explicit opt-in environment variables are set.

## CI Required Checks

The `CI` GitHub Actions workflow must pass for Python 3.11, 3.12, and 3.13. CI runs package checks, unit tests, integration skip confirmation, ruff, format check, validators, CLI help checks, and `scripts/release_check.py --version 0.2.0`.

CI must not set Google credential environment variables, service account key paths, real Spreadsheet IDs, DB connection values, Playwright browser runs, or CakePHP parser commands.

## Secret And Artifact Hygiene

- Confirm no generated credential, `.env`, `.webapp-debug/`, WAL, artifact, Playwright auth state, or service account key file is tracked:

```bash
git ls-files | grep -E '(^|/)(\\.env|\\.webapp-debug|service-account|credentials|wal|auth-state)'
```

- Service account keys must remain outside the repository and should use owner-only filesystem permissions.
- Do not paste private keys, access tokens, client identifiers, Cookie values, Authorization headers, DB passwords, or connection strings into release notes, issues, logs, WAL, or Sheets.

## Optional Google Integration

Real Google integration tests are opt-in only. Use a dedicated test Spreadsheet and service account. Do not use production or shared business Spreadsheets.

When `scripts/init_sheets.py --create` is used, the created Spreadsheet is not automatically shared with users and is not deleted by this project. This project does not call Drive API sharing or deletion flows.

## Tag Readiness

- `git status --short --branch` is clean.
- `python scripts/release_check.py --version 0.2.0` passes locally.
- GitHub Actions `CI` is green for the commit to be tagged.
- README, INSTALL, DECISIONS, CHANGELOG, and this checklist match the implemented behavior.
- `docs/RELEASE_NOTES_v0.2.0.md` exists and matches the release scope.
- `docs/IMPLEMENTATION_PLAN.md` does not mark future phases complete.
- Known limitations still list dynamic browser discovery, Test Runs/Defects Sheets apply, JavaScript parsing, advanced locator/page object support, Playwright runner orchestration, Drive API sharing/deletion, and binary artifact PII redaction as unimplemented.
- Version bump and release automation policy is still undecided unless a later phase records a concrete decision.

Tag creation is manual and must not happen until all checks above are true. Example only:

```bash
git tag -a v0.2.0 -m "v0.2.0"
```

GitHub Release creation is also manual. PyPI and Docker publishing are not supported by v0.2.0 release preparation.

## Release Notes

Summarize:

- Added deterministic validators, redaction, WAL, Sheets initialization, cooperative lock, coverage evaluator, read-only snapshot export, CakePHP static Inventory discovery, local Inventory sync planning/apply, local Scenario sync planning/apply, Playwright project skeleton bootstrap, static Scenario-to-Playwright skeleton generation, and CI.
- Clarified opt-in Google integration test boundaries.
- Repeated that dynamic discovery, Test Runs/Defects Sheets apply, JavaScript parsing, advanced locator/page object support, and Playwright runner orchestration remain future work.

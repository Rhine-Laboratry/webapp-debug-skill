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
```

`python -m pytest tests/integration -q` should skip real Google integration tests unless explicit opt-in environment variables are set.

## CI Required Checks

The `CI` GitHub Actions workflow must pass for Python 3.11, 3.12, and 3.13. CI runs package checks, unit tests, integration skip confirmation, ruff, format check, validators, and CLI help checks.

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
- README, INSTALL, DECISIONS, CHANGELOG, and this checklist match the implemented behavior.
- `docs/IMPLEMENTATION_PLAN.md` does not mark future phases complete.
- Known limitations still list CakePHP discovery, JavaScript parsing, Playwright generation/runner orchestration, Drive API sharing/deletion, and binary artifact PII redaction as unimplemented.
- Version bump and release automation policy is still undecided unless a later phase records a concrete decision.

## Release Notes

Summarize:

- Added deterministic validators, redaction, WAL, Sheets initialization, cooperative lock, coverage evaluator, read-only snapshot export, and CI.
- Clarified opt-in Google integration test boundaries.
- Repeated that CakePHP discovery and Playwright generation remain future work.

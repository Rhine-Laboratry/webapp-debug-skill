# Changelog

All notable changes to this project are recorded here.

## [0.2.0] - Unreleased

### Added

- Codex and Claude Skill metadata compatibility checks.
- Deterministic config validation and Google Sheets schema validation.
- Text, JSON, JSONL, YAML, and HAR redaction for supported textual artifacts.
- Append-only WAL support for redacted Sheets mutations.
- Google SDK-independent Sheets backend abstraction and cooperative lock.
- Safe `scripts/init_sheets.py` initialization flow with dry-run, WAL, lock, and read-back.
- Google Sheets adapter and opt-in real Google integration tests.
- Bounded coverage evaluator with strict and explicit risk-gated modes.
- Read-only Google Sheets snapshot export for coverage/report JSON input.
- CakePHP static Inventory discovery that writes local JSON snapshots without running PHP, Composer, DB, browser, or Google Sheets operations.
- Local Inventory sync planning from discovery JSON and read-only Sheets snapshot JSON without applying Google Sheets writes.
- Inventory sync plan application to Google Sheets with Spreadsheet ID confirmation, cooperative lock, WAL, and read-back verification.
- Local Scenario sync planning from Inventory/Scenario snapshot JSON without applying Google Sheets writes.
- Scenario sync plan application to Google Sheets with Spreadsheet ID confirmation, cooperative lock, WAL, read-back verification, and Inventory mapping updates.
- Playwright project skeleton bootstrap with dry-run and generated-file manifest/checksum ownership checks, without running Playwright, npm, Composer, browser, DB, or Google APIs.
- Static Scenario-to-Playwright test skeleton generation from structured Scenario rows, with BLOCKED status planning for unsupported actions and unsafe runtime data requirements.
- Locator candidate model, confidence gating, and generated page object support with manifest ownership checks.
- Playwright runner preflight with DB guard, auth state path, network allowlist, browser policy, generated manifest/checksum validation, and no-execution dry-run.
- GitHub Actions CI for tests, integration skip confirmation, lint, validators, CLI help, and package checks.
- Release notes draft and `scripts/release_check.py` readiness self-check for the `v0.2.0` target.

### Changed

- README, INSTALL, Skill docs, and implementation plan now distinguish implemented v0.2 hardening helpers, CakePHP static discovery, static Playwright skeleton generation, locator/page object support, and runner preflight from future dynamic discovery and artifact/status work.
- Google integration tests are documented as opt-in and are not part of default CI.
- Package versioning is managed by `pyproject.toml`; `src/webapp_debug_skill/__init__.py` mirrors the same `0.2.0` version.

### Security

- CI does not configure Google credential environment variables or service account keys.
- Unsupported binary artifacts remain fail-closed rather than being marked safe.
- The v0.2 Sheets lock remains a single-writer cooperative lock, not a strong distributed lock.

### Known Limitations

- Dynamic browser discovery, Test Runs/Defects Sheets apply, and high-precision CakePHP AST adapters are not implemented.
- JavaScript parsing is not implemented.
- Playwright artifact collection and status classification are not implemented.
- Drive API sharing, Spreadsheet deletion, OAuth user flow, and domain-wide delegation are not implemented.
- Screenshot, video, PDF, and trace image PII redaction are not implemented.
- Version bump and release automation policy remains undecided.

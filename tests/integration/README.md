# Google Sheets Integration Tests

These tests are opt-in and are skipped by default. They may call the real
Google Sheets API only when all required environment variables are set.

Run existing-spreadsheet integration tests with a test-only spreadsheet:

```bash
WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION=1 \
WEBAPP_DEBUG_GOOGLE_CREDENTIALS_FILE=/path/outside/repo/service-account.json \
WEBAPP_DEBUG_GOOGLE_SPREADSHEET_ID=... \
WEBAPP_DEBUG_GOOGLE_CONFIRM_SPREADSHEET_ID=... \
python -m pytest tests/integration -q
```

Optional create-flow coverage requires additional explicit confirmation:

```bash
WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION=1 \
WEBAPP_DEBUG_GOOGLE_CREDENTIALS_FILE=/path/outside/repo/service-account.json \
WEBAPP_DEBUG_GOOGLE_SPREADSHEET_ID=... \
WEBAPP_DEBUG_GOOGLE_CONFIRM_SPREADSHEET_ID=... \
WEBAPP_DEBUG_GOOGLE_ALLOW_CREATE=1 \
WEBAPP_DEBUG_GOOGLE_CREATE_TITLE="webapp-debug integration test" \
python -m pytest tests/integration -q
```

Rules:

- Use only a dedicated test spreadsheet that the service account can edit.
- Do not use production or shared business spreadsheets.
- Keep the service account JSON outside this repository.
- Do not set credentials through `GOOGLE_APPLICATION_CREDENTIALS` for these tests.
- The tests do not call Drive API, sharing APIs, delete APIs, DBs, or `gcloud`.
- Created spreadsheets are not deleted or shared automatically.

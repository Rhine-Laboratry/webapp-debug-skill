"""Google Sheets API adapter for the SheetsBackend protocol."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from webapp_debug_skill.errors import (
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_POLICY_BLOCKED,
)
from webapp_debug_skill.redaction import secret_findings
from webapp_debug_skill.sheets_client import (
    AddHeaders,
    BatchResult,
    ClearMetadata,
    CreateTab,
    InitialSheetSpec,
    Mutation,
    SetMetadata,
    SheetTab,
    SheetsBackendError,
    SheetsBatchInvalidError,
    SpreadsheetState,
    validate_batch,
    validate_plain_string,
)

METADATA_TAB = "Metadata"
METADATA_HEADERS = ("key", "value", "updated_at", "notes")
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
NON_RETRYABLE_HTTP_STATUSES = {400, 401, 403, 404, 409}


@dataclass(frozen=True)
class GoogleRetryPolicy:
    """Bounded retry policy for read-only Google API calls."""

    max_attempts: int = 3
    initial_delay: float = 0.5
    max_delay: float = 5.0


@dataclass(frozen=True)
class MetadataRow:
    """Parsed Metadata tab row."""

    key: str
    value: str
    updated_at: str
    notes: str
    row_index: int


@dataclass(frozen=True)
class RawSpreadsheetSnapshot:
    """Parsed Google Sheets state used for translation."""

    spreadsheet_id: str
    sheet_ids: Mapping[str, int]
    headers: Mapping[str, tuple[str, ...]]
    metadata_rows: Mapping[str, MetadataRow]


@dataclass(frozen=True)
class MetadataStorageInspection:
    """Safe Metadata storage inspection result."""

    status: str
    reason_code: str | None = None


def utc_now() -> datetime:
    """Return current UTC time."""

    return datetime.now(UTC)


def format_rfc3339(value: datetime) -> str:
    """Format a timezone-aware datetime as RFC 3339 UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise SheetsBatchInvalidError("clock", "NAIVE_DATETIME")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def a1_quote_sheet_name(title: str) -> str:
    """Quote a sheet title for A1 notation."""

    return "'" + title.replace("'", "''") + "'"


def formula_like(value: str) -> bool:
    """Return whether a value may be interpreted as a formula."""

    stripped = value.lstrip(" \t\r\n\v\f\u0000")
    return stripped != "" and stripped[0] in {"=", "+", "-", "@"}


def string_cell(value: str) -> dict[str, dict[str, str]]:
    """Return a userEnteredValue string cell."""

    return {"userEnteredValue": {"stringValue": value}}


def safe_reason(value: object, fallback: str) -> str:
    """Return safe diagnostic text."""

    rendered = str(value)
    return fallback if secret_findings(rendered) else rendered


def reject_secret_cell(path: str, value: str) -> None:
    """Reject raw secret markers returned by Google Sheets."""

    findings = secret_findings(value)
    if findings:
        raise SheetsBackendError(
            "SHEETS_RESPONSE_INVALID",
            path,
            findings[0][1],
            exit_code=EXIT_POLICY_BLOCKED,
        )


def http_status(error: BaseException) -> int | None:
    """Extract an HTTP status from Google or fake errors."""

    resp = getattr(error, "resp", None)
    status = getattr(resp, "status", None)
    if isinstance(status, int):
        return status
    for attr in ("status", "status_code"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value
    return None


def is_transport_error(error: BaseException) -> bool:
    """Return whether an error is a retryable transport-like failure."""

    return isinstance(error, (OSError, TimeoutError, ConnectionError)) or bool(
        getattr(error, "transport_error", False)
    )


def classify_google_error(
    error: BaseException,
    *,
    operation: str,
    after_write: bool = False,
) -> SheetsBackendError:
    """Convert Google/fake errors into safe backend errors."""

    status = http_status(error)
    if after_write and (is_transport_error(error) or status in {500, 502, 503, 504}):
        return SheetsBackendError(
            "SHEETS_WRITE_OUTCOME_UNKNOWN",
            operation,
            "UNKNOWN_WRITE_RESULT",
            may_have_applied=True,
        )
    if operation == "create" and (is_transport_error(error) or status in {500, 502, 503, 504}):
        return SheetsBackendError(
            "SHEETS_CREATE_OUTCOME_UNKNOWN",
            operation,
            "UNKNOWN_CREATE_RESULT",
            may_have_applied=True,
        )
    if is_transport_error(error):
        return SheetsBackendError("SHEETS_NETWORK_FAILED", operation, "TRANSPORT_FAILED")
    if status == 400:
        return SheetsBackendError("SHEETS_API_BAD_REQUEST", operation, "HTTP_400")
    if status == 401:
        return SheetsBackendError("SHEETS_AUTH_FAILED", operation, "HTTP_401")
    if status == 403:
        return SheetsBackendError("SHEETS_PERMISSION_DENIED", operation, "HTTP_403")
    if status == 404:
        return SheetsBackendError("SHEETS_NOT_FOUND", operation, "HTTP_404")
    if status == 409:
        return SheetsBackendError("SHEETS_API_CONFLICT", operation, "HTTP_409")
    if status == 429:
        return SheetsBackendError("SHEETS_RATE_LIMITED", operation, "HTTP_429")
    if status in {500, 502, 503, 504}:
        return SheetsBackendError("SHEETS_SERVICE_UNAVAILABLE", operation, f"HTTP_{status}")
    return SheetsBackendError(
        "SHEETS_BACKEND_IO_FAILED", operation, safe_reason(error, "GOOGLE_API_FAILED")
    )


class GoogleSheetsBackend:
    """SheetsBackend implementation backed by an injected Google Sheets v4 service."""

    def __init__(
        self,
        *,
        spreadsheet_id: str,
        service: Any,
        clock: Callable[[], datetime] = utc_now,
        retry_policy: GoogleRetryPolicy = GoogleRetryPolicy(),
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[int], float] = lambda _attempt: 0.0,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.service = service
        self.clock = clock
        self.retry_policy = retry_policy
        self.sleeper = sleeper
        self.jitter = jitter

    def read_spreadsheet(self) -> SpreadsheetState:
        """Read tab titles, header rows and metadata without mutation."""

        snapshot = self._fetch_snapshot()
        return SpreadsheetState(
            spreadsheet_id=snapshot.spreadsheet_id,
            metadata=tuple(sorted((key, row.value) for key, row in snapshot.metadata_rows.items())),
            tabs=tuple(
                SheetTab(title, headers) for title, headers in sorted(snapshot.headers.items())
            ),
        )

    def read_metadata(self, keys: Sequence[str]) -> dict[str, str]:
        """Read selected metadata keys."""

        snapshot = self._fetch_snapshot()
        wanted = set(keys)
        return {key: row.value for key, row in snapshot.metadata_rows.items() if key in wanted}

    def apply_batch(self, mutations: Sequence[Mutation]) -> BatchResult:
        """Translate domain mutations into a single Google batchUpdate call."""

        validate_batch(mutations)
        timestamp = format_rfc3339(self.clock())
        snapshot = self._fetch_snapshot()
        requests = self._translate_batch(snapshot, mutations, timestamp)
        body = {"requests": requests, "includeSpreadsheetInResponse": False}
        try:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body=body,
            ).execute(num_retries=0)
        except Exception as exc:
            raise classify_google_error(exc, operation="batchUpdate", after_write=True) from None
        try:
            read_back = self.read_spreadsheet()
        except SheetsBackendError as exc:
            raise SheetsBackendError(
                "SHEETS_WRITE_OUTCOME_UNKNOWN",
                exc.path,
                "UNKNOWN_WRITE_RESULT",
                may_have_applied=True,
            ) from None
        return BatchResult(applied_mutations=len(mutations), spreadsheet_state=read_back)

    def create_spreadsheet(
        self,
        title: str,
        *,
        initial_tabs: tuple[InitialSheetSpec, ...] = (),
    ) -> SpreadsheetState:
        """Create a spreadsheet without Metadata bootstrap."""

        if title == "":
            raise SheetsBatchInvalidError("title", "EMPTY")
        validate_plain_string("title", title)
        body: dict[str, Any] = {"properties": {"title": title}}
        if initial_tabs:
            body["sheets"] = [
                self._initial_sheet_body(index + 1, spec) for index, spec in enumerate(initial_tabs)
            ]
        try:
            response = (
                self.service.spreadsheets()
                .create(
                    body=body,
                )
                .execute(num_retries=0)
            )
        except Exception as exc:
            raise classify_google_error(exc, operation="create", after_write=False) from None
        if not isinstance(response, Mapping):
            raise SheetsBackendError(
                "SHEETS_RESPONSE_INVALID",
                "create.response",
                "OBJECT_REQUIRED",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
        spreadsheet_id = response.get("spreadsheetId")
        if not isinstance(spreadsheet_id, str) or spreadsheet_id == "":
            raise SheetsBackendError(
                "SHEETS_RESPONSE_INVALID",
                "create.spreadsheetId",
                "MISSING",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
        return SpreadsheetState(spreadsheet_id=spreadsheet_id)

    def inspect_metadata_storage(self) -> MetadataStorageInspection:
        """Inspect Metadata tab availability without creating it."""

        spreadsheet = self._execute_read(
            self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                fields="spreadsheetId,sheets(properties(sheetId,title))",
            ),
            "get",
        )
        sheet_ids = self._parse_sheet_ids(spreadsheet)
        if METADATA_TAB not in sheet_ids:
            if any(title.lower() == METADATA_TAB.lower() for title in sheet_ids):
                return MetadataStorageInspection("CONFLICT", "SHEETS_BOOTSTRAP_CONFLICT")
            return MetadataStorageInspection("MISSING", "SHEETS_INIT_BOOTSTRAP_REQUIRED")
        try:
            values = self._execute_read(
                self.service.spreadsheets()
                .values()
                .batchGet(
                    spreadsheetId=self.spreadsheet_id,
                    ranges=[f"{a1_quote_sheet_name(METADATA_TAB)}!A:D"],
                    majorDimension="ROWS",
                    valueRenderOption="FORMULA",
                ),
                "values.batchGet",
            )
            value_ranges = self._parse_value_ranges(values, 1)
            rows = value_ranges[0].get("values", [])
            if not isinstance(rows, list):
                raise self._response_invalid("values.values", "LIST_REQUIRED")
            self._parse_metadata_rows([self._normalize_row(row) for row in rows])
        except SheetsBackendError as exc:
            return MetadataStorageInspection("INVALID", exc.code)
        return MetadataStorageInspection("READY")

    def bootstrap_metadata_storage(self) -> BatchResult:
        """Create Metadata tab/header as one atomic batchUpdate and read back."""

        inspection = self.inspect_metadata_storage()
        if inspection.status == "READY":
            return BatchResult(applied_mutations=0, spreadsheet_state=self.read_spreadsheet())
        if inspection.status != "MISSING":
            raise SheetsBackendError(
                inspection.reason_code or "SHEETS_BOOTSTRAP_CONFLICT",
                "Metadata",
                "BOOTSTRAP_BLOCKED",
                exit_code=EXIT_POLICY_BLOCKED,
            )
        spreadsheet = self._execute_read(
            self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                fields="spreadsheetId,sheets(properties(sheetId,title))",
            ),
            "get",
        )
        sheet_ids = self._parse_sheet_ids(spreadsheet)
        next_sheet_id = max(sheet_ids.values(), default=0) + 1
        body = {
            "requests": [
                {"addSheet": {"properties": {"sheetId": next_sheet_id, "title": METADATA_TAB}}},
                self._headers_request(next_sheet_id, 0, METADATA_HEADERS),
            ],
            "includeSpreadsheetInResponse": False,
        }
        try:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body=body,
            ).execute(num_retries=0)
        except Exception as exc:
            raise classify_google_error(exc, operation="batchUpdate", after_write=True) from None
        try:
            state = self.read_spreadsheet()
        except SheetsBackendError as exc:
            raise SheetsBackendError(
                "SHEETS_WRITE_OUTCOME_UNKNOWN",
                exc.path,
                "UNKNOWN_WRITE_RESULT",
                may_have_applied=True,
            ) from None
        return BatchResult(applied_mutations=2, spreadsheet_state=state)

    def _fetch_snapshot(self) -> RawSpreadsheetSnapshot:
        spreadsheet = self._execute_read(
            self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                fields="spreadsheetId,sheets(properties(sheetId,title))",
            ),
            "get",
        )
        sheet_ids = self._parse_sheet_ids(spreadsheet)
        if METADATA_TAB not in sheet_ids:
            raise SheetsBackendError(
                "SHEETS_LOCK_STORAGE_UNAVAILABLE",
                "Metadata",
                "TAB_MISSING",
                exit_code=EXIT_POLICY_BLOCKED,
            )
        ranges = [
            (
                f"{a1_quote_sheet_name(title)}!A:D"
                if title == METADATA_TAB
                else f"{a1_quote_sheet_name(title)}!1:1"
            )
            for title in sheet_ids
        ]
        values = self._execute_read(
            self.service.spreadsheets()
            .values()
            .batchGet(
                spreadsheetId=self.spreadsheet_id,
                ranges=ranges,
                majorDimension="ROWS",
                valueRenderOption="FORMULA",
            ),
            "values.batchGet",
        )
        value_ranges = self._parse_value_ranges(values, len(ranges))
        headers: dict[str, tuple[str, ...]] = {}
        metadata_rows: dict[str, MetadataRow] = {}
        for title, value_range in zip(sheet_ids, value_ranges, strict=True):
            rows = value_range.get("values", [])
            if not isinstance(rows, list):
                raise self._response_invalid("values.values", "LIST_REQUIRED")
            normalized_rows = [self._normalize_row(row) for row in rows]
            if title == METADATA_TAB:
                metadata_rows = self._parse_metadata_rows(normalized_rows)
                headers[title] = tuple(normalized_rows[0]) if normalized_rows else ()
            else:
                header = tuple(normalized_rows[0]) if normalized_rows else ()
                for index, value in enumerate(header):
                    reject_secret_cell(f"{title}.headers.[{index}]", value)
                    if formula_like(value):
                        raise SheetsBackendError(
                            "SHEETS_METADATA_FORMULA_REJECTED",
                            f"{title}.headers.[{index}]",
                            "FORMULA_HEADER",
                            exit_code=EXIT_POLICY_BLOCKED,
                        )
                headers[title] = header
        return RawSpreadsheetSnapshot(
            spreadsheet_id=self.spreadsheet_id,
            sheet_ids=sheet_ids,
            headers=headers,
            metadata_rows=metadata_rows,
        )

    def _execute_read(self, request: Any, operation: str) -> Any:
        attempts = max(1, self.retry_policy.max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                return request.execute(num_retries=0)
            except Exception as exc:
                status = http_status(exc)
                retryable = status in RETRYABLE_HTTP_STATUSES or is_transport_error(exc)
                if not retryable or status in NON_RETRYABLE_HTTP_STATUSES or attempt >= attempts:
                    raise classify_google_error(exc, operation=operation) from None
                delay = min(
                    self.retry_policy.initial_delay * (2 ** (attempt - 1)),
                    self.retry_policy.max_delay,
                )
                self.sleeper(delay + self.jitter(attempt))
        raise SheetsBackendError("SHEETS_BACKEND_IO_FAILED", operation, "RETRY_EXHAUSTED")

    def _parse_sheet_ids(self, response: Any) -> dict[str, int]:
        if not isinstance(response, Mapping):
            raise self._response_invalid("spreadsheet", "OBJECT_REQUIRED")
        sheets = response.get("sheets")
        if not isinstance(sheets, list):
            raise self._response_invalid("spreadsheet.sheets", "LIST_REQUIRED")
        sheet_ids: dict[str, int] = {}
        for index, sheet in enumerate(sheets):
            properties = sheet.get("properties") if isinstance(sheet, Mapping) else None
            if not isinstance(properties, Mapping):
                raise self._response_invalid(f"sheets.[{index}].properties", "OBJECT_REQUIRED")
            title = properties.get("title")
            sheet_id = properties.get("sheetId")
            if not isinstance(title, str) or not isinstance(sheet_id, int):
                raise self._response_invalid(f"sheets.[{index}].properties", "INVALID")
            sheet_ids[title] = sheet_id
        return sheet_ids

    def _parse_value_ranges(self, response: Any, expected_count: int) -> list[Mapping[str, Any]]:
        if not isinstance(response, Mapping):
            raise self._response_invalid("values", "OBJECT_REQUIRED")
        value_ranges = response.get("valueRanges")
        if not isinstance(value_ranges, list) or len(value_ranges) != expected_count:
            raise self._response_invalid("values.valueRanges", "INVALID_COUNT")
        if not all(isinstance(value_range, Mapping) for value_range in value_ranges):
            raise self._response_invalid("values.valueRanges", "OBJECT_REQUIRED")
        return value_ranges

    def _normalize_row(self, row: Any) -> list[str]:
        if not isinstance(row, list):
            raise self._response_invalid("values.row", "LIST_REQUIRED")
        return [str(cell) for cell in row]

    def _parse_metadata_rows(self, rows: list[list[str]]) -> dict[str, MetadataRow]:
        if not rows or tuple(rows[0][: len(METADATA_HEADERS)]) != METADATA_HEADERS:
            raise SheetsBackendError(
                "SHEETS_METADATA_SCHEMA_INVALID",
                "Metadata.headers",
                "HEADER_MISMATCH",
                exit_code=EXIT_POLICY_BLOCKED,
            )
        metadata: dict[str, MetadataRow] = {}
        for row_offset, raw_row in enumerate(rows[1:], start=1):
            row = [*raw_row, "", "", "", ""][:4]
            key, value, updated_at, notes = row
            if key == "":
                if any(cell != "" for cell in (value, updated_at, notes)):
                    raise SheetsBackendError(
                        "SHEETS_METADATA_ROW_INVALID",
                        f"Metadata.rows.[{row_offset}]",
                        "EMPTY_KEY_WITH_VALUES",
                        exit_code=EXIT_POLICY_BLOCKED,
                    )
                continue
            reject_secret_cell(f"Metadata.rows.[{row_offset}].key", key)
            reject_secret_cell(f"Metadata.rows.[{row_offset}].value", value)
            if formula_like(key):
                raise SheetsBackendError(
                    "SHEETS_METADATA_FORMULA_REJECTED",
                    f"Metadata.rows.[{row_offset}].key",
                    "FORMULA_KEY",
                    exit_code=EXIT_POLICY_BLOCKED,
                )
            if formula_like(value):
                raise SheetsBackendError(
                    "SHEETS_METADATA_FORMULA_REJECTED",
                    f"Metadata.rows.[{row_offset}].value",
                    "FORMULA_VALUE",
                    exit_code=EXIT_POLICY_BLOCKED,
                )
            if key in metadata:
                raise SheetsBackendError(
                    "SHEETS_METADATA_DUPLICATE_KEY",
                    "Metadata.key",
                    "DUPLICATE_KEY",
                    exit_code=EXIT_POLICY_BLOCKED,
                )
            metadata[key] = MetadataRow(
                key=key,
                value=value,
                updated_at=updated_at,
                notes=notes,
                row_index=row_offset,
            )
        return metadata

    def _translate_batch(
        self,
        snapshot: RawSpreadsheetSnapshot,
        mutations: Sequence[Mutation],
        timestamp: str,
    ) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        sheet_ids = dict(snapshot.sheet_ids)
        headers = {title: list(values) for title, values in snapshot.headers.items()}
        metadata = {key: row for key, row in snapshot.metadata_rows.items()}
        metadata_sheet_id = sheet_ids[METADATA_TAB]
        next_sheet_id = max(sheet_ids.values(), default=999) + 1
        next_metadata_row = max((row.row_index for row in metadata.values()), default=0) + 1

        for mutation in mutations:
            if isinstance(mutation, CreateTab):
                if mutation.name in sheet_ids:
                    raise SheetsBatchInvalidError("mutations.name", "TAB_EXISTS")
                sheet_id = next_sheet_id
                next_sheet_id += 1
                sheet_ids[mutation.name] = sheet_id
                headers[mutation.name] = []
                requests.append(
                    {"addSheet": {"properties": {"sheetId": sheet_id, "title": mutation.name}}}
                )
                if mutation.headers:
                    requests.append(self._headers_request(sheet_id, 0, mutation.headers))
                    headers[mutation.name].extend(mutation.headers)
            elif isinstance(mutation, AddHeaders):
                if mutation.tab_name not in sheet_ids:
                    raise SheetsBatchInvalidError("mutations.tab_name", "TAB_MISSING")
                start_column = len(headers.get(mutation.tab_name, []))
                requests.append(
                    self._headers_request(
                        sheet_ids[mutation.tab_name], start_column, mutation.headers
                    )
                )
                headers.setdefault(mutation.tab_name, []).extend(mutation.headers)
            elif isinstance(mutation, SetMetadata):
                for key, value in mutation.values:
                    if key in metadata:
                        row = metadata[key]
                        metadata[key] = MetadataRow(key, value, timestamp, row.notes, row.row_index)
                        requests.append(
                            self._metadata_value_request(
                                metadata_sheet_id, row.row_index, value, timestamp
                            )
                        )
                    else:
                        row = MetadataRow(key, value, timestamp, "", next_metadata_row)
                        next_metadata_row += 1
                        metadata[key] = row
                        requests.append(self._metadata_new_row_request(metadata_sheet_id, row))
            elif isinstance(mutation, ClearMetadata):
                for key in mutation.keys:
                    if key in metadata:
                        row = metadata[key]
                        metadata[key] = MetadataRow(key, "", timestamp, row.notes, row.row_index)
                        requests.append(
                            self._metadata_value_request(
                                metadata_sheet_id, row.row_index, "", timestamp
                            )
                        )
            else:
                raise SheetsBatchInvalidError("mutations", "UNKNOWN_MUTATION")
        return requests

    def _headers_request(
        self,
        sheet_id: int,
        start_column: int,
        headers: Sequence[str],
    ) -> dict[str, Any]:
        return {
            "updateCells": {
                "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": start_column},
                "rows": [{"values": [string_cell(header) for header in headers]}],
                "fields": "userEnteredValue",
            }
        }

    def _metadata_value_request(
        self,
        sheet_id: int,
        row_index: int,
        value: str,
        timestamp: str,
    ) -> dict[str, Any]:
        return {
            "updateCells": {
                "start": {
                    "sheetId": sheet_id,
                    "rowIndex": row_index,
                    "columnIndex": 1,
                },
                "rows": [{"values": [string_cell(value), string_cell(timestamp)]}],
                "fields": "userEnteredValue",
            }
        }

    def _metadata_new_row_request(self, sheet_id: int, row: MetadataRow) -> dict[str, Any]:
        return {
            "updateCells": {
                "start": {
                    "sheetId": sheet_id,
                    "rowIndex": row.row_index,
                    "columnIndex": 0,
                },
                "rows": [
                    {
                        "values": [
                            string_cell(row.key),
                            string_cell(row.value),
                            string_cell(row.updated_at),
                            string_cell(row.notes),
                        ]
                    }
                ],
                "fields": "userEnteredValue",
            }
        }

    def _initial_sheet_body(self, sheet_id: int, spec: InitialSheetSpec) -> dict[str, Any]:
        validate_plain_string("initial_tabs.title", spec.title)
        for header in spec.headers:
            validate_plain_string("initial_tabs.headers", header)
        sheet: dict[str, Any] = {"properties": {"sheetId": sheet_id, "title": spec.title}}
        if spec.headers:
            sheet["data"] = [
                {
                    "rowData": [
                        {"values": [string_cell(header) for header in spec.headers]},
                    ],
                    "startRow": 0,
                    "startColumn": 0,
                }
            ]
        return sheet

    def _response_invalid(self, path: str, reason: str) -> SheetsBackendError:
        return SheetsBackendError(
            "SHEETS_RESPONSE_INVALID",
            path,
            reason,
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )

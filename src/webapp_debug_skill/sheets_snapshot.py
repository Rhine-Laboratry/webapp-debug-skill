"""Read-only Google Sheets snapshot export for coverage/report inputs."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from webapp_debug_skill.errors import (
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_EXTERNAL_FAILURE,
    EXIT_POLICY_BLOCKED,
)
from webapp_debug_skill.google_sheets_backend import a1_quote_sheet_name, formula_like
from webapp_debug_skill.redaction import (
    redact_inline_text,
    redact_secret_value,
    redacted,
    secret_findings,
    secret_type_for_key,
)
from webapp_debug_skill.sheets_init import CanonicalSheetsSchema, CanonicalTab

SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_MAX_ROWS_PER_TAB = 10_000
TRAILING_HEADER_SCAN_COLUMNS = 25
TOP_LEVEL_COMPAT_TABS = ("Inventory", "Scenarios", "Test Runs", "Defects")


class SheetsSnapshotError(RuntimeError):
    """Safe snapshot export failure."""

    def __init__(
        self,
        code: str,
        path: str = "snapshot",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_POLICY_BLOCKED,
    ) -> None:
        safe_code = "SHEETS_SNAPSHOT_FAILED" if secret_findings(code) else code
        super().__init__(safe_code)
        self.code = safe_code
        self.path = "snapshot" if secret_findings(path) else path
        self.reason = "FAILED" if secret_findings(reason) else reason
        self.exit_code = exit_code


class SheetsSnapshotReader(Protocol):
    """Read-only backend contract for snapshot export."""

    spreadsheet_id: str

    def list_sheet_titles(self) -> tuple[str, ...]:
        """Read available sheet titles."""

    def read_value_ranges(self, ranges: Mapping[str, str]) -> dict[str, list[list[str]]]:
        """Read A1 ranges keyed by tab title."""


@dataclass(frozen=True)
class SnapshotWarning:
    """Safe snapshot warning."""

    code: str
    tab: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        """Return JSON representation."""

        return {"code": self.code, "tab": self.tab, "detail": self.detail}


@dataclass(frozen=True)
class SnapshotSummary:
    """Safe snapshot summary."""

    row_counts: dict[str, int]
    warnings: tuple[SnapshotWarning, ...] = ()
    redactions: dict[str, int] = field(default_factory=dict)
    unknown_trailing_columns: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return JSON representation."""

        return {
            "row_counts": self.row_counts,
            "warnings": [warning.to_dict() for warning in self.warnings],
            "redactions": dict(sorted(self.redactions.items())),
            "unknown_trailing_columns": self.unknown_trailing_columns,
        }


@dataclass(frozen=True)
class SheetsSnapshot:
    """Snapshot payload and safe summary."""

    payload: dict[str, Any]
    summary: SnapshotSummary


class SheetsSnapshotExporter:
    """Build a read-only snapshot from a Sheets backend and canonical schema."""

    def __init__(
        self,
        *,
        reader: SheetsSnapshotReader,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.reader = reader
        self.clock = clock

    def export(
        self,
        schema: CanonicalSheetsSchema,
        *,
        tabs: Sequence[str] | None = None,
        max_rows_per_tab: int = DEFAULT_MAX_ROWS_PER_TAB,
    ) -> SheetsSnapshot:
        """Read selected tabs and return a redacted snapshot."""

        if max_rows_per_tab < 1:
            raise SheetsSnapshotError(
                "SHEETS_SNAPSHOT_SCHEMA_INVALID",
                "max_rows_per_tab",
                "BELOW_MINIMUM",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
        selected_tabs = select_tabs(schema, tabs)
        available = set(self.reader.list_sheet_titles())
        missing = [tab.name for tab in selected_tabs if tab.name not in available]
        if missing:
            raise SheetsSnapshotError(
                "SHEETS_SNAPSHOT_TAB_MISSING",
                "tabs",
                "MISSING_REQUIRED_TAB",
            )
        ranges = {
            tab.name: bounded_a1_range(tab.name, len(tab.headers), max_rows_per_tab)
            for tab in selected_tabs
        }
        raw_rows = self.reader.read_value_ranges(ranges)
        tabs_payload: dict[str, list[dict[str, str]]] = {}
        warnings: list[SnapshotWarning] = []
        redaction_counts: Counter[str] = Counter()
        trailing_counts: dict[str, int] = {}
        for tab in selected_tabs:
            result = parse_tab_rows(
                tab,
                raw_rows.get(tab.name, []),
                max_rows_per_tab=max_rows_per_tab,
                redaction_counts=redaction_counts,
            )
            tabs_payload[tab.name] = result.rows
            warnings.extend(result.warnings)
            if result.unknown_trailing_columns:
                trailing_counts[tab.name] = result.unknown_trailing_columns
        row_counts = {tab_name: len(rows) for tab_name, rows in tabs_payload.items()}
        summary = SnapshotSummary(
            row_counts=row_counts,
            warnings=tuple(warnings),
            redactions=dict(redaction_counts),
            unknown_trailing_columns=trailing_counts,
        )
        payload: dict[str, Any] = {
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "source": {
                "kind": "google_sheets",
                "spreadsheet_id": self.reader.spreadsheet_id,
                "schema_version": schema.schema_version,
            },
            "exported_at": format_rfc3339(self.clock()),
            "tabs": tabs_payload,
            "summary": summary.to_dict(),
        }
        for name in TOP_LEVEL_COMPAT_TABS:
            payload[name] = tabs_payload.get(name, [])
        findings = secret_findings({"tabs": tabs_payload})
        if findings:
            raise SheetsSnapshotError(
                "SHEETS_SNAPSHOT_REDACTION_FAILED",
                findings[0][0],
                findings[0][1],
            )
        return SheetsSnapshot(payload=payload, summary=summary)


@dataclass(frozen=True)
class ParsedTabRows:
    """Parsed rows for one tab."""

    rows: list[dict[str, str]]
    warnings: tuple[SnapshotWarning, ...] = ()
    unknown_trailing_columns: int = 0


def select_tabs(
    schema: CanonicalSheetsSchema,
    requested: Sequence[str] | None,
) -> tuple[CanonicalTab, ...]:
    """Select canonical tabs by exact name."""

    by_name = {tab.name: tab for tab in schema.tabs}
    if requested is None:
        return schema.tabs
    selected: list[CanonicalTab] = []
    for name in requested:
        if name not in by_name:
            raise SheetsSnapshotError(
                "SHEETS_SNAPSHOT_SCHEMA_INVALID",
                "tabs",
                "UNKNOWN_TAB",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
        selected.append(by_name[name])
    return tuple(selected)


def parse_tab_rows(
    tab: CanonicalTab,
    raw_rows: Sequence[Sequence[Any]],
    *,
    max_rows_per_tab: int,
    redaction_counts: Counter[str],
) -> ParsedTabRows:
    """Parse one tab using exact canonical headers."""

    if not raw_rows:
        return ParsedTabRows(
            rows=[],
            warnings=(SnapshotWarning("SHEETS_SNAPSHOT_TAB_EMPTY", tab.name, "NO_HEADER"),),
        )
    header = [str(cell) for cell in raw_rows[0]]
    validate_header(tab, header)
    trailing = count_unknown_trailing_columns(header, len(tab.headers))
    warnings: list[SnapshotWarning] = []
    if trailing:
        warnings.append(
            SnapshotWarning(
                "SHEETS_SNAPSHOT_UNKNOWN_TRAILING_COLUMNS",
                tab.name,
                str(trailing),
            )
        )
    data_rows = list(raw_rows[1:])
    if len(data_rows) > max_rows_per_tab:
        raise SheetsSnapshotError(
            "SHEETS_SNAPSHOT_ROW_LIMIT_EXCEEDED",
            tab.name,
            "ROW_LIMIT_EXCEEDED",
        )
    rows: list[dict[str, str]] = []
    for raw_row in data_rows:
        values = [str(cell) for cell in raw_row]
        if not any(value != "" for value in values):
            continue
        padded = [*values, *[""] * len(tab.headers)][: len(tab.headers)]
        parsed = {
            header_name: redact_cell_value(header_name, value, redaction_counts)
            for header_name, value in zip(tab.headers, padded, strict=True)
        }
        if tab.name == "Inventory" and "status" not in parsed:
            parsed["status"] = parsed.get("discovery_status", "")
        rows.append(parsed)
    return ParsedTabRows(rows=rows, warnings=tuple(warnings), unknown_trailing_columns=trailing)


def validate_header(tab: CanonicalTab, header: Sequence[str]) -> None:
    """Validate exact canonical prefix and unsafe header shapes."""

    canonical = list(tab.headers)
    if len(header) < len(canonical):
        raise SheetsSnapshotError(
            "SHEETS_SNAPSHOT_HEADER_CONFLICT",
            tab.name,
            "HEADER_TOO_SHORT",
        )
    prefix = list(header[: len(canonical)])
    if any(value == "" for value in prefix):
        raise SheetsSnapshotError(
            "SHEETS_SNAPSHOT_HEADER_CONFLICT",
            tab.name,
            "INTERNAL_EMPTY_HEADER",
        )
    last_non_empty = max((index for index, value in enumerate(header) if value != ""), default=-1)
    if last_non_empty >= 0 and any(value == "" for value in header[: last_non_empty + 1]):
        raise SheetsSnapshotError(
            "SHEETS_SNAPSHOT_HEADER_CONFLICT",
            tab.name,
            "INTERNAL_EMPTY_HEADER",
        )
    if any(formula_like(value) for value in header if value != ""):
        raise SheetsSnapshotError(
            "SHEETS_SNAPSHOT_HEADER_CONFLICT",
            tab.name,
            "FORMULA_HEADER",
        )
    non_empty = [value for value in header if value != ""]
    if len(non_empty) != len(set(non_empty)):
        raise SheetsSnapshotError(
            "SHEETS_SNAPSHOT_HEADER_CONFLICT",
            tab.name,
            "DUPLICATE_HEADER",
        )
    if prefix != canonical:
        raise SheetsSnapshotError(
            "SHEETS_SNAPSHOT_HEADER_CONFLICT",
            tab.name,
            "CANONICAL_PREFIX_MISMATCH",
        )


def count_unknown_trailing_columns(header: Sequence[str], canonical_count: int) -> int:
    """Count non-empty headers after the canonical prefix."""

    return sum(1 for value in header[canonical_count:] if value != "")


def redact_cell_value(column_name: str, value: str, counts: Counter[str]) -> str:
    """Redact one cell based on column name and inline content."""

    kind = secret_type_for_key(column_name)
    if kind is not None:
        counts[kind] += 1
        return str(redact_secret_value(value, kind))
    redacted_value = redact_inline_text(value, counts, {})
    if secret_findings(redacted_value):
        counts["SECRET"] += 1
        return redacted("SECRET")
    return redacted_value


def bounded_a1_range(tab_name: str, canonical_column_count: int, max_rows_per_tab: int) -> str:
    """Build a bounded A1 range with extra header columns for trailing warnings."""

    column_count = canonical_column_count + TRAILING_HEADER_SCAN_COLUMNS
    end_column = column_letter(column_count)
    end_row = max_rows_per_tab + 1
    return f"{a1_quote_sheet_name(tab_name)}!A1:{end_column}{end_row}"


def column_letter(index: int) -> str:
    """Convert a 1-based column index to A1 letters."""

    if index < 1:
        raise ValueError("column index must be positive")
    result = ""
    current = index
    while current:
        current, remainder = divmod(current - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def format_rfc3339(value: datetime) -> str:
    """Format a timezone-aware datetime as RFC 3339 UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise SheetsSnapshotError(
            "SHEETS_SNAPSHOT_SCHEMA_INVALID",
            "clock",
            "NAIVE_DATETIME",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write_snapshot(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write snapshot JSON."""

    rendered = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
        fsync_dir(path.parent)
    except OSError:
        raise SheetsSnapshotError(
            "SHEETS_SNAPSHOT_WRITE_FAILED",
            "output",
            "WRITE_FAILED",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def validate_output_path(path: Path, *, force: bool, protected_paths: Sequence[Path]) -> None:
    """Validate snapshot output target before writing."""

    try:
        stat_result = path.lstat()
    except OSError:
        stat_result = None
    if stat_result is not None:
        if stat.S_ISLNK(stat_result.st_mode):
            raise SheetsSnapshotError(
                "SHEETS_SNAPSHOT_OUTPUT_UNSAFE",
                "output",
                "SYMLINK_REJECTED",
            )
        if not stat.S_ISREG(stat_result.st_mode):
            raise SheetsSnapshotError(
                "SHEETS_SNAPSHOT_OUTPUT_UNSAFE",
                "output",
                "NOT_REGULAR_FILE",
            )
        if not force:
            raise SheetsSnapshotError(
                "SHEETS_SNAPSHOT_OUTPUT_EXISTS",
                "output",
                "EXISTS",
            )
    output_resolved = path.resolve(strict=False)
    for protected in protected_paths:
        if output_resolved == protected.resolve(strict=False):
            raise SheetsSnapshotError(
                "SHEETS_SNAPSHOT_OUTPUT_UNSAFE",
                "output",
                "INPUT_PATH_REJECTED",
            )


def fsync_dir(directory: Path) -> None:
    """Fsync a directory where supported."""

    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

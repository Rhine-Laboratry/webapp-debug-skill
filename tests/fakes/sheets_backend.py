"""Fake Sheets backend used by Phase 3B unit tests."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from webapp_debug_skill.sheets_client import (
    AddHeaders,
    AppendRows,
    BatchResult,
    ClearMetadata,
    CreateTab,
    InitialSheetSpec,
    Mutation,
    SetMetadata,
    SheetRow,
    SheetTab,
    SheetsBackendError,
    SheetsBatchInvalidError,
    SpreadsheetState,
    UpdateRowValues,
    validate_batch,
)


class FakeSheetsBackend:
    """Deterministic in-memory Sheets backend with failure injection."""

    def __init__(
        self,
        *,
        spreadsheet_id: str = "fake-spreadsheet",
        metadata: Mapping[str, str] | None = None,
        tabs: Mapping[str, Sequence[str]] | None = None,
        rows: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        metadata_available: bool = True,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self._metadata = dict(metadata or {})
        self._tabs = {str(name): list(headers) for name, headers in (tabs or {}).items()}
        self._rows = {
            str(name): [dict(row) for row in tab_rows] for name, tab_rows in (rows or {}).items()
        }
        self.metadata_available = metadata_available
        self.read_count = 0
        self.write_count = 0
        self.create_count = 0
        self.call_log: list[tuple[str, int | str]] = []
        self.applied_batches: list[tuple[Mutation, ...]] = []
        self.fail_before_apply = False
        self.fail_after_apply = False
        self.fail_reads = False
        self.after_apply_hook: Callable[["FakeSheetsBackend"], None] | None = None

    def read_spreadsheet(self) -> SpreadsheetState:
        """Read spreadsheet state without mutating fake state."""

        self._maybe_fail_read()
        self.read_count += 1
        self.call_log.append(("read_spreadsheet", self.read_count))
        return self._state()

    def read_metadata(self, keys: Sequence[str]) -> dict[str, str]:
        """Read selected metadata values."""

        self._maybe_fail_read()
        if not self.metadata_available:
            raise SheetsBackendError(
                "SHEETS_LOCK_STORAGE_UNAVAILABLE",
                "metadata",
                "UNAVAILABLE",
            )
        self.read_count += 1
        self.call_log.append(("read_metadata", ",".join(keys)))
        return {key: self._metadata[key] for key in keys if key in self._metadata}

    def apply_batch(self, mutations: Sequence[Mutation]) -> BatchResult:
        """Apply all mutations atomically, with failure injection."""

        self.call_log.append(("apply_batch", len(mutations)))
        if self.fail_before_apply:
            raise SheetsBackendError("SHEETS_BACKEND_IO_FAILED", "backend", "WRITE_FAILED")
        validate_batch(mutations)
        next_metadata = copy.deepcopy(self._metadata)
        next_tabs = copy.deepcopy(self._tabs)
        next_rows = copy.deepcopy(self._rows)
        for index, mutation in enumerate(mutations):
            self._apply_to_snapshot(next_metadata, next_tabs, next_rows, mutation, index)

        self._metadata = next_metadata
        self._tabs = next_tabs
        self._rows = next_rows
        self.write_count += 1
        self.applied_batches.append(tuple(mutations))
        if self.after_apply_hook is not None:
            self.after_apply_hook(self)
        result = BatchResult(applied_mutations=len(mutations), spreadsheet_state=self._state())
        if self.fail_after_apply:
            raise SheetsBackendError(
                "SHEETS_BACKEND_IO_FAILED",
                "backend",
                "UNKNOWN_WRITE_RESULT",
                may_have_applied=True,
            )
        return result

    def create_spreadsheet(
        self,
        title: str,
        *,
        initial_tabs: tuple[InitialSheetSpec, ...] = (),
    ) -> SpreadsheetState:
        """Create a deterministic fake spreadsheet."""

        if self.fail_before_apply:
            raise SheetsBackendError("SHEETS_BACKEND_IO_FAILED", "backend", "CREATE_FAILED")
        self.create_count += 1
        self.call_log.append(("create_spreadsheet", title))
        self.spreadsheet_id = f"fake-{self.create_count}"
        self._metadata = {}
        self._tabs = {spec.title: list(spec.headers) for spec in initial_tabs}
        self._rows = {spec.title: [] for spec in initial_tabs}
        return self._state()

    def set_metadata_direct(self, values: Mapping[str, str]) -> None:
        """Mutate metadata directly for race-condition tests."""

        self._metadata.update(dict(values))

    def clear_metadata_direct(self, keys: Sequence[str]) -> None:
        """Clear metadata directly for race-condition tests."""

        for key in keys:
            self._metadata.pop(key, None)

    def set_tabs_direct(self, tabs: Mapping[str, Sequence[str]]) -> None:
        """Replace tabs directly for race-condition and read-back tests."""

        self._tabs = {str(name): list(headers) for name, headers in tabs.items()}
        self._rows = {name: self._rows.get(name, []) for name in self._tabs}

    def set_rows_direct(self, tab_name: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Replace tab rows directly for race-condition and read-back tests."""

        self._rows[str(tab_name)] = [dict(row) for row in rows]

    def _state(self) -> SpreadsheetState:
        return SpreadsheetState(
            spreadsheet_id=str(self.spreadsheet_id),
            metadata=tuple(sorted((str(key), str(value)) for key, value in self._metadata.items())),
            tabs=tuple(
                SheetTab(
                    str(name),
                    tuple(headers),
                    tuple(
                        SheetRow(tuple((str(key), str(value)) for key, value in row.items()))
                        for row in self._rows.get(name, [])
                    ),
                )
                for name, headers in sorted(self._tabs.items())
            ),
        )

    def _maybe_fail_read(self) -> None:
        if self.fail_reads:
            raise SheetsBackendError("SHEETS_BACKEND_IO_FAILED", "backend", "READ_FAILED")

    def _apply_to_snapshot(
        self,
        metadata: dict[str, str],
        tabs: dict[str, list[str]],
        rows: dict[str, list[dict[str, Any]]],
        mutation: Mutation,
        index: int,
    ) -> None:
        if isinstance(mutation, SetMetadata):
            if not self.metadata_available:
                raise SheetsBackendError(
                    "SHEETS_LOCK_STORAGE_UNAVAILABLE",
                    "metadata",
                    "UNAVAILABLE",
                )
            metadata.update(dict(mutation.values))
        elif isinstance(mutation, ClearMetadata):
            if not self.metadata_available:
                raise SheetsBackendError(
                    "SHEETS_LOCK_STORAGE_UNAVAILABLE",
                    "metadata",
                    "UNAVAILABLE",
                )
            for key in mutation.keys:
                metadata.pop(key, None)
        elif isinstance(mutation, CreateTab):
            if mutation.name in tabs:
                raise SheetsBatchInvalidError(f"mutations.[{index}].name", "TAB_EXISTS")
            tabs[mutation.name] = list(mutation.headers)
            rows[mutation.name] = []
        elif isinstance(mutation, AddHeaders):
            if mutation.tab_name not in tabs:
                raise SheetsBatchInvalidError(f"mutations.[{index}].tab_name", "TAB_MISSING")
            for header in mutation.headers:
                if header not in tabs[mutation.tab_name]:
                    tabs[mutation.tab_name].append(header)
                    for row in rows.get(mutation.tab_name, []):
                        row.setdefault(header, "")
        elif isinstance(mutation, AppendRows):
            if mutation.tab_name not in tabs:
                raise SheetsBatchInvalidError(f"mutations.[{index}].tab_name", "TAB_MISSING")
            headers = tabs[mutation.tab_name]
            tab_rows = rows.setdefault(mutation.tab_name, [])
            for row_offset, row_values in enumerate(mutation.rows):
                incoming = dict(row_values)
                self._validate_row_columns(
                    headers,
                    incoming,
                    f"mutations.[{index}].rows.[{row_offset}]",
                )
                row = {header: "" for header in headers}
                row.update(incoming)
                tab_rows.append(row)
        elif isinstance(mutation, UpdateRowValues):
            if mutation.tab_name not in tabs:
                raise SheetsBatchInvalidError(f"mutations.[{index}].tab_name", "TAB_MISSING")
            headers = tabs[mutation.tab_name]
            tab_rows = rows.setdefault(mutation.tab_name, [])
            if mutation.row_index >= len(tab_rows):
                raise SheetsBatchInvalidError(f"mutations.[{index}].row_index", "ROW_MISSING")
            values = dict(mutation.values)
            expected = dict(mutation.expected_values)
            self._validate_row_columns(headers, values, f"mutations.[{index}].values")
            self._validate_row_columns(
                headers,
                expected,
                f"mutations.[{index}].expected_values",
            )
            current = tab_rows[mutation.row_index]
            for column, expected_value in expected.items():
                if str(current.get(column, "")) != str(expected_value):
                    raise SheetsBatchInvalidError(
                        f"mutations.[{index}].expected_values.{column}",
                        "EXPECTED_VALUE_MISMATCH",
                    )
            current.update(values)
        else:
            raise SheetsBatchInvalidError(f"mutations.[{index}]", "UNKNOWN_MUTATION")

    def _validate_row_columns(
        self,
        headers: Sequence[str],
        values: Mapping[str, Any],
        path: str,
    ) -> None:
        for column in values:
            if column not in headers:
                raise SheetsBatchInvalidError(f"{path}.{column}", "UNKNOWN_COLUMN")

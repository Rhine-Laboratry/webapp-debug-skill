"""Fake Sheets backend used by Phase 3B unit tests."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence

from webapp_debug_skill.sheets_client import (
    AddHeaders,
    BatchResult,
    ClearMetadata,
    CreateTab,
    Mutation,
    SetMetadata,
    SheetTab,
    SheetsBackendError,
    SheetsBatchInvalidError,
    SpreadsheetState,
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
        metadata_available: bool = True,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self._metadata = dict(metadata or {})
        self._tabs = {str(name): list(headers) for name, headers in (tabs or {}).items()}
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
        for index, mutation in enumerate(mutations):
            self._apply_to_snapshot(next_metadata, next_tabs, mutation, index)

        self._metadata = next_metadata
        self._tabs = next_tabs
        self.write_count += 1
        self.applied_batches.append(tuple(mutations))
        if self.after_apply_hook is not None:
            self.after_apply_hook(self)
        result = BatchResult(applied_mutations=len(mutations), spreadsheet_state=self._state())
        if self.fail_after_apply:
            raise SheetsBackendError("SHEETS_BACKEND_IO_FAILED", "backend", "UNKNOWN_WRITE_RESULT")
        return result

    def create_spreadsheet(self, title: str) -> SpreadsheetState:
        """Create a deterministic fake spreadsheet."""

        if self.fail_before_apply:
            raise SheetsBackendError("SHEETS_BACKEND_IO_FAILED", "backend", "CREATE_FAILED")
        self.create_count += 1
        self.call_log.append(("create_spreadsheet", title))
        self.spreadsheet_id = f"fake-{self.create_count}"
        self._metadata = {}
        self._tabs = {}
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

    def _state(self) -> SpreadsheetState:
        return SpreadsheetState(
            spreadsheet_id=str(self.spreadsheet_id),
            metadata=tuple(sorted((str(key), str(value)) for key, value in self._metadata.items())),
            tabs=tuple(
                SheetTab(str(name), tuple(headers)) for name, headers in sorted(self._tabs.items())
            ),
        )

    def _maybe_fail_read(self) -> None:
        if self.fail_reads:
            raise SheetsBackendError("SHEETS_BACKEND_IO_FAILED", "backend", "READ_FAILED")

    def _apply_to_snapshot(
        self,
        metadata: dict[str, str],
        tabs: dict[str, list[str]],
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
        elif isinstance(mutation, AddHeaders):
            if mutation.tab_name not in tabs:
                raise SheetsBatchInvalidError(f"mutations.[{index}].tab_name", "TAB_MISSING")
            for header in mutation.headers:
                if header not in tabs[mutation.tab_name]:
                    tabs[mutation.tab_name].append(header)
        else:
            raise SheetsBatchInvalidError(f"mutations.[{index}]", "UNKNOWN_MUTATION")

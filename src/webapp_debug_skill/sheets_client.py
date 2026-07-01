"""Google-SDK-independent Sheets backend contracts and mutations."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from webapp_debug_skill.errors import EXIT_ARGUMENT_OR_SCHEMA, EXIT_EXTERNAL_FAILURE
from webapp_debug_skill.redaction import secret_findings


class SheetsBackendError(RuntimeError):
    """Safe backend error that does not expose raw payloads."""

    def __init__(
        self,
        code: str,
        path: str = "sheets",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_EXTERNAL_FAILURE,
        may_have_applied: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.path = path
        self.reason = reason
        self.exit_code = exit_code
        self.may_have_applied = may_have_applied


class SheetsBatchInvalidError(SheetsBackendError):
    """Raised when a mutation batch is invalid before external mutation."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(
            "SHEETS_BATCH_INVALID",
            path,
            reason,
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )


@dataclass(frozen=True)
class SheetTab:
    """A tab snapshot with header names only for Phase 3B."""

    name: str
    headers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpreadsheetState:
    """Immutable spreadsheet state snapshot."""

    spreadsheet_id: str
    metadata: tuple[tuple[str, str], ...] = ()
    tabs: tuple[SheetTab, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        spreadsheet_id: str,
        *,
        metadata: Mapping[str, str] | None = None,
        tabs: Mapping[str, Sequence[str]] | None = None,
    ) -> "SpreadsheetState":
        """Build a state snapshot from mappings using defensive copies."""

        metadata_items = tuple(
            sorted((str(key), str(value)) for key, value in (metadata or {}).items())
        )
        tab_items = tuple(
            SheetTab(str(name), tuple(str(header) for header in headers))
            for name, headers in sorted((tabs or {}).items())
        )
        return cls(spreadsheet_id=str(spreadsheet_id), metadata=metadata_items, tabs=tab_items)

    def metadata_dict(self) -> dict[str, str]:
        """Return a mutable copy of metadata."""

        return dict(self.metadata)

    def tabs_dict(self) -> dict[str, list[str]]:
        """Return a mutable copy of tab headers."""

        return {tab.name: list(tab.headers) for tab in self.tabs}


@dataclass(frozen=True)
class BatchResult:
    """Safe batch result."""

    applied_mutations: int
    spreadsheet_state: SpreadsheetState


@dataclass(frozen=True)
class InitialSheetSpec:
    """Initial tab/header specification for spreadsheet creation."""

    title: str
    headers: tuple[str, ...] = ()


class SheetsMutation:
    """Marker base class for typed Sheets mutations."""


@dataclass(frozen=True)
class SetMetadata(SheetsMutation):
    """Set metadata key/value pairs as plain strings."""

    values: tuple[tuple[str, str], ...]

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "SetMetadata":
        """Build mutation from mapping."""

        return cls(tuple((str(key), str(value)) for key, value in values.items()))


@dataclass(frozen=True)
class ClearMetadata(SheetsMutation):
    """Clear metadata keys."""

    keys: tuple[str, ...]


@dataclass(frozen=True)
class CreateTab(SheetsMutation):
    """Create a tab with optional headers."""

    name: str
    headers: tuple[str, ...] = ()


@dataclass(frozen=True)
class AddHeaders(SheetsMutation):
    """Append headers to an existing tab."""

    tab_name: str
    headers: tuple[str, ...]


Mutation = SetMetadata | ClearMetadata | CreateTab | AddHeaders


class SheetsBackend(Protocol):
    """Backend contract used by domain logic without exposing Google SDK types."""

    def read_spreadsheet(self) -> SpreadsheetState:
        """Read spreadsheet state without side effects."""

    def read_metadata(self, keys: Sequence[str]) -> dict[str, str]:
        """Read selected metadata values without side effects."""

    def apply_batch(self, mutations: Sequence[Mutation]) -> BatchResult:
        """Apply all mutations atomically, in order."""

    def create_spreadsheet(
        self,
        title: str,
        *,
        initial_tabs: tuple[InitialSheetSpec, ...] = (),
    ) -> SpreadsheetState:
        """Create a spreadsheet. Fake implements this; real Google backend is Phase 3C."""


def validate_plain_string(path: str, value: str) -> None:
    """Validate a string intended for spreadsheet storage."""

    if not isinstance(value, str):
        raise SheetsBatchInvalidError(path, "STRING_REQUIRED")
    if value.startswith(("=", "+", "-", "@")):
        raise SheetsBatchInvalidError(path, "FORMULA_LIKE_VALUE")
    findings = secret_findings(value)
    if findings:
        raise SheetsBatchInvalidError(path, findings[0][1])


def validate_mutation(mutation: Mutation, index: int) -> None:
    """Validate a single mutation without exposing payload values."""

    prefix = f"mutations.[{index}]"
    if isinstance(mutation, SetMetadata):
        if not mutation.values:
            raise SheetsBatchInvalidError(f"{prefix}.values", "EMPTY")
        for key, value in mutation.values:
            validate_plain_string(f"{prefix}.values.key", key)
            validate_plain_string(f"{prefix}.values.value", value)
    elif isinstance(mutation, ClearMetadata):
        if not mutation.keys:
            raise SheetsBatchInvalidError(f"{prefix}.keys", "EMPTY")
        for key in mutation.keys:
            validate_plain_string(f"{prefix}.keys", key)
    elif isinstance(mutation, CreateTab):
        validate_plain_string(f"{prefix}.name", mutation.name)
        for header in mutation.headers:
            validate_plain_string(f"{prefix}.headers", header)
    elif isinstance(mutation, AddHeaders):
        validate_plain_string(f"{prefix}.tab_name", mutation.tab_name)
        if not mutation.headers:
            raise SheetsBatchInvalidError(f"{prefix}.headers", "EMPTY")
        for header in mutation.headers:
            validate_plain_string(f"{prefix}.headers", header)
    else:
        raise SheetsBatchInvalidError(prefix, "UNKNOWN_MUTATION")


def validate_batch(mutations: Sequence[Mutation]) -> None:
    """Validate a mutation batch before applying it."""

    if not mutations:
        raise SheetsBatchInvalidError("mutations", "EMPTY")
    for index, mutation in enumerate(mutations):
        validate_mutation(mutation, index)


def clone_state(state: SpreadsheetState) -> SpreadsheetState:
    """Return a defensive state copy."""

    return copy.deepcopy(state)

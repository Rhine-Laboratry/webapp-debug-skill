from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from tests.fakes.sheets_backend import FakeSheetsBackend
from webapp_debug_skill.sheets_client import ClearMetadata, Mutation, SetMetadata, SpreadsheetState
from webapp_debug_skill.sheets_init import (
    CanonicalSheetsSchema,
    CanonicalTab,
    InitPolicy,
    SheetsInitializer,
)
from webapp_debug_skill.sheets_lock import LOCK_OWNER, SheetsLockManager
from webapp_debug_skill.wal import PENDING, SCHEMA_VERSION, WalEntry, WalError, payload_hash

TOKEN = "phase3c1-owner-token-do-not-print"
OTHER_TOKEN = "phase3c1-other-token-do-not-print"
RUN_ID = "phase3c1-run"
COMMIT = "abc123"
NOW = datetime(2026, 6, 26, 0, 0, 0, tzinfo=UTC)
SECRET = "SECRET_MARKER_PHASE3C1_DO_NOT_LEAK"


def schema(
    *,
    version: int = 1,
    tabs: Sequence[tuple[str, Sequence[str]]] | None = None,
) -> CanonicalSheetsSchema:
    return CanonicalSheetsSchema(
        schema_version=version,
        tabs=tuple(
            CanonicalTab(name, tuple(headers))
            for name, headers in (
                tabs
                or (
                    ("Features", ("Feature ID", "Title", "Status")),
                    ("Scenarios", ("Scenario ID", "Feature ID", "Steps")),
                )
            )
        ),
    )


def one_tab_schema(headers: Sequence[str] = ("Feature ID", "Title")) -> CanonicalSheetsSchema:
    return schema(tabs=(("Features", tuple(headers)),))


def schema_mapping(
    *,
    version: Any = 1,
    tabs: Sequence[tuple[str, Sequence[str]]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": version,
        "tabs": [
            {"name": name, "columns": [[header, "manual"] for header in headers]}
            for name, headers in (
                tabs
                or (
                    ("Features", ("Feature ID", "Title")),
                    ("Scenarios", ("Scenario ID", "Feature ID")),
                )
            )
        ],
    }


def state(
    *,
    metadata: Mapping[str, str] | None = None,
    tabs: Mapping[str, Sequence[str]] | None = None,
) -> SpreadsheetState:
    return SpreadsheetState.from_mapping("fake-spreadsheet", metadata=metadata, tabs=tabs)


def policy() -> InitPolicy:
    return InitPolicy(run_id=RUN_ID, ttl_seconds=60, commit_sha=COMMIT)


def lock_manager(
    backend: FakeSheetsBackend,
    *,
    token: str = TOKEN,
    now: datetime = NOW,
) -> SheetsLockManager:
    return SheetsLockManager(backend, now_factory=lambda: now, token_factory=lambda: token)


class SpyWal:
    def __init__(
        self,
        *,
        events: list[str] | None = None,
        fail_pending: bool = False,
        fail_ack: bool = False,
        pending_entries: Sequence[WalEntry] = (),
    ) -> None:
        self.events = events if events is not None else []
        self.fail_pending = fail_pending
        self.fail_ack = fail_ack
        self.pending: list[WalEntry] = list(pending_entries)
        self.acks: list[str] = []
        self.pending_payloads: list[dict[str, Any]] = []

    def append_pending(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        operation_id: str | None = None,
    ) -> WalEntry:
        self.events.append("wal_pending")
        if self.fail_pending:
            raise WalError("WAL_WRITE_FAILED", "wal", "WRITE_FAILED")
        op_id = operation_id or f"op-{len(self.pending) + 1}"
        entry = WalEntry(
            schema_version=SCHEMA_VERSION,
            sequence=len(self.pending) + len(self.acks) + 1,
            operation_id=op_id,
            run_id=RUN_ID,
            operation=operation,
            payload_hash=payload_hash(payload),
            payload=payload,
            created_at="2026-06-26T00:00:00Z",
            status=PENDING,
        )
        self.pending.append(entry)
        self.pending_payloads.append(payload)
        return entry

    def append_ack(self, operation_id: str, payload: dict[str, Any] | None = None) -> WalEntry:
        self.events.append("wal_ack")
        if self.fail_ack:
            raise WalError("WAL_WRITE_FAILED", "wal", "WRITE_FAILED")
        entry = WalEntry(
            schema_version=SCHEMA_VERSION,
            sequence=len(self.pending) + len(self.acks) + 1,
            operation_id=operation_id,
            run_id=RUN_ID,
            operation="ack",
            payload_hash=payload_hash(payload or {"acknowledged_operation_id": operation_id}),
            payload=payload or {"acknowledged_operation_id": operation_id},
            created_at="2026-06-26T00:00:01Z",
            status="acknowledged",
        )
        self.acks.append(operation_id)
        return entry

    def replay_plan(self) -> list[WalEntry]:
        return [entry for entry in self.pending if entry.operation_id not in set(self.acks)]


def initializer(
    backend: FakeSheetsBackend,
    wal: Any,
    *,
    operation_id: str = "op-phase3c1",
    token: str = TOKEN,
) -> SheetsInitializer:
    return SheetsInitializer(
        backend=backend,
        lock_manager=lock_manager(backend, token=token),
        wal=wal,
        operation_id_factory=lambda: operation_id,
    )


def batch_kind(mutations: Sequence[Mutation]) -> str:
    if (
        len(mutations) == 1
        and isinstance(mutations[0], SetMetadata)
        and LOCK_OWNER in dict(mutations[0].values)
    ):
        return "lock_acquire_batch"
    if len(mutations) == 1 and isinstance(mutations[0], ClearMetadata):
        return "lock_release_batch"
    return "initializer_batch"


def record_backend_events(backend: FakeSheetsBackend, events: list[str]) -> None:
    original_apply = backend.apply_batch
    original_read = backend.read_spreadsheet
    read_count = 0

    def apply_batch(mutations: Sequence[Mutation]) -> Any:
        events.append(batch_kind(mutations))
        return original_apply(mutations)

    def read_spreadsheet() -> SpreadsheetState:
        nonlocal read_count
        read_count += 1
        events.append("fresh_state_read" if read_count == 1 else "state_read_back")
        return original_read()

    backend.apply_batch = apply_batch  # type: ignore[method-assign]
    backend.read_spreadsheet = read_spreadsheet  # type: ignore[method-assign]


def assert_order(events: Sequence[str], expected: Sequence[str]) -> None:
    positions = [events.index(item) for item in expected]
    assert positions == sorted(positions), events


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)
        assert TOKEN not in str(value)
        assert OTHER_TOKEN not in str(value)

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.sheets_backend import FakeSheetsBackend
from tests.unit.sheets_init_helpers import (
    COMMIT,
    OTHER_TOKEN,
    RUN_ID,
    SECRET,
    SpyWal,
    assert_no_secret,
    batch_kind,
    initializer,
    one_tab_schema,
    policy,
    record_backend_events,
    schema,
    state,
)
from webapp_debug_skill.sheets_client import (
    AddHeaders,
    BatchResult,
    CreateTab,
    Mutation,
    SheetsBackendError,
)
from webapp_debug_skill.sheets_init import (
    INIT_OPERATION,
    SCHEMA_VERSION_METADATA_KEY,
    InitExecutionError,
    InitOutcome,
    generate_init_plan,
)
from webapp_debug_skill.sheets_lock import LOCK_FIELDS, LOCK_OWNER, LOCK_RUN_ID
from webapp_debug_skill.wal import AppendOnlyWal, canonical_json


def pending_payload() -> dict[str, Any]:
    plan = generate_init_plan(one_tab_schema(), state())
    return plan.to_payload("fake-spreadsheet")


def pending_spy_wal(events: list[str] | None = None) -> SpyWal:
    wal = SpyWal(events=events)
    wal.append_pending(INIT_OPERATION, pending_payload(), operation_id="op-pending")
    wal.events.clear()
    return wal


def test_wal_pending_failure_prevents_initializer_backend_batch() -> None:
    events: list[str] = []
    backend = FakeSheetsBackend()
    record_backend_events(backend, events)
    wal = SpyWal(events=events, fail_pending=True)

    with pytest.raises(InitExecutionError) as exc_info:
        initializer(backend, wal).execute(one_tab_schema(), policy())

    assert exc_info.value.code == "SHEETS_INIT_WAL_FAILED"
    assert "initializer_batch" not in events
    assert wal.pending == []
    assert wal.acks == []


def test_batch_failure_before_apply_leaves_pending_wal_without_ack() -> None:
    events: list[str] = []
    backend = FakeSheetsBackend()
    original_apply = backend.apply_batch

    def apply_batch(mutations: Sequence[Mutation]) -> BatchResult:
        events.append(batch_kind(mutations))
        if batch_kind(mutations) == "initializer_batch":
            raise SheetsBackendError("SHEETS_BACKEND_IO_FAILED", "backend", "WRITE_FAILED")
        return original_apply(mutations)

    backend.apply_batch = apply_batch  # type: ignore[method-assign]
    wal = SpyWal(events=events)

    with pytest.raises(InitExecutionError) as exc_info:
        initializer(backend, wal).execute(one_tab_schema(), policy())

    assert exc_info.value.code == "SHEETS_BACKEND_IO_FAILED"
    assert len(wal.pending) == 1
    assert wal.acks == []
    assert backend.read_spreadsheet().tabs_dict() == {}


def test_ambiguous_write_with_successful_readback_is_acked_without_resend() -> None:
    backend = FakeSheetsBackend()
    original_apply = backend.apply_batch
    initializer_batches = 0

    def apply_batch(mutations: Sequence[Mutation]) -> BatchResult:
        nonlocal initializer_batches
        if batch_kind(mutations) == "initializer_batch":
            initializer_batches += 1
            original_apply(mutations)
            raise SheetsBackendError("SHEETS_BACKEND_IO_FAILED", "backend", "UNKNOWN_WRITE_RESULT")
        return original_apply(mutations)

    backend.apply_batch = apply_batch  # type: ignore[method-assign]
    wal = SpyWal()

    result = initializer(backend, wal).execute(one_tab_schema(), policy())

    assert result.outcome == InitOutcome.APPLIED
    assert result.recovered_after_ambiguous_write is True
    assert result.wal_acknowledged is True
    assert initializer_batches == 1


def test_ambiguous_write_with_no_postconditions_is_not_resent() -> None:
    backend = FakeSheetsBackend()
    original_apply = backend.apply_batch
    initializer_batches = 0
    wal = SpyWal()

    def apply_batch(mutations: Sequence[Mutation]) -> BatchResult:
        nonlocal initializer_batches
        if batch_kind(mutations) == "initializer_batch":
            initializer_batches += 1
            original_apply(mutations)
            backend.set_tabs_direct({})
            backend.clear_metadata_direct([SCHEMA_VERSION_METADATA_KEY])
            raise SheetsBackendError("SHEETS_BACKEND_IO_FAILED", "backend", "UNKNOWN_WRITE_RESULT")
        return original_apply(mutations)

    backend.apply_batch = apply_batch  # type: ignore[method-assign]

    with pytest.raises(InitExecutionError) as exc_info:
        initializer(backend, wal).execute(one_tab_schema(), policy())

    assert exc_info.value.code == "SHEETS_INIT_OUTCOME_UNKNOWN"
    assert exc_info.value.result is not None
    assert exc_info.value.result.wal_pending_written is True
    assert initializer_batches == 1
    assert wal.acks == []


def test_successful_batch_readback_io_failure_does_not_ack() -> None:
    backend = FakeSheetsBackend()
    original_read = backend.read_spreadsheet
    read_calls = 0
    wal = SpyWal()

    def read_spreadsheet() -> Any:
        nonlocal read_calls
        read_calls += 1
        if read_calls == 2:
            raise SheetsBackendError("SHEETS_BACKEND_IO_FAILED", "backend", "READ_FAILED")
        return original_read()

    backend.read_spreadsheet = read_spreadsheet  # type: ignore[method-assign]

    with pytest.raises(InitExecutionError) as exc_info:
        initializer(backend, wal).execute(one_tab_schema(), policy())

    assert exc_info.value.code == "SHEETS_BACKEND_IO_FAILED"
    assert len(wal.pending) == 1
    assert wal.acks == []


def test_readback_mismatch_does_not_ack() -> None:
    backend = FakeSheetsBackend()
    original_apply = backend.apply_batch
    wal = SpyWal()

    def apply_batch(mutations: Sequence[Mutation]) -> BatchResult:
        result = original_apply(mutations)
        if batch_kind(mutations) == "initializer_batch":
            backend.set_tabs_direct({"Features": ["Feature ID", "Unexpected", "Title"]})
        return result

    backend.apply_batch = apply_batch  # type: ignore[method-assign]

    with pytest.raises(InitExecutionError) as exc_info:
        initializer(backend, wal).execute(one_tab_schema(), policy())

    assert exc_info.value.code == "SHEETS_INIT_READBACK_MISMATCH"
    assert len(wal.pending) == 1
    assert wal.acks == []


def test_wal_ack_failure_is_not_success() -> None:
    backend = FakeSheetsBackend()
    wal = SpyWal(fail_ack=True)

    result = initializer(backend, wal).execute(one_tab_schema(), policy())

    assert result.outcome == InitOutcome.WAL_ACK_FAILED
    assert result.applied is True
    assert result.wal_acknowledged is False
    assert result.reason_code == "SHEETS_INIT_WAL_FAILED"


def test_release_lost_or_owner_mismatch_is_reported_without_clearing_other_owner() -> None:
    backend = FakeSheetsBackend()
    original_apply = backend.apply_batch

    def apply_batch(mutations: Sequence[Mutation]) -> BatchResult:
        result = original_apply(mutations)
        if batch_kind(mutations) == "initializer_batch":
            backend.set_metadata_direct({LOCK_OWNER: OTHER_TOKEN, LOCK_RUN_ID: "other-run"})
        return result

    backend.apply_batch = apply_batch  # type: ignore[method-assign]
    result = initializer(backend, SpyWal()).execute(one_tab_schema(), policy())

    assert result.outcome == InitOutcome.LOCK_RELEASE_FAILED
    assert result.reason_code == "SHEETS_LOCK_OWNER_MISMATCH"
    assert backend.read_metadata([LOCK_OWNER])[LOCK_OWNER] == OTHER_TOKEN

    lost_backend = FakeSheetsBackend()
    lost_original_apply = lost_backend.apply_batch

    def lost_apply_batch(mutations: Sequence[Mutation]) -> BatchResult:
        result = lost_original_apply(mutations)
        if batch_kind(mutations) == "initializer_batch":
            lost_backend.clear_metadata_direct(LOCK_FIELDS)
        return result

    lost_backend.apply_batch = lost_apply_batch  # type: ignore[method-assign]
    lost = initializer(lost_backend, SpyWal()).execute(one_tab_schema(), policy())

    assert lost.outcome == InitOutcome.LOCK_RELEASE_FAILED
    assert lost.reason_code == "SHEETS_LOCK_LOST"


def test_reconcile_pending_already_applied_acks_without_backend_write() -> None:
    events: list[str] = []
    backend = FakeSheetsBackend(
        metadata={SCHEMA_VERSION_METADATA_KEY: "1"},
        tabs={"Features": ["Feature ID", "Title"]},
    )
    record_backend_events(backend, events)
    wal = pending_spy_wal(events)

    result = initializer(backend, wal).reconcile_pending(one_tab_schema(), policy())

    assert result.outcome == InitOutcome.ALREADY_APPLIED
    assert result.wal_acknowledged is True
    assert wal.acks == ["op-pending"]
    assert "initializer_batch" not in events


def test_reconcile_pending_not_applied_returns_retry_required_without_write() -> None:
    events: list[str] = []
    backend = FakeSheetsBackend()
    record_backend_events(backend, events)
    wal = pending_spy_wal(events)

    result = initializer(backend, wal).reconcile_pending(one_tab_schema(), policy())

    assert result.outcome == InitOutcome.RETRY_REQUIRED
    assert wal.acks == []
    assert "initializer_batch" not in events


def test_reconcile_partial_postcondition_is_conflict_without_ack() -> None:
    backend = FakeSheetsBackend(metadata={SCHEMA_VERSION_METADATA_KEY: "1"})
    wal = pending_spy_wal()

    with pytest.raises(InitExecutionError) as exc_info:
        initializer(backend, wal).reconcile_pending(one_tab_schema(), policy())

    assert exc_info.value.code == "SHEETS_INIT_RECONCILIATION_CONFLICT"
    assert wal.acks == []


def test_reconcile_ignores_acked_operation() -> None:
    backend = FakeSheetsBackend()
    wal = pending_spy_wal()
    wal.acks.append("op-pending")

    result = initializer(backend, wal).reconcile_pending(one_tab_schema(), policy())

    assert result.outcome == InitOutcome.NOOP
    assert result.noop is True
    assert backend.write_count == 2


def write_real_pending_wal(path: Path) -> AppendOnlyWal:
    wal = AppendOnlyWal(
        path,
        RUN_ID,
        clock=lambda: "2026-06-26T00:00:00Z",
        fsync_func=lambda _fd: None,
    )
    wal.append_pending(INIT_OPERATION, pending_payload(), operation_id="op-real")
    return wal


def test_reconcile_rejects_wal_hash_mismatch_duplicate_and_incomplete(tmp_path: Path) -> None:
    for name, mutate in {
        "hash": lambda text: text.replace("Feature ID", "Changed ID", 1),
        "incomplete": lambda text: text.rstrip("\n"),
    }.items():
        path = tmp_path / f"{name}.jsonl"
        wal = write_real_pending_wal(path)
        path.write_text(mutate(path.read_text(encoding="utf-8")), encoding="utf-8")

        with pytest.raises(InitExecutionError) as exc_info:
            initializer(FakeSheetsBackend(), wal).reconcile_pending(one_tab_schema(), policy())

        assert exc_info.value.code == "SHEETS_INIT_WAL_FAILED"

    duplicate_path = tmp_path / "duplicate.jsonl"
    wal = write_real_pending_wal(duplicate_path)
    first = json.loads(duplicate_path.read_text(encoding="utf-8").splitlines()[0])
    duplicate = {**first, "sequence": 2}
    duplicate_path.write_text(
        canonical_json(first) + "\n" + canonical_json(duplicate) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(InitExecutionError) as duplicate_exc:
        initializer(FakeSheetsBackend(), wal).reconcile_pending(one_tab_schema(), policy())

    assert duplicate_exc.value.code == "SHEETS_INIT_WAL_FAILED"


def test_reconcile_lock_conflict_and_read_failure_are_safe() -> None:
    active_lock = {
        "writer_lock_owner": OTHER_TOKEN,
        "writer_lock_run_id": "other-run",
        "writer_lock_acquired_at": "2026-06-26T00:00:00Z",
        "writer_lock_expires_at": "2026-06-26T00:01:00Z",
        "writer_lock_commit_sha": COMMIT,
    }
    locked_backend = FakeSheetsBackend(metadata=active_lock)
    with pytest.raises(InitExecutionError) as lock_exc:
        initializer(locked_backend, pending_spy_wal()).reconcile_pending(schema(), policy())
    assert lock_exc.value.code == "SHEETS_LOCK_HELD"
    assert lock_exc.value.exit_code == 5

    failing_backend = FakeSheetsBackend()
    failing_backend.fail_reads = True
    with pytest.raises(InitExecutionError) as read_exc:
        initializer(failing_backend, pending_spy_wal()).reconcile_pending(schema(), policy())
    assert read_exc.value.code == "SHEETS_BACKEND_IO_FAILED"
    assert read_exc.value.exit_code == 4


def test_reconcile_and_execution_results_do_not_expose_secret_marker() -> None:
    backend = FakeSheetsBackend()
    original_apply = backend.apply_batch

    def apply_batch(mutations: Sequence[Mutation]) -> BatchResult:
        if any(isinstance(mutation, (CreateTab, AddHeaders)) for mutation in mutations):
            raise SheetsBackendError("SHEETS_BACKEND_IO_FAILED", "backend", SECRET)
        return original_apply(mutations)

    backend.apply_batch = apply_batch  # type: ignore[method-assign]

    with pytest.raises(InitExecutionError) as exc_info:
        initializer(backend, SpyWal()).execute(one_tab_schema(), policy())

    assert_no_secret(exc_info.value, exc_info.value.path, exc_info.value.reason)

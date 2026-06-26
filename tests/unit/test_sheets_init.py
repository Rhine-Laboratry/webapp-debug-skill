from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.sheets_backend import FakeSheetsBackend
from tests.unit.sheets_init_helpers import (
    COMMIT,
    RUN_ID,
    SECRET,
    SpyWal,
    assert_no_secret,
    assert_order,
    initializer,
    one_tab_schema,
    policy,
    record_backend_events,
    schema,
    schema_mapping,
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
    SCHEMA_VERSION_METADATA_KEY,
    InitOutcome,
    InitPlanningError,
    canonical_schema_from_mapping,
    generate_init_plan,
    mutation_to_payload,
)
from webapp_debug_skill.wal import AppendOnlyWal


def payload_types(mutations: Sequence[Mutation]) -> list[dict[str, Any]]:
    return [mutation_to_payload(mutation) for mutation in mutations]


def test_missing_tabs_produce_deterministic_canonical_plan() -> None:
    plan = generate_init_plan(schema(), state())

    assert plan.outcome == InitOutcome.READY
    assert payload_types(plan.mutations) == [
        {"type": "create_tab", "name": "Features"},
        {
            "type": "add_headers",
            "tab_name": "Features",
            "headers": ["Feature ID", "Title", "Status"],
        },
        {"type": "create_tab", "name": "Scenarios"},
        {
            "type": "add_headers",
            "tab_name": "Scenarios",
            "headers": ["Scenario ID", "Feature ID", "Steps"],
        },
        {
            "type": "set_metadata",
            "values": {SCHEMA_VERSION_METADATA_KEY: "1"},
        },
    ]
    assert plan.fingerprint == generate_init_plan(schema(), state()).fingerprint


def test_existing_empty_tab_gets_all_headers() -> None:
    plan = generate_init_plan(one_tab_schema(), state(tabs={"Features": []}))

    assert payload_types(plan.mutations) == [
        {"type": "add_headers", "tab_name": "Features", "headers": ["Feature ID", "Title"]},
        {"type": "set_metadata", "values": {SCHEMA_VERSION_METADATA_KEY: "1"}},
    ]


def test_existing_strict_prefix_gets_missing_suffix_only() -> None:
    plan = generate_init_plan(
        one_tab_schema(("Feature ID", "Title", "Status")),
        state(tabs={"Features": ["Feature ID"]}),
    )

    assert payload_types(plan.mutations) == [
        {"type": "add_headers", "tab_name": "Features", "headers": ["Title", "Status"]},
        {"type": "set_metadata", "values": {SCHEMA_VERSION_METADATA_KEY: "1"}},
    ]


def test_exact_headers_and_schema_version_are_noop() -> None:
    plan = generate_init_plan(
        one_tab_schema(),
        state(
            metadata={SCHEMA_VERSION_METADATA_KEY: "1"},
            tabs={"Features": ["Feature ID", "Title"]},
        ),
    )

    assert plan.outcome == InitOutcome.NOOP
    assert plan.noop is True
    assert plan.mutations == ()


def test_unknown_trailing_column_and_unknown_tab_are_preserved() -> None:
    plan = generate_init_plan(
        one_tab_schema(),
        state(
            metadata={SCHEMA_VERSION_METADATA_KEY: "1", "human_note": "keep"},
            tabs={
                "Features": ["Feature ID", "Title", "Human Notes"],
                "Human Tab": ["Do Not Touch"],
            },
        ),
    )

    assert plan.mutations == ()


def test_schema_version_missing_same_and_upgrade_rules() -> None:
    target = one_tab_schema()

    missing = generate_init_plan(target, state(tabs={"Features": ["Feature ID", "Title"]}))
    same = generate_init_plan(
        target,
        state(
            metadata={SCHEMA_VERSION_METADATA_KEY: "1"}, tabs={"Features": ["Feature ID", "Title"]}
        ),
    )
    upgrade = generate_init_plan(
        schema(version=2, tabs=(("Features", ("Feature ID", "Title")),)),
        state(
            metadata={SCHEMA_VERSION_METADATA_KEY: "1"}, tabs={"Features": ["Feature ID", "Title"]}
        ),
    )

    assert payload_types(missing.mutations) == [
        {"type": "set_metadata", "values": {SCHEMA_VERSION_METADATA_KEY: "1"}}
    ]
    assert same.mutations == ()
    assert payload_types(upgrade.mutations) == [
        {"type": "set_metadata", "values": {SCHEMA_VERSION_METADATA_KEY: "2"}}
    ]


def test_schema_version_downgrade_is_rejected() -> None:
    with pytest.raises(InitPlanningError) as exc_info:
        generate_init_plan(
            one_tab_schema(),
            state(
                metadata={SCHEMA_VERSION_METADATA_KEY: "2"},
                tabs={"Features": ["Feature ID", "Title"]},
            ),
        )

    assert exc_info.value.code == "SHEETS_SCHEMA_DOWNGRADE_BLOCKED"
    assert exc_info.value.exit_code == 3


@pytest.mark.parametrize("version", ["1.0", "+1", " 1 ", True, False, -1])
def test_invalid_schema_version_values_are_rejected(version: object) -> None:
    with pytest.raises(InitPlanningError) as exc_info:
        generate_init_plan(
            one_tab_schema(),
            state(
                metadata={
                    SCHEMA_VERSION_METADATA_KEY: str(version)
                    if isinstance(version, bool)
                    else version
                },
                tabs={"Features": ["Feature ID", "Title"]},
            ),
        )

    assert exc_info.value.code == "SHEETS_SCHEMA_VERSION_INVALID"


@pytest.mark.parametrize(
    "headers",
    [
        ["Title", "Feature ID"],
        ["Feature ID", "Unknown", "Title"],
        ["Feature ID", "Unknown"],
        ["Feature ID", "Title", "Feature ID"],
        ["Feature ID", ""],
        ["feature id", "Title"],
    ],
)
def test_header_conflicts_are_rejected(headers: list[str]) -> None:
    with pytest.raises(InitPlanningError) as exc_info:
        generate_init_plan(one_tab_schema(), state(tabs={"Features": headers}))

    assert exc_info.value.code == "SHEETS_SCHEMA_CONFLICT"


def test_canonical_duplicate_empty_and_formula_like_headers_are_rejected() -> None:
    duplicate = schema_mapping(tabs=(("Features", ("Feature ID", "Feature ID")),))
    empty = schema_mapping(tabs=(("Features", ("Feature ID", "")),))
    formula = schema_mapping(tabs=(("Features", ("Feature ID", " \t=SUM(A1:A2)")),))

    with pytest.raises(InitPlanningError) as duplicate_exc:
        canonical_schema_from_mapping(duplicate)
    with pytest.raises(InitPlanningError) as empty_exc:
        canonical_schema_from_mapping(empty)
    with pytest.raises(InitPlanningError) as formula_exc:
        canonical_schema_from_mapping(formula)

    assert duplicate_exc.value.reason == "DUPLICATE_COLUMN"
    assert empty_exc.value.code == "SHEETS_INIT_UNSAFE_HEADER"
    assert formula_exc.value.code == "SHEETS_INIT_UNSAFE_HEADER"


def test_plan_payload_rejects_secret_marker_without_leaking_it() -> None:
    plan = generate_init_plan(one_tab_schema(("Feature ID", SECRET)), state())

    with pytest.raises(InitPlanningError) as exc_info:
        _ = plan.fingerprint

    assert_no_secret(exc_info.value, exc_info.value.path, exc_info.value.reason)


def test_read_only_plan_has_no_writes_lock_wal_or_operation_id() -> None:
    backend = FakeSheetsBackend()

    def fail_operation_id() -> str:
        raise AssertionError("operation id should not be generated for read-only planning")

    init = initializer(backend, wal=object(), operation_id="unused")
    init.operation_id_factory = fail_operation_id

    plan = init.plan_read_only(one_tab_schema())

    assert plan.advisory is True
    assert backend.read_count == 1
    assert backend.write_count == 0


def test_execute_happy_path_orders_lock_wal_batch_readback_ack_release() -> None:
    events: list[str] = []
    backend = FakeSheetsBackend()
    record_backend_events(backend, events)
    wal = SpyWal(events=events)

    result = initializer(backend, wal).execute(schema(), policy())

    assert result.outcome == InitOutcome.APPLIED
    assert result.applied is True
    assert result.read_back_verified is True
    assert result.wal_pending_written is True
    assert result.wal_acknowledged is True
    assert result.lock_released is True
    assert result.applied_mutation_count == 5
    assert backend.write_count == 3
    assert len(backend.applied_batches[1]) == 5
    assert_order(
        events,
        [
            "lock_acquire_batch",
            "fresh_state_read",
            "wal_pending",
            "initializer_batch",
            "state_read_back",
            "wal_ack",
            "lock_release_batch",
        ],
    )


def test_pending_wal_is_fsynced_before_initializer_backend_write(tmp_path: Path) -> None:
    events: list[str] = []
    backend = FakeSheetsBackend()
    original_apply = backend.apply_batch
    fsync_count = 0

    def fsync(_fd: int) -> None:
        nonlocal fsync_count
        fsync_count += 1
        events.append("wal_pending_fsync" if fsync_count == 1 else "wal_ack_fsync")

    def apply_batch(mutations: Sequence[Mutation]) -> BatchResult:
        if any(isinstance(mutation, (CreateTab, AddHeaders)) for mutation in mutations):
            events.append("initializer_batch")
        return original_apply(mutations)

    backend.apply_batch = apply_batch  # type: ignore[method-assign]
    wal = AppendOnlyWal(
        tmp_path / "wal.jsonl",
        RUN_ID,
        clock=lambda: "2026-06-26T00:00:00Z",
        fsync_func=fsync,
    )

    result = initializer(backend, wal, operation_id="op-fsync").execute(one_tab_schema(), policy())

    assert result.wal_acknowledged is True
    assert_order(events, ["wal_pending_fsync", "initializer_batch", "wal_ack_fsync"])


def test_second_execute_is_noop_without_initializer_wal_or_batch() -> None:
    backend = FakeSheetsBackend()
    first_wal = SpyWal()
    initializer(backend, first_wal, operation_id="op-1").execute(one_tab_schema(), policy())
    writes_after_first = backend.write_count

    events: list[str] = []
    record_backend_events(backend, events)
    second_wal = SpyWal(events=events)

    result = initializer(backend, second_wal, operation_id="op-2").execute(
        one_tab_schema(), policy()
    )

    assert result.outcome == InitOutcome.NOOP
    assert result.noop is True
    assert second_wal.pending == []
    assert second_wal.acks == []
    assert "initializer_batch" not in events
    assert backend.write_count == writes_after_first + 2


def test_noop_still_honors_active_lock_policy() -> None:
    backend = FakeSheetsBackend(
        metadata={
            SCHEMA_VERSION_METADATA_KEY: "1",
            "writer_lock_owner": "other",
            "writer_lock_run_id": "other-run",
            "writer_lock_acquired_at": "2026-06-26T00:00:00Z",
            "writer_lock_expires_at": "2026-06-26T00:01:00Z",
            "writer_lock_commit_sha": COMMIT,
        },
        tabs={"Features": ["Feature ID", "Title"]},
    )

    with pytest.raises(Exception) as exc_info:
        initializer(backend, wal=object()).execute(one_tab_schema(), policy())

    assert getattr(exc_info.value, "code") == "SHEETS_LOCK_HELD"
    assert getattr(exc_info.value, "exit_code") == 5
    assert backend.write_count == 0


def test_execute_reloads_state_after_lock_and_replans() -> None:
    backend = FakeSheetsBackend()
    hook_calls = 0

    def after_apply(fake: FakeSheetsBackend) -> None:
        nonlocal hook_calls
        hook_calls += 1
        if hook_calls == 1:
            fake.set_tabs_direct({"Features": ["Feature ID", "Title"]})
            fake.set_metadata_direct({SCHEMA_VERSION_METADATA_KEY: "1"})

    backend.after_apply_hook = after_apply
    result = initializer(backend, SpyWal()).execute(one_tab_schema(), policy())

    assert result.outcome == InitOutcome.NOOP
    assert result.planned_mutation_count == 0
    assert backend.write_count == 2


def test_backend_raw_secret_reason_is_not_exposed() -> None:
    backend = FakeSheetsBackend()
    original_apply = backend.apply_batch

    def apply_batch(mutations: Sequence[Mutation]) -> BatchResult:
        if any(isinstance(mutation, (CreateTab, AddHeaders)) for mutation in mutations):
            raise SheetsBackendError("SHEETS_BACKEND_IO_FAILED", "backend", SECRET)
        return original_apply(mutations)

    backend.apply_batch = apply_batch  # type: ignore[method-assign]

    with pytest.raises(Exception) as exc_info:
        initializer(backend, SpyWal()).execute(
            one_tab_schema(),
            policy(),
        )

    assert_no_secret(exc_info.value, getattr(exc_info.value, "reason", ""))

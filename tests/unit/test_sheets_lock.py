from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes.sheets_backend import FakeSheetsBackend
from webapp_debug_skill.sheets_lock import (
    LOCK_ACQUIRED_AT,
    LOCK_COMMIT_SHA,
    LOCK_EXPIRES_AT,
    LOCK_FIELDS,
    LOCK_OWNER,
    LOCK_RUN_ID,
    LockLease,
    SheetsLockError,
    SheetsLockManager,
    format_rfc3339,
)


TOKEN = "full-owner-token-do-not-print"
OTHER_TOKEN = "other-owner-token-do-not-print"
RUN_ID = "run-1"
OTHER_RUN_ID = "run-2"
NOW = datetime(2026, 6, 26, 0, 0, 0, tzinfo=UTC)
COMMIT = "abc123"


def manager(
    backend: FakeSheetsBackend, now: datetime = NOW, token: str = TOKEN
) -> SheetsLockManager:
    return SheetsLockManager(backend, now_factory=lambda: now, token_factory=lambda: token)


def lock_metadata(
    *,
    owner: str = OTHER_TOKEN,
    run_id: str = OTHER_RUN_ID,
    acquired_at: datetime = NOW - timedelta(minutes=10),
    expires_at: datetime = NOW + timedelta(minutes=10),
    commit_sha: str = COMMIT,
) -> dict[str, str]:
    return {
        LOCK_OWNER: owner,
        LOCK_RUN_ID: run_id,
        LOCK_ACQUIRED_AT: format_rfc3339(acquired_at),
        LOCK_EXPIRES_AT: format_rfc3339(expires_at),
        LOCK_COMMIT_SHA: commit_sha,
    }


def acquire_default(backend: FakeSheetsBackend) -> LockLease:
    return manager(backend).acquire(run_id=RUN_ID, ttl=timedelta(minutes=30), commit_sha=COMMIT)


def assert_no_token(*values: object) -> None:
    for value in values:
        rendered = str(value)
        assert TOKEN not in rendered
        assert OTHER_TOKEN not in rendered


def test_acquire_from_empty_state_writes_five_fields_in_one_atomic_batch() -> None:
    backend = FakeSheetsBackend()

    lease = acquire_default(backend)

    assert lease.owner_token == TOKEN
    assert lease.run_id == RUN_ID
    assert lease.acquired_at == NOW
    assert lease.expires_at == NOW + timedelta(minutes=30)
    assert backend.write_count == 1
    assert len(backend.applied_batches) == 1
    batch = backend.applied_batches[0]
    assert len(batch) == 1
    values = dict(batch[0].values)  # type: ignore[attr-defined]
    assert set(values) == set(LOCK_FIELDS)
    assert backend.read_metadata(LOCK_FIELDS)[LOCK_OWNER] == TOKEN


def test_active_lock_blocks_without_write() -> None:
    backend = FakeSheetsBackend(metadata=lock_metadata())

    with pytest.raises(SheetsLockError) as exc_info:
        acquire_default(backend)

    assert exc_info.value.code == "SHEETS_LOCK_HELD"
    assert exc_info.value.exit_code == 5
    assert backend.write_count == 0


def test_now_equal_expires_at_is_expired_and_can_be_acquired() -> None:
    backend = FakeSheetsBackend(metadata=lock_metadata(expires_at=NOW))

    lease = acquire_default(backend)

    assert lease.owner_token == TOKEN
    assert backend.read_metadata(LOCK_FIELDS)[LOCK_OWNER] == TOKEN


def test_expired_lock_is_replaced() -> None:
    backend = FakeSheetsBackend(metadata=lock_metadata(expires_at=NOW - timedelta(seconds=1)))

    lease = acquire_default(backend)

    assert lease.owner_token == TOKEN
    assert backend.write_count == 1


def test_partial_lock_is_rejected() -> None:
    backend = FakeSheetsBackend(metadata={LOCK_OWNER: OTHER_TOKEN})

    with pytest.raises(SheetsLockError) as exc_info:
        acquire_default(backend)

    assert exc_info.value.code == "SHEETS_LOCK_CORRUPT"
    assert backend.write_count == 0


@pytest.mark.parametrize(
    "metadata",
    [
        {**lock_metadata(), LOCK_ACQUIRED_AT: "not-a-time"},
        {
            **lock_metadata(
                acquired_at=NOW,
                expires_at=NOW - timedelta(seconds=1),
            )
        },
    ],
)
def test_corrupt_timestamp_states_are_rejected(metadata: dict[str, str]) -> None:
    backend = FakeSheetsBackend(metadata=metadata)

    with pytest.raises(SheetsLockError) as exc_info:
        acquire_default(backend)

    assert exc_info.value.code == "SHEETS_LOCK_CORRUPT"
    assert backend.write_count == 0


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_ttl_zero_or_negative_is_rejected(ttl: timedelta) -> None:
    backend = FakeSheetsBackend()

    with pytest.raises(SheetsLockError) as exc_info:
        manager(backend).acquire(run_id=RUN_ID, ttl=ttl, commit_sha=COMMIT)

    assert exc_info.value.code == "SHEETS_LOCK_INVALID_TTL"
    assert exc_info.value.exit_code == 2
    assert backend.write_count == 0


def test_naive_datetime_is_rejected() -> None:
    backend = FakeSheetsBackend()
    naive = datetime(2026, 6, 26, 0, 0, 0)

    with pytest.raises(SheetsLockError) as exc_info:
        manager(backend, now=naive).acquire(
            run_id=RUN_ID, ttl=timedelta(minutes=1), commit_sha=COMMIT
        )

    assert exc_info.value.reason == "NAIVE_DATETIME"
    assert backend.write_count == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (LOCK_OWNER, OTHER_TOKEN),
        (LOCK_RUN_ID, OTHER_RUN_ID),
    ],
)
def test_readback_mismatch_after_write_is_detected(field: str, value: str) -> None:
    backend = FakeSheetsBackend()

    def race(fake: FakeSheetsBackend) -> None:
        fake.set_metadata_direct({field: value})

    backend.after_apply_hook = race
    with pytest.raises(SheetsLockError) as exc_info:
        acquire_default(backend)

    assert exc_info.value.code == "SHEETS_LOCK_RACE"
    assert exc_info.value.exit_code == 5
    assert backend.write_count == 1
    assert_no_token(exc_info.value, exc_info.value.path, exc_info.value.reason)


def test_backend_read_and_write_failures_are_mapped() -> None:
    read_backend = FakeSheetsBackend()
    read_backend.fail_reads = True
    with pytest.raises(SheetsLockError) as read_exc:
        acquire_default(read_backend)
    assert read_exc.value.code == "SHEETS_BACKEND_IO_FAILED"
    assert read_exc.value.exit_code == 4

    write_backend = FakeSheetsBackend()
    write_backend.fail_before_apply = True
    with pytest.raises(SheetsLockError) as write_exc:
        acquire_default(write_backend)
    assert write_exc.value.code == "SHEETS_BACKEND_IO_FAILED"
    assert write_exc.value.exit_code == 4


def test_lock_storage_unavailable_is_blocked_without_bootstrap() -> None:
    backend = FakeSheetsBackend(metadata_available=False)

    with pytest.raises(SheetsLockError) as exc_info:
        acquire_default(backend)

    assert exc_info.value.code == "SHEETS_LOCK_STORAGE_UNAVAILABLE"
    assert backend.write_count == 0


def test_release_by_correct_owner_clears_all_fields_in_one_batch() -> None:
    backend = FakeSheetsBackend()
    lease = acquire_default(backend)

    manager(backend).release(lease)

    assert backend.write_count == 2
    clear_batch = backend.applied_batches[-1]
    assert len(clear_batch) == 1
    assert set(clear_batch[0].keys) == set(LOCK_FIELDS)  # type: ignore[attr-defined]
    assert backend.read_metadata(LOCK_FIELDS) == {}


@pytest.mark.parametrize(
    "lease",
    [
        LockLease(
            owner_token="not-the-owner",
            run_id=RUN_ID,
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            commit_sha=COMMIT,
        ),
        LockLease(
            owner_token=TOKEN,
            run_id="wrong-run",
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            commit_sha=COMMIT,
        ),
    ],
)
def test_release_owner_or_run_mismatch_does_not_clear(lease: LockLease) -> None:
    backend = FakeSheetsBackend(metadata=lock_metadata(owner=TOKEN, run_id=RUN_ID))

    with pytest.raises(SheetsLockError) as exc_info:
        manager(backend).release(lease)

    assert exc_info.value.code == "SHEETS_LOCK_OWNER_MISMATCH"
    assert backend.write_count == 0
    assert backend.read_metadata(LOCK_FIELDS)[LOCK_OWNER] == TOKEN


def test_release_missing_lock_is_rejected() -> None:
    backend = FakeSheetsBackend()
    lease = LockLease(
        owner_token=TOKEN,
        run_id=RUN_ID,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        commit_sha=COMMIT,
    )

    with pytest.raises(SheetsLockError) as exc_info:
        manager(backend).release(lease)

    assert exc_info.value.code == "SHEETS_LOCK_LOST"
    assert backend.write_count == 0


def test_release_detects_clear_readback_race() -> None:
    backend = FakeSheetsBackend()
    lease = acquire_default(backend)

    def race(fake: FakeSheetsBackend) -> None:
        fake.set_metadata_direct({LOCK_OWNER: OTHER_TOKEN})

    backend.after_apply_hook = race
    with pytest.raises(SheetsLockError) as exc_info:
        manager(backend).release(lease)

    assert exc_info.value.code == "SHEETS_LOCK_RACE"
    assert backend.write_count == 2


def test_release_does_not_clear_other_owner_even_when_expired() -> None:
    backend = FakeSheetsBackend(
        metadata=lock_metadata(
            owner=OTHER_TOKEN, run_id=OTHER_RUN_ID, expires_at=NOW - timedelta(minutes=1)
        )
    )
    lease = LockLease(
        owner_token=TOKEN,
        run_id=RUN_ID,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        commit_sha=COMMIT,
    )

    with pytest.raises(SheetsLockError) as exc_info:
        manager(backend).release(lease)

    assert exc_info.value.code == "SHEETS_LOCK_OWNER_MISMATCH"
    assert backend.write_count == 0
    assert backend.read_metadata(LOCK_FIELDS)[LOCK_OWNER] == OTHER_TOKEN


def test_owner_token_is_not_leaked_to_exception_stdout_stderr_or_json_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = FakeSheetsBackend()

    def race(fake: FakeSheetsBackend) -> None:
        fake.set_metadata_direct({LOCK_OWNER: OTHER_TOKEN})

    backend.after_apply_hook = race
    with pytest.raises(SheetsLockError) as exc_info:
        acquire_default(backend)
    captured = capsys.readouterr()
    safe_details = {
        "code": exc_info.value.code,
        "path": exc_info.value.path,
        "reason": exc_info.value.reason,
    }

    assert_no_token(exc_info.value, captured.out, captured.err, json.dumps(safe_details))


def test_lock_tests_do_not_use_external_services(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def fail_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", fail_socket)
    backend = FakeSheetsBackend()

    lease = acquire_default(backend)
    manager(backend).release(lease)

    assert backend.read_metadata(LOCK_FIELDS) == {}

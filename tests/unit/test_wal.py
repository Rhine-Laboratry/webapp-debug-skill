from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from webapp_debug_skill.wal import (
    ACKNOWLEDGED,
    PENDING,
    AppendOnlyWal,
    WalError,
    canonical_json,
    default_wal_path,
    parse_wal_text,
    payload_hash,
)


SECRET = "SECRET_MARKER_DO_NOT_LEAK"


def safe_payload(value: str = "ok") -> dict[str, Any]:
    return {"values": [{"message": value, "token": "<REDACTED:TOKEN>"}]}


def make_wal(tmp_path: Path, fsync_calls: list[int] | None = None) -> AppendOnlyWal:
    return AppendOnlyWal(
        tmp_path / "run.jsonl",
        "run-1",
        clock=lambda: "2026-06-25T00:00:00Z",
        uuid_factory=lambda: "operation-1",
        fsync_func=(lambda fd: fsync_calls.append(fd))
        if fsync_calls is not None
        else (lambda _fd: None),
    )


def entry_dict(
    sequence: int,
    operation_id: str,
    status: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload or safe_payload()
    return {
        "schema_version": 1,
        "sequence": sequence,
        "operation_id": operation_id,
        "run_id": "run-1",
        "operation": "sheets.batch_update" if status == PENDING else "sheets.batch_update.ack",
        "payload_hash": payload_hash(payload),
        "payload": payload,
        "created_at": "2026-06-25T00:00:00Z",
        "status": status,
    }


def write_entries(path: Path, entries: list[dict[str, Any]], trailing_newline: bool = True) -> None:
    text = "".join(canonical_json(entry) + "\n" for entry in entries)
    if not trailing_newline:
        text = text.rstrip("\n")
    path.write_text(text, encoding="utf-8")


def assert_no_secret(*values: str | bytes) -> None:
    for value in values:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        assert SECRET not in value


def test_default_wal_path() -> None:
    assert default_wal_path(Path("/repo"), "run-1") == Path(
        "/repo/.webapp-debug/state/wal/run-1.jsonl"
    )


def test_pending_append_and_fsync(tmp_path: Path) -> None:
    fsync_calls: list[int] = []
    wal = make_wal(tmp_path, fsync_calls)

    entry = wal.append_pending("sheets.batch_update", safe_payload())

    assert entry.sequence == 1
    assert entry.status == PENDING
    assert fsync_calls
    text = wal.path.read_text(encoding="utf-8")
    assert '"status":"pending"' in text
    assert_no_secret(text)


def test_ack_is_appended_without_rewriting_past_rows(tmp_path: Path) -> None:
    wal = make_wal(tmp_path)
    pending = wal.append_pending("sheets.batch_update", safe_payload())
    before = wal.path.read_text(encoding="utf-8")

    ack = wal.append_ack(pending.operation_id)
    after = wal.path.read_text(encoding="utf-8")

    assert ack.sequence == 2
    assert ack.status == ACKNOWLEDGED
    assert after.startswith(before)
    assert len(after.splitlines()) == 2


def test_hash_mismatch_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    entry = entry_dict(1, "op-1", PENDING)
    entry["payload_hash"] = "sha256:bad"
    write_entries(path, [entry])

    wal = AppendOnlyWal(path, "run-1")

    with pytest.raises(WalError) as exc_info:
        wal.read_entries()
    assert exc_info.value.code == "WAL_HASH_MISMATCH"


@pytest.mark.parametrize(
    "entries",
    [
        [entry_dict(1, "op-1", PENDING), entry_dict(3, "op-2", PENDING)],
        [entry_dict(1, "op-1", PENDING), entry_dict(1, "op-2", PENDING)],
        [entry_dict(2, "op-1", PENDING)],
    ],
)
def test_sequence_missing_duplicate_or_reversed_is_detected(
    tmp_path: Path, entries: list[dict[str, Any]]
) -> None:
    path = tmp_path / "run.jsonl"
    write_entries(path, entries)
    wal = AppendOnlyWal(path, "run-1")

    with pytest.raises(WalError) as exc_info:
        wal.read_entries()

    assert exc_info.value.code == "WAL_SEQUENCE_INVALID"


def test_operation_id_duplicate_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    write_entries(path, [entry_dict(1, "op-1", PENDING), entry_dict(2, "op-1", PENDING)])
    wal = AppendOnlyWal(path, "run-1")

    with pytest.raises(WalError) as exc_info:
        wal.read_entries()

    assert exc_info.value.code == "WAL_OPERATION_DUPLICATE"


def test_ack_unknown_operation_and_duplicate_ack_are_detected(tmp_path: Path) -> None:
    unknown_text = canonical_json(entry_dict(1, "op-1", ACKNOWLEDGED)) + "\n"
    with pytest.raises(WalError) as unknown_exc:
        parse_wal_text(unknown_text)
    assert unknown_exc.value.reason == "ACK_WITHOUT_PENDING"

    path = tmp_path / "run.jsonl"
    write_entries(
        path,
        [
            entry_dict(1, "op-1", PENDING),
            entry_dict(2, "op-1", ACKNOWLEDGED),
            entry_dict(3, "op-1", ACKNOWLEDGED),
        ],
    )
    wal = AppendOnlyWal(path, "run-1")
    with pytest.raises(WalError) as duplicate_exc:
        wal.read_entries()
    assert duplicate_exc.value.reason == "DUPLICATE_ACK"


def test_incomplete_final_line_is_blocked(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    write_entries(path, [entry_dict(1, "op-1", PENDING)], trailing_newline=False)
    wal = AppendOnlyWal(path, "run-1")

    with pytest.raises(WalError) as exc_info:
        wal.read_entries()

    assert exc_info.value.code == "WAL_INCOMPLETE"


def test_partial_write_failure_is_reported_safely(tmp_path: Path) -> None:
    class BrokenHandle:
        def __enter__(self) -> "BrokenHandle":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def write(self, _line: str) -> None:
            raise OSError(SECRET)

        def flush(self) -> None:
            return None

        def fileno(self) -> int:
            return 1

    def broken_open(*_args: object, **_kwargs: object) -> BrokenHandle:
        return BrokenHandle()

    wal = AppendOnlyWal(tmp_path / "run.jsonl", "run-1", opener=broken_open)

    with pytest.raises(WalError) as exc_info:
        wal.append_pending("sheets.batch_update", safe_payload())

    assert exc_info.value.code == "WAL_WRITE_FAILED"
    assert_no_secret(str(exc_info.value))


def test_unredacted_payload_is_rejected_before_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wal = make_wal(tmp_path)

    with pytest.raises(WalError) as exc_info:
        wal.append_pending("sheets.batch_update", {"token": SECRET})
    captured = capsys.readouterr()

    assert exc_info.value.code == "WAL_PAYLOAD_NOT_REDACTED"
    assert not wal.path.exists()
    assert_no_secret(captured.out, captured.err, str(exc_info.value))


def test_secret_marker_is_not_written_to_wal(tmp_path: Path) -> None:
    wal = make_wal(tmp_path)

    wal.append_pending("sheets.batch_update", safe_payload())

    assert_no_secret(wal.path.read_text(encoding="utf-8"))


def test_replay_plan_excludes_acknowledged_operations(tmp_path: Path) -> None:
    wal = AppendOnlyWal(
        tmp_path / "run.jsonl",
        "run-1",
        clock=lambda: "2026-06-25T00:00:00Z",
        uuid_factory=lambda: "op-1",
        fsync_func=lambda _fd: None,
    )
    op1 = wal.append_pending("sheets.batch_update", safe_payload("one"), operation_id="op-1")
    op2 = wal.append_pending("sheets.batch_update", safe_payload("two"), operation_id="op-2")
    wal.append_ack(op1.operation_id)

    plan = wal.replay_plan()

    assert [entry.operation_id for entry in plan] == [op2.operation_id]


def test_wal_reading_does_not_execute_external_mutation(tmp_path: Path) -> None:
    wal = make_wal(tmp_path)
    wal.append_pending("sheets.batch_update", safe_payload())

    plan = wal.replay_plan()

    assert len(plan) == 1
    assert plan[0].operation == "sheets.batch_update"

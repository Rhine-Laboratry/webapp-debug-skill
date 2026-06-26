"""Append-only WAL support for webapp-debug local state."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from webapp_debug_skill.redaction import secret_findings

SCHEMA_VERSION = 1
PENDING = "pending"
ACKNOWLEDGED = "acknowledged"


class WalError(RuntimeError):
    """Safe WAL failure with a reason code."""

    def __init__(self, code: str, path: str, reason: str) -> None:
        super().__init__(code)
        self.code = code
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class WalEntry:
    """A validated WAL entry."""

    schema_version: int
    sequence: int
    operation_id: str
    run_id: str
    operation: str
    payload_hash: str
    payload: dict[str, Any]
    created_at: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable entry."""

        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "operation_id": self.operation_id,
            "run_id": self.run_id,
            "operation": self.operation,
            "payload_hash": self.payload_hash,
            "payload": self.payload,
            "created_at": self.created_at,
            "status": self.status,
        }


def default_wal_path(root: Path, run_id: str) -> Path:
    """Return the default WAL path for a run id."""

    return root / ".webapp-debug/state/wal" / f"{run_id}.jsonl"


def canonical_json(value: Any) -> str:
    """Return canonical JSON for hashing."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the sha256 hash of canonical payload JSON."""

    import hashlib

    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def default_clock() -> str:
    """Return a UTC ISO timestamp."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_payload_redacted(payload: Mapping[str, Any]) -> None:
    """Reject payloads that still look like they contain raw secrets."""

    findings = secret_findings(payload)
    if findings:
        path, reason = findings[0]
        raise WalError("WAL_PAYLOAD_NOT_REDACTED", path, reason)


def validate_entry_shape(raw: Any) -> WalEntry:
    """Validate a raw JSON object as a WAL entry."""

    if not isinstance(raw, dict):
        raise WalError("WAL_ENTRY_INVALID", "$", "ENTRY_OBJECT_REQUIRED")
    required = {
        "schema_version",
        "sequence",
        "operation_id",
        "run_id",
        "operation",
        "payload_hash",
        "payload",
        "created_at",
        "status",
    }
    missing = sorted(required - set(raw))
    if missing:
        raise WalError("WAL_ENTRY_INVALID", missing[0], "REQUIRED")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise WalError("WAL_ENTRY_INVALID", "schema_version", "INVALID_CONST")
    if not isinstance(raw["sequence"], int) or raw["sequence"] < 1:
        raise WalError("WAL_ENTRY_INVALID", "sequence", "INVALID_SEQUENCE")
    for key in ("operation_id", "run_id", "operation", "payload_hash", "created_at", "status"):
        if not isinstance(raw[key], str) or raw[key] == "":
            raise WalError("WAL_ENTRY_INVALID", key, "INVALID_STRING")
    if raw["status"] not in {PENDING, ACKNOWLEDGED}:
        raise WalError("WAL_ENTRY_INVALID", "status", "INVALID_STATUS")
    if not isinstance(raw["payload"], dict):
        raise WalError("WAL_ENTRY_INVALID", "payload", "INVALID_PAYLOAD")
    expected_hash = payload_hash(raw["payload"])
    if raw["payload_hash"] != expected_hash:
        raise WalError("WAL_HASH_MISMATCH", "payload_hash", "HASH_MISMATCH")
    validate_payload_redacted(raw["payload"])
    return WalEntry(
        schema_version=raw["schema_version"],
        sequence=raw["sequence"],
        operation_id=raw["operation_id"],
        run_id=raw["run_id"],
        operation=raw["operation"],
        payload_hash=raw["payload_hash"],
        payload=raw["payload"],
        created_at=raw["created_at"],
        status=raw["status"],
    )


def parse_wal_text(text: str) -> list[WalEntry]:
    """Parse and validate WAL JSONL text."""

    if text and not text.endswith("\n"):
        raise WalError("WAL_INCOMPLETE", "line:last", "INCOMPLETE_FINAL_LINE")
    entries: list[WalEntry] = []
    pending_seen: set[str] = set()
    ack_seen: set[str] = set()
    expected_sequence = 1

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            raise WalError("WAL_INVALID_JSON", f"line:{line_number}", "JSON_INVALID") from None
        entry = validate_entry_shape(raw)
        if entry.sequence != expected_sequence:
            if entry.sequence < expected_sequence:
                raise WalError("WAL_SEQUENCE_INVALID", "sequence", "SEQUENCE_DUPLICATE_OR_REVERSED")
            raise WalError("WAL_SEQUENCE_INVALID", "sequence", "SEQUENCE_MISSING")
        expected_sequence += 1

        if entry.status == PENDING:
            if entry.operation_id in pending_seen:
                raise WalError("WAL_OPERATION_DUPLICATE", "operation_id", "DUPLICATE_PENDING")
            if entry.operation_id in ack_seen:
                raise WalError("WAL_OPERATION_DUPLICATE", "operation_id", "PENDING_AFTER_ACK")
            pending_seen.add(entry.operation_id)
        else:
            if entry.operation_id not in pending_seen:
                raise WalError("WAL_ACK_INVALID", "operation_id", "ACK_WITHOUT_PENDING")
            if entry.operation_id in ack_seen:
                raise WalError("WAL_ACK_INVALID", "operation_id", "DUPLICATE_ACK")
            ack_seen.add(entry.operation_id)
        entries.append(entry)
    return entries


class AppendOnlyWal:
    """Append-only JSONL WAL with injectable clock, UUID and fsync hooks."""

    def __init__(
        self,
        path: Path,
        run_id: str,
        *,
        clock: Callable[[], str] = default_clock,
        uuid_factory: Callable[[], str] | None = None,
        fsync_func: Callable[[int], None] = os.fsync,
        opener: Callable[..., TextIO] = open,
    ) -> None:
        self.path = path
        self.run_id = run_id
        self.clock = clock
        self.uuid_factory = uuid_factory or (lambda: str(uuid.uuid4()))
        self.fsync_func = fsync_func
        self.opener = opener

    def read_entries(self) -> list[WalEntry]:
        """Read and validate existing WAL entries."""

        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            raise WalError("WAL_READ_FAILED", "wal", "READ_FAILED") from None
        return parse_wal_text(text)

    def next_sequence(self) -> int:
        """Return the next sequence after validating existing entries."""

        return len(self.read_entries()) + 1

    def append_pending(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        operation_id: str | None = None,
    ) -> WalEntry:
        """Append a pending operation entry after redaction checks."""

        validate_payload_redacted(payload)
        existing = self.read_entries()
        op_id = operation_id or self.uuid_factory()
        if any(entry.operation_id == op_id and entry.status == PENDING for entry in existing):
            raise WalError("WAL_OPERATION_DUPLICATE", "operation_id", "DUPLICATE_PENDING")
        entry = WalEntry(
            schema_version=SCHEMA_VERSION,
            sequence=len(existing) + 1,
            operation_id=op_id,
            run_id=self.run_id,
            operation=operation,
            payload_hash=payload_hash(payload),
            payload=payload,
            created_at=self.clock(),
            status=PENDING,
        )
        self._append_entry(entry)
        return entry

    def append_ack(self, operation_id: str, payload: dict[str, Any] | None = None) -> WalEntry:
        """Append an acknowledgement entry for a pending operation."""

        ack_payload = payload or {"acknowledged_operation_id": operation_id}
        validate_payload_redacted(ack_payload)
        existing = self.read_entries()
        if not any(
            entry.operation_id == operation_id and entry.status == PENDING for entry in existing
        ):
            raise WalError("WAL_ACK_INVALID", "operation_id", "ACK_WITHOUT_PENDING")
        if any(
            entry.operation_id == operation_id and entry.status == ACKNOWLEDGED
            for entry in existing
        ):
            raise WalError("WAL_ACK_INVALID", "operation_id", "DUPLICATE_ACK")
        pending = next(
            entry
            for entry in existing
            if entry.operation_id == operation_id and entry.status == PENDING
        )
        entry = WalEntry(
            schema_version=SCHEMA_VERSION,
            sequence=len(existing) + 1,
            operation_id=operation_id,
            run_id=self.run_id,
            operation=f"{pending.operation}.ack",
            payload_hash=payload_hash(ack_payload),
            payload=ack_payload,
            created_at=self.clock(),
            status=ACKNOWLEDGED,
        )
        self._append_entry(entry)
        return entry

    def replay_plan(self) -> list[WalEntry]:
        """Return pending entries that are not acknowledged, without external mutation."""

        entries = self.read_entries()
        acknowledged = {entry.operation_id for entry in entries if entry.status == ACKNOWLEDGED}
        return [
            entry
            for entry in entries
            if entry.status == PENDING and entry.operation_id not in acknowledged
        ]

    def _append_entry(self, entry: WalEntry) -> None:
        """Append an entry and fsync before returning."""

        line = canonical_json(entry.to_dict()) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
            with self.opener(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                self.fsync_func(handle.fileno())
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError:
            raise WalError("WAL_WRITE_FAILED", "wal", "WRITE_FAILED") from None

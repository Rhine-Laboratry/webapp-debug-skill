"""Cooperative single-writer lock stored through the Sheets backend abstraction."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from webapp_debug_skill.errors import (
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_EXTERNAL_FAILURE,
    EXIT_LOCK_CONFLICT,
)
from webapp_debug_skill.sheets_client import (
    ClearMetadata,
    SetMetadata,
    SheetsBackend,
    SheetsBackendError,
)

LOCK_FIELDS = (
    "writer_lock_owner",
    "writer_lock_run_id",
    "writer_lock_acquired_at",
    "writer_lock_expires_at",
    "writer_lock_commit_sha",
)
LOCK_OWNER = "writer_lock_owner"
LOCK_RUN_ID = "writer_lock_run_id"
LOCK_ACQUIRED_AT = "writer_lock_acquired_at"
LOCK_EXPIRES_AT = "writer_lock_expires_at"
LOCK_COMMIT_SHA = "writer_lock_commit_sha"


class SheetsLockError(RuntimeError):
    """Safe lock error that never includes the owner token."""

    def __init__(
        self,
        code: str,
        path: str = "lock",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_LOCK_CONFLICT,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.path = path
        self.reason = reason
        self.exit_code = exit_code


@dataclass(frozen=True)
class LockLease:
    """A successful lock lease. The full owner token is intentionally local-only."""

    owner_token: str
    run_id: str
    acquired_at: datetime
    expires_at: datetime
    commit_sha: str

    @property
    def owner_fingerprint(self) -> str:
        """Short non-secret owner token fingerprint for diagnostics."""

        return owner_fingerprint(self.owner_token)


def owner_fingerprint(owner_token: str) -> str:
    """Return a short non-secret fingerprint for an owner token."""

    return hashlib.sha256(owner_token.encode("utf-8")).hexdigest()[:12]


def default_token_factory() -> str:
    """Return a cryptographically random owner token."""

    return secrets.token_urlsafe(32)


def default_now() -> datetime:
    """Return current UTC time."""

    return datetime.now(UTC)


def require_aware_utc(value: datetime, path: str) -> datetime:
    """Require timezone-aware datetime and normalize to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise SheetsLockError(
            "SHEETS_LOCK_CORRUPT",
            path,
            "NAIVE_DATETIME",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return value.astimezone(UTC)


def format_rfc3339(value: datetime) -> str:
    """Format an aware datetime as RFC3339 UTC."""

    aware = require_aware_utc(value, "datetime")
    return aware.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_rfc3339(value: str, path: str) -> datetime:
    """Parse RFC3339 UTC timestamp."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SheetsLockError("SHEETS_LOCK_CORRUPT", path, "TIMESTAMP_INVALID") from None
    return require_aware_utc(parsed, path)


def lock_values_empty(values: Mapping[str, str]) -> bool:
    """Return whether all lock fields are absent or empty."""

    return all(values.get(field, "") == "" for field in LOCK_FIELDS)


def read_complete_lock(values: Mapping[str, str]) -> dict[str, str] | None:
    """Return complete lock values, None if empty, or raise on partial state."""

    present = {field: values.get(field, "") for field in LOCK_FIELDS}
    non_empty = {field: value for field, value in present.items() if value != ""}
    if not non_empty:
        return None
    if set(non_empty) != set(LOCK_FIELDS):
        raise SheetsLockError("SHEETS_LOCK_CORRUPT", "metadata", "PARTIAL_LOCK")
    return non_empty


def backend_error_to_lock_error(error: SheetsBackendError) -> SheetsLockError:
    """Convert backend errors to safe lock errors."""

    if error.code == "SHEETS_LOCK_STORAGE_UNAVAILABLE":
        return SheetsLockError(
            "SHEETS_LOCK_STORAGE_UNAVAILABLE",
            error.path,
            error.reason,
            exit_code=EXIT_LOCK_CONFLICT,
        )
    if error.code == "SHEETS_BATCH_INVALID":
        return SheetsLockError(
            "SHEETS_BATCH_INVALID",
            error.path,
            error.reason,
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return SheetsLockError(
        "SHEETS_BACKEND_IO_FAILED",
        error.path,
        error.reason,
        exit_code=EXIT_EXTERNAL_FAILURE,
    )


class SheetsLockManager:
    """Cooperative lock manager using backend metadata storage.

    This is not a complete distributed lock or compare-and-swap primitive. It relies on a
    single-writer operating model and detects races through read-back after atomic batches.
    """

    def __init__(
        self,
        backend: SheetsBackend,
        *,
        now_factory: Callable[[], datetime] = default_now,
        token_factory: Callable[[], str] = default_token_factory,
    ) -> None:
        self.backend = backend
        self.now_factory = now_factory
        self.token_factory = token_factory

    def acquire(self, *, run_id: str, ttl: timedelta, commit_sha: str) -> LockLease:
        """Acquire a lock lease or raise a safe SheetsLockError."""

        if ttl <= timedelta(0):
            raise SheetsLockError(
                "SHEETS_LOCK_INVALID_TTL",
                "ttl",
                "MUST_BE_POSITIVE",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
        now = require_aware_utc(self.now_factory(), "now")
        current = self._read_lock_fields()
        lock = read_complete_lock(current)
        if lock is not None:
            acquired_at = parse_rfc3339(lock[LOCK_ACQUIRED_AT], LOCK_ACQUIRED_AT)
            expires_at = parse_rfc3339(lock[LOCK_EXPIRES_AT], LOCK_EXPIRES_AT)
            if expires_at < acquired_at:
                raise SheetsLockError(
                    "SHEETS_LOCK_CORRUPT", LOCK_EXPIRES_AT, "EXPIRES_BEFORE_ACQUIRED"
                )
            if now < expires_at:
                raise SheetsLockError("SHEETS_LOCK_HELD", "lock", "ACTIVE_LOCK")

        owner_token = self.token_factory()
        if not owner_token:
            raise SheetsLockError(
                "SHEETS_LOCK_CORRUPT",
                "owner_token",
                "EMPTY_OWNER_TOKEN",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
        acquired_at = now
        expires_at = now + ttl
        values = {
            LOCK_OWNER: owner_token,
            LOCK_RUN_ID: run_id,
            LOCK_ACQUIRED_AT: format_rfc3339(acquired_at),
            LOCK_EXPIRES_AT: format_rfc3339(expires_at),
            LOCK_COMMIT_SHA: commit_sha,
        }
        self._apply_lock_batch([SetMetadata.from_mapping(values)])
        read_back = self._read_lock_fields()
        if any(read_back.get(field) != value for field, value in values.items()):
            raise SheetsLockError("SHEETS_LOCK_RACE", "lock", "READ_BACK_MISMATCH")
        return LockLease(
            owner_token=owner_token,
            run_id=run_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
            commit_sha=commit_sha,
        )

    def release(self, lease: LockLease) -> None:
        """Release a lock only when owner token and run id match."""

        current = self._read_lock_fields()
        lock = read_complete_lock(current)
        if lock is None:
            raise SheetsLockError("SHEETS_LOCK_LOST", "lock", "MISSING")
        if lock[LOCK_OWNER] != lease.owner_token or lock[LOCK_RUN_ID] != lease.run_id:
            raise SheetsLockError("SHEETS_LOCK_OWNER_MISMATCH", "lock", "OWNER_MISMATCH")
        self._apply_lock_batch([ClearMetadata(keys=LOCK_FIELDS)])
        read_back = self._read_lock_fields()
        if not lock_values_empty(read_back):
            raise SheetsLockError("SHEETS_LOCK_RACE", "lock", "CLEAR_READ_BACK_MISMATCH")

    def _read_lock_fields(self) -> dict[str, str]:
        try:
            return self.backend.read_metadata(LOCK_FIELDS)
        except SheetsBackendError as exc:
            raise backend_error_to_lock_error(exc) from None

    def _apply_lock_batch(self, mutations: list[SetMetadata | ClearMetadata]) -> None:
        try:
            self.backend.apply_batch(mutations)
        except SheetsBackendError as exc:
            raise backend_error_to_lock_error(exc) from None

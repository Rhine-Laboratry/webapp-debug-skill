"""Metadata storage bootstrap with WAL and read-back checks."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from webapp_debug_skill.errors import (
    EXIT_EXTERNAL_FAILURE,
    EXIT_LOCK_CONFLICT,
    EXIT_POLICY_BLOCKED,
)
from webapp_debug_skill.google_sheets_backend import (
    METADATA_HEADERS,
    METADATA_TAB,
    MetadataStorageInspection,
)
from webapp_debug_skill.sheets_client import BatchResult, SheetsBackendError
from webapp_debug_skill.wal import AppendOnlyWal, WalError, canonical_json

BOOTSTRAP_OPERATION = "sheets.bootstrap_metadata"
CREATE_OPERATION = "sheets.create"


class BootstrapOutcome(str, Enum):
    """Bootstrap outcome."""

    NOOP = "NOOP"
    BOOTSTRAPPED = "BOOTSTRAPPED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    RETRY_REQUIRED = "RETRY_REQUIRED"
    CONFLICT = "CONFLICT"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    WAL_ACK_FAILED = "WAL_ACK_FAILED"


class MetadataBootstrapBackend(Protocol):
    """Backend capability needed for Metadata storage bootstrap."""

    spreadsheet_id: str

    def inspect_metadata_storage(self) -> MetadataStorageInspection:
        """Inspect Metadata tab state."""

    def bootstrap_metadata_storage(self) -> BatchResult:
        """Create Metadata tab/header and read back."""


class SheetsBootstrapError(RuntimeError):
    """Safe bootstrap error."""

    def __init__(
        self,
        code: str,
        path: str = "bootstrap",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_POLICY_BLOCKED,
        result: "BootstrapResult | None" = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.path = path
        self.reason = reason
        self.exit_code = exit_code
        self.result = result


@dataclass(frozen=True)
class BootstrapResult:
    """Safe bootstrap result."""

    outcome: BootstrapOutcome
    plan_fingerprint: str
    bootstrap_required: bool
    bootstrapped: bool = False
    planned_mutation_count: int = 0
    applied_mutation_count: int = 0
    read_back_verified: bool = False
    wal_pending_written: bool = False
    wal_acknowledged: bool = False
    reason_code: str | None = None


def bootstrap_payload(spreadsheet_id: str) -> dict[str, Any]:
    """Return safe canonical bootstrap payload."""

    payload = {
        "spreadsheet_id": spreadsheet_id,
        "target_tab": METADATA_TAB,
        "canonical_header": list(METADATA_HEADERS),
    }
    payload["plan_fingerprint"] = bootstrap_fingerprint(payload)
    return payload


def bootstrap_fingerprint(payload: dict[str, Any]) -> str:
    """Return bootstrap plan fingerprint without self-reference."""

    material = {key: value for key, value in payload.items() if key != "plan_fingerprint"}
    return "sha256:" + hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def metadata_ready(inspection: MetadataStorageInspection) -> bool:
    """Return whether Metadata storage is ready."""

    return inspection.status == "READY"


class SheetsBootstrapper:
    """Coordinate Metadata bootstrap through WAL, atomic batch and read-back."""

    def __init__(
        self,
        *,
        backend: MetadataBootstrapBackend,
        wal: AppendOnlyWal,
        operation_id_factory: Callable[[], str],
    ) -> None:
        self.backend = backend
        self.wal = wal
        self.operation_id_factory = operation_id_factory

    def plan(self) -> BootstrapResult:
        """Return read-only bootstrap plan."""

        inspection = self.backend.inspect_metadata_storage()
        payload = bootstrap_payload(self.backend.spreadsheet_id)
        if inspection.status == "READY":
            return BootstrapResult(
                outcome=BootstrapOutcome.NOOP,
                plan_fingerprint=payload["plan_fingerprint"],
                bootstrap_required=False,
            )
        if inspection.status == "MISSING":
            return BootstrapResult(
                outcome=BootstrapOutcome.RETRY_REQUIRED,
                plan_fingerprint=payload["plan_fingerprint"],
                bootstrap_required=True,
                planned_mutation_count=2,
                reason_code="SHEETS_INIT_BOOTSTRAP_REQUIRED",
            )
        raise SheetsBootstrapError(
            inspection.reason_code or "SHEETS_BOOTSTRAP_CONFLICT",
            "Metadata",
            "BOOTSTRAP_BLOCKED",
            exit_code=EXIT_POLICY_BLOCKED,
        )

    def bootstrap(self) -> BootstrapResult:
        """Bootstrap Metadata storage if absent."""

        inspection = self.backend.inspect_metadata_storage()
        payload = bootstrap_payload(self.backend.spreadsheet_id)
        if inspection.status == "READY":
            return BootstrapResult(
                outcome=BootstrapOutcome.NOOP,
                plan_fingerprint=payload["plan_fingerprint"],
                bootstrap_required=False,
            )
        if inspection.status != "MISSING":
            raise SheetsBootstrapError(
                inspection.reason_code or "SHEETS_BOOTSTRAP_CONFLICT",
                "Metadata",
                "BOOTSTRAP_BLOCKED",
                exit_code=EXIT_POLICY_BLOCKED,
            )

        operation_id = self.operation_id_factory()
        try:
            self.wal.append_pending(BOOTSTRAP_OPERATION, payload, operation_id=operation_id)
        except WalError as exc:
            raise SheetsBootstrapError(
                "SHEETS_INIT_WAL_FAILED",
                exc.path,
                exc.reason,
                exit_code=EXIT_EXTERNAL_FAILURE,
            ) from None

        pending_result = BootstrapResult(
            outcome=BootstrapOutcome.RETRY_REQUIRED,
            plan_fingerprint=payload["plan_fingerprint"],
            bootstrap_required=True,
            planned_mutation_count=2,
            wal_pending_written=True,
        )
        try:
            result = self.backend.bootstrap_metadata_storage()
        except SheetsBackendError as exc:
            if exc.may_have_applied or exc.reason == "UNKNOWN_WRITE_RESULT":
                return self._handle_ambiguous(operation_id, pending_result)
            raise SheetsBootstrapError(
                exc.code,
                exc.path,
                exc.reason,
                exit_code=exc.exit_code,
                result=pending_result,
            ) from None

        inspection = self.backend.inspect_metadata_storage()
        if not metadata_ready(inspection):
            raise SheetsBootstrapError(
                "SHEETS_BOOTSTRAP_CONFLICT",
                "Metadata",
                "READ_BACK_MISMATCH",
                exit_code=EXIT_LOCK_CONFLICT,
                result=pending_result,
            )
        return self._ack_result(
            operation_id, payload, recovered=False, applied=result.applied_mutations
        )

    def reconcile(self) -> BootstrapResult:
        """Reconcile pending bootstrap operations without resending."""

        pending = [
            entry for entry in self.wal.replay_plan() if entry.operation == BOOTSTRAP_OPERATION
        ]
        if not pending:
            payload = bootstrap_payload(self.backend.spreadsheet_id)
            return BootstrapResult(
                outcome=BootstrapOutcome.NOOP,
                plan_fingerprint=payload["plan_fingerprint"],
                bootstrap_required=False,
            )
        entry = pending[0]
        inspection = self.backend.inspect_metadata_storage()
        fingerprint = bootstrap_fingerprint(dict(entry.payload))
        if inspection.status == "READY":
            try:
                self.wal.append_ack(entry.operation_id)
            except WalError as exc:
                raise SheetsBootstrapError(
                    "SHEETS_INIT_WAL_FAILED",
                    exc.path,
                    exc.reason,
                    exit_code=EXIT_EXTERNAL_FAILURE,
                ) from None
            return BootstrapResult(
                outcome=BootstrapOutcome.ALREADY_APPLIED,
                plan_fingerprint=fingerprint,
                bootstrap_required=True,
                bootstrapped=True,
                read_back_verified=True,
                wal_pending_written=True,
                wal_acknowledged=True,
            )
        if inspection.status == "MISSING":
            return BootstrapResult(
                outcome=BootstrapOutcome.RETRY_REQUIRED,
                plan_fingerprint=fingerprint,
                bootstrap_required=True,
                wal_pending_written=True,
                reason_code="SHEETS_BOOTSTRAP_OUTCOME_UNKNOWN",
            )
        raise SheetsBootstrapError(
            "SHEETS_BOOTSTRAP_CONFLICT",
            "Metadata",
            inspection.reason_code or "PARTIAL_OR_CONFLICT",
            exit_code=EXIT_LOCK_CONFLICT,
        )

    def _handle_ambiguous(
        self,
        operation_id: str,
        pending_result: BootstrapResult,
    ) -> BootstrapResult:
        inspection = self.backend.inspect_metadata_storage()
        if inspection.status == "READY":
            return self._ack_result(
                operation_id,
                bootstrap_payload(self.backend.spreadsheet_id),
                recovered=True,
                applied=2,
            )
        if inspection.status == "MISSING":
            return BootstrapResult(
                outcome=BootstrapOutcome.OUTCOME_UNKNOWN,
                plan_fingerprint=pending_result.plan_fingerprint,
                bootstrap_required=True,
                planned_mutation_count=2,
                wal_pending_written=True,
                reason_code="SHEETS_BOOTSTRAP_OUTCOME_UNKNOWN",
            )
        raise SheetsBootstrapError(
            "SHEETS_BOOTSTRAP_CONFLICT",
            "Metadata",
            inspection.reason_code or "PARTIAL_OR_CONFLICT",
            exit_code=EXIT_LOCK_CONFLICT,
            result=pending_result,
        )

    def _ack_result(
        self,
        operation_id: str,
        payload: dict[str, Any],
        *,
        recovered: bool,
        applied: int,
    ) -> BootstrapResult:
        try:
            self.wal.append_ack(operation_id)
        except WalError:
            return BootstrapResult(
                outcome=BootstrapOutcome.WAL_ACK_FAILED,
                plan_fingerprint=payload["plan_fingerprint"],
                bootstrap_required=True,
                bootstrapped=True,
                planned_mutation_count=2,
                applied_mutation_count=applied,
                read_back_verified=True,
                wal_pending_written=True,
                wal_acknowledged=False,
                reason_code="SHEETS_INIT_WAL_FAILED",
            )
        return BootstrapResult(
            outcome=BootstrapOutcome.BOOTSTRAPPED
            if not recovered
            else BootstrapOutcome.ALREADY_APPLIED,
            plan_fingerprint=payload["plan_fingerprint"],
            bootstrap_required=True,
            bootstrapped=True,
            planned_mutation_count=2,
            applied_mutation_count=applied,
            read_back_verified=True,
            wal_pending_written=True,
            wal_acknowledged=True,
        )

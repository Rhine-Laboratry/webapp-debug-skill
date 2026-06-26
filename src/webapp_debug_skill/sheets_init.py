"""Google-SDK-independent Sheets initialization planning and execution."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from webapp_debug_skill.config import load_json_file
from webapp_debug_skill.errors import (
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_EXTERNAL_FAILURE,
    EXIT_LOCK_CONFLICT,
    EXIT_POLICY_BLOCKED,
)
from webapp_debug_skill.redaction import secret_findings
from webapp_debug_skill.sheets_client import (
    AddHeaders,
    CreateTab,
    Mutation,
    SetMetadata,
    SheetsBackend,
    SheetsBackendError,
    SpreadsheetState,
)
from webapp_debug_skill.sheets_lock import (
    LOCK_OWNER,
    LOCK_RUN_ID,
    SheetsLockError,
    SheetsLockManager,
)
from webapp_debug_skill.sheets_schema import validate_sheets_schema
from webapp_debug_skill.wal import AppendOnlyWal, WalError, canonical_json

SCHEMA_VERSION_METADATA_KEY = "webapp_debug_schema_version"
INIT_OPERATION = "sheets.init.batch_update"


def safe_diagnostic_text(value: str, fallback: str) -> str:
    """Return diagnostic text only when it does not look secret-bearing."""

    text = str(value)
    return fallback if secret_findings(text) else text


class InitOutcome(str, Enum):
    """Initializer outcome values."""

    READY = "READY"
    NOOP = "NOOP"
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    RETRY_REQUIRED = "RETRY_REQUIRED"
    WAL_ACK_FAILED = "WAL_ACK_FAILED"
    LOCK_RELEASE_FAILED = "LOCK_RELEASE_FAILED"
    BLOCKED_SCHEMA_CONFLICT = "BLOCKED_SCHEMA_CONFLICT"
    BLOCKED_DOWNGRADE = "BLOCKED_DOWNGRADE"
    BLOCKED_BOOTSTRAP_REQUIRED = "BLOCKED_BOOTSTRAP_REQUIRED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    RECONCILIATION_CONFLICT = "RECONCILIATION_CONFLICT"


class InitPlanningError(RuntimeError):
    """Safe planning failure."""

    def __init__(
        self,
        code: str,
        path: str = "sheets_init",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_POLICY_BLOCKED,
    ) -> None:
        safe_code = safe_diagnostic_text(code, "SHEETS_INIT_FAILED")
        super().__init__(safe_code)
        self.code = safe_code
        self.path = safe_diagnostic_text(path, "sheets_init")
        self.reason = safe_diagnostic_text(reason, "FAILED")
        self.exit_code = exit_code


class InitExecutionError(RuntimeError):
    """Safe execution failure."""

    def __init__(
        self,
        code: str,
        path: str = "sheets_init",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_EXTERNAL_FAILURE,
        result: "InitResult | None" = None,
    ) -> None:
        safe_code = safe_diagnostic_text(code, "SHEETS_INIT_FAILED")
        super().__init__(safe_code)
        self.code = safe_code
        self.path = safe_diagnostic_text(path, "sheets_init")
        self.reason = safe_diagnostic_text(reason, "FAILED")
        self.exit_code = exit_code
        self.result = result


@dataclass(frozen=True)
class CanonicalTab:
    """Canonical tab with ordered headers."""

    name: str
    headers: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalSheetsSchema:
    """Validated canonical Sheets schema subset used by initialization."""

    schema_version: int
    tabs: tuple[CanonicalTab, ...]


@dataclass(frozen=True)
class InitPolicy:
    """Execution policy injected into the initializer."""

    run_id: str
    ttl_seconds: int
    commit_sha: str


@dataclass(frozen=True)
class InitPlan:
    """Deterministic initialization plan."""

    outcome: InitOutcome
    mutations: tuple[Mutation, ...]
    target_schema_version: int
    canonical_tabs: tuple[CanonicalTab, ...]
    reason_code: str | None = None
    safe_details: tuple[tuple[str, str], ...] = ()
    advisory: bool = False

    @property
    def noop(self) -> bool:
        """Return whether this plan has no initializer mutations."""

        return len(self.mutations) == 0 and self.outcome == InitOutcome.NOOP

    @property
    def fingerprint(self) -> str:
        """Return sha256 fingerprint of canonical plan JSON."""

        return (
            "sha256:"
            + hashlib.sha256(canonical_json(self.to_payload()).encode("utf-8")).hexdigest()
        )

    def to_payload(self, spreadsheet_id: str | None = None) -> dict[str, Any]:
        """Return redaction-safe canonical payload for WAL/fingerprinting."""

        payload: dict[str, Any] = {
            "schema_version": self.target_schema_version,
            "mutation_count": len(self.mutations),
            "mutations": [mutation_to_payload(mutation) for mutation in self.mutations],
            "postconditions": postconditions_payload(
                self.target_schema_version, self.canonical_tabs
            ),
        }
        if spreadsheet_id is not None:
            payload["spreadsheet_id"] = spreadsheet_id
        findings = secret_findings(payload)
        if findings:
            raise InitPlanningError("SHEETS_SCHEMA_CONFLICT", findings[0][0], findings[0][1])
        return payload


@dataclass(frozen=True)
class InitResult:
    """Safe initializer result."""

    outcome: InitOutcome
    plan_fingerprint: str
    planned_mutation_count: int
    applied_mutation_count: int = 0
    noop: bool = False
    applied: bool = False
    read_back_verified: bool = False
    wal_pending_written: bool = False
    wal_acknowledged: bool = False
    recovered_after_ambiguous_write: bool = False
    lock_released: bool = False
    reason_code: str | None = None
    safe_details: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def with_release_failure(self, code: str, reason: str) -> "InitResult":
        """Return a copy that reports lock release failure."""

        return InitResult(
            outcome=InitOutcome.LOCK_RELEASE_FAILED,
            plan_fingerprint=self.plan_fingerprint,
            planned_mutation_count=self.planned_mutation_count,
            applied_mutation_count=self.applied_mutation_count,
            noop=self.noop,
            applied=self.applied,
            read_back_verified=self.read_back_verified,
            wal_pending_written=self.wal_pending_written,
            wal_acknowledged=self.wal_acknowledged,
            recovered_after_ambiguous_write=self.recovered_after_ambiguous_write,
            lock_released=False,
            reason_code=code,
            safe_details=self.safe_details + (("release", reason),),
        )


def mutation_to_payload(mutation: Mutation) -> dict[str, Any]:
    """Serialize a typed mutation to canonical JSON payload."""

    if isinstance(mutation, CreateTab):
        return {"type": "create_tab", "name": mutation.name}
    if isinstance(mutation, AddHeaders):
        return {
            "type": "add_headers",
            "tab_name": mutation.tab_name,
            "headers": list(mutation.headers),
        }
    if isinstance(mutation, SetMetadata):
        return {"type": "set_metadata", "values": dict(mutation.values)}
    raise InitPlanningError(
        "SHEETS_SCHEMA_CONFLICT",
        "mutations",
        "UNKNOWN_MUTATION",
        exit_code=EXIT_ARGUMENT_OR_SCHEMA,
    )


def mutation_from_payload(payload: Mapping[str, Any]) -> Mutation:
    """Deserialize WAL payload mutation."""

    mutation_type = payload.get("type")
    if mutation_type == "create_tab":
        return CreateTab(str(payload["name"]))
    if mutation_type == "add_headers":
        return AddHeaders(
            str(payload["tab_name"]), tuple(str(header) for header in payload["headers"])
        )
    if mutation_type == "set_metadata":
        values = payload.get("values")
        if not isinstance(values, Mapping):
            raise InitPlanningError("SHEETS_SCHEMA_CONFLICT", "wal.values", "INVALID")
        return SetMetadata.from_mapping({str(key): str(value) for key, value in values.items()})
    raise InitPlanningError("SHEETS_SCHEMA_CONFLICT", "wal.mutations", "UNKNOWN_MUTATION")


def postconditions_payload(schema_version: int, tabs: Sequence[CanonicalTab]) -> dict[str, Any]:
    """Build intended postcondition payload."""

    return {
        "schema_version": str(schema_version),
        "tabs": [{"name": tab.name, "headers": list(tab.headers)} for tab in tabs],
    }


def parse_postconditions(payload: Mapping[str, Any]) -> tuple[int, tuple[CanonicalTab, ...]]:
    """Parse intended postconditions from WAL payload."""

    postconditions = payload.get("postconditions")
    if not isinstance(postconditions, Mapping):
        raise InitPlanningError("SHEETS_SCHEMA_CONFLICT", "wal.postconditions", "INVALID")
    version = parse_schema_version_value(postconditions.get("schema_version"), "wal.schema_version")
    raw_tabs = postconditions.get("tabs")
    if not isinstance(raw_tabs, list):
        raise InitPlanningError("SHEETS_SCHEMA_CONFLICT", "wal.tabs", "INVALID")
    tabs: list[CanonicalTab] = []
    for index, raw_tab in enumerate(raw_tabs):
        if not isinstance(raw_tab, Mapping):
            raise InitPlanningError("SHEETS_SCHEMA_CONFLICT", f"wal.tabs.[{index}]", "INVALID")
        raw_headers = raw_tab.get("headers")
        if not isinstance(raw_headers, list):
            raise InitPlanningError(
                "SHEETS_SCHEMA_CONFLICT", f"wal.tabs.[{index}].headers", "INVALID"
            )
        tabs.append(
            CanonicalTab(str(raw_tab.get("name")), tuple(str(header) for header in raw_headers))
        )
    return version, tuple(tabs)


def is_formula_like(value: str) -> bool:
    """Return whether a header could be interpreted as a formula."""

    stripped = value.lstrip(" \t\r\n\v\f\u0000")
    return stripped == "" or stripped[0] in {"=", "+", "-", "@"}


def parse_schema_version_value(value: Any, path: str) -> int:
    """Parse strict positive integer schema version."""

    if isinstance(value, bool):
        raise InitPlanningError("SHEETS_SCHEMA_VERSION_INVALID", path, "INVALID")
    if isinstance(value, int):
        if value < 1:
            raise InitPlanningError("SHEETS_SCHEMA_VERSION_INVALID", path, "INVALID")
        return value
    if isinstance(value, str):
        if not value.isdecimal() or value.startswith("0"):
            raise InitPlanningError("SHEETS_SCHEMA_VERSION_INVALID", path, "INVALID")
        parsed = int(value)
        if parsed < 1:
            raise InitPlanningError("SHEETS_SCHEMA_VERSION_INVALID", path, "INVALID")
        return parsed
    raise InitPlanningError("SHEETS_SCHEMA_VERSION_INVALID", path, "INVALID")


def canonical_schema_from_mapping(schema: Mapping[str, Any]) -> CanonicalSheetsSchema:
    """Build canonical initialization schema from a validated schema mapping."""

    schema_version = parse_schema_version_value(schema.get("schema_version"), "schema_version")
    raw_tabs = schema.get("tabs")
    if not isinstance(raw_tabs, list):
        raise InitPlanningError("SHEETS_SCHEMA_CONFLICT", "tabs", "INVALID")

    seen_tabs: set[str] = set()
    tabs: list[CanonicalTab] = []
    for tab_index, raw_tab in enumerate(raw_tabs):
        if not isinstance(raw_tab, Mapping):
            raise InitPlanningError("SHEETS_SCHEMA_CONFLICT", f"tabs.[{tab_index}]", "INVALID")
        tab_name = str(raw_tab.get("name"))
        if tab_name in seen_tabs:
            raise InitPlanningError("SHEETS_SCHEMA_CONFLICT", "tabs", "DUPLICATE_TAB")
        seen_tabs.add(tab_name)
        raw_columns = raw_tab.get("columns")
        if not isinstance(raw_columns, list):
            raise InitPlanningError(
                "SHEETS_SCHEMA_CONFLICT", f"tabs.[{tab_index}].columns", "INVALID"
            )
        headers: list[str] = []
        seen_headers: set[str] = set()
        for column_index, raw_column in enumerate(raw_columns):
            if not isinstance(raw_column, list) or len(raw_column) < 1:
                raise InitPlanningError(
                    "SHEETS_SCHEMA_CONFLICT",
                    f"tabs.[{tab_index}].columns.[{column_index}]",
                    "INVALID",
                )
            header = str(raw_column[0])
            if header == "" or is_formula_like(header):
                raise InitPlanningError(
                    "SHEETS_INIT_UNSAFE_HEADER",
                    f"tabs.[{tab_index}].columns.[{column_index}]",
                    "UNSAFE_HEADER",
                )
            if header in seen_headers:
                raise InitPlanningError(
                    "SHEETS_SCHEMA_CONFLICT",
                    f"tabs.[{tab_index}].columns",
                    "DUPLICATE_COLUMN",
                )
            seen_headers.add(header)
            headers.append(header)
        tabs.append(CanonicalTab(tab_name, tuple(headers)))
    return CanonicalSheetsSchema(schema_version=schema_version, tabs=tuple(tabs))


def load_canonical_schema(schema_path: Path) -> CanonicalSheetsSchema:
    """Load canonical schema through the existing sheets schema validator."""

    validation = validate_sheets_schema(schema_path)
    if not validation.ok:
        raise InitPlanningError("SHEETS_SCHEMA_CONFLICT", "schema", validation.code)
    schema, issues = load_json_file(schema_path)
    if issues:
        raise InitPlanningError("SHEETS_SCHEMA_CONFLICT", "schema", issues[0].reason)
    assert schema is not None
    return canonical_schema_from_mapping(schema)


def current_schema_version(state: SpreadsheetState) -> int | None:
    """Return current schema version from metadata, or None if unset."""

    metadata = state.metadata_dict()
    if SCHEMA_VERSION_METADATA_KEY not in metadata:
        return None
    return parse_schema_version_value(
        metadata[SCHEMA_VERSION_METADATA_KEY], SCHEMA_VERSION_METADATA_KEY
    )


def plan_tab_headers(tab: CanonicalTab, existing_headers: list[str] | None) -> tuple[Mutation, ...]:
    """Plan tab/header mutations for one canonical tab."""

    canonical = list(tab.headers)
    if existing_headers is None:
        return (CreateTab(tab.name), AddHeaders(tab.name, tuple(canonical)))
    if any(header == "" for header in existing_headers):
        raise InitPlanningError("SHEETS_SCHEMA_CONFLICT", tab.name, "EMPTY_EXISTING_HEADER")
    if len(existing_headers) == 0:
        return (AddHeaders(tab.name, tuple(canonical)),)
    if existing_headers == canonical[: len(existing_headers)] and len(existing_headers) < len(
        canonical
    ):
        return (AddHeaders(tab.name, tuple(canonical[len(existing_headers) :])),)
    if existing_headers[: len(canonical)] == canonical:
        trailing = existing_headers[len(canonical) :]
        if set(trailing) & set(canonical):
            raise InitPlanningError(
                "SHEETS_SCHEMA_CONFLICT", tab.name, "DUPLICATE_CANONICAL_TRAILING"
            )
        return ()
    raise InitPlanningError("SHEETS_SCHEMA_CONFLICT", tab.name, "HEADER_CONFLICT")


def generate_init_plan(
    schema: CanonicalSheetsSchema,
    state: SpreadsheetState,
    *,
    advisory: bool = False,
) -> InitPlan:
    """Generate a deterministic initialization plan from schema and state."""

    current_version = current_schema_version(state)
    if current_version is not None and current_version > schema.schema_version:
        raise InitPlanningError(
            "SHEETS_SCHEMA_DOWNGRADE_BLOCKED",
            SCHEMA_VERSION_METADATA_KEY,
            "DOWNGRADE",
        )

    existing_tabs = state.tabs_dict()
    mutations: list[Mutation] = []
    for tab in schema.tabs:
        mutations.extend(plan_tab_headers(tab, existing_tabs.get(tab.name)))

    if current_version != schema.schema_version:
        mutations.append(
            SetMetadata.from_mapping({SCHEMA_VERSION_METADATA_KEY: str(schema.schema_version)})
        )

    outcome = InitOutcome.NOOP if not mutations else InitOutcome.READY
    return InitPlan(
        outcome=outcome,
        mutations=tuple(mutations),
        target_schema_version=schema.schema_version,
        canonical_tabs=schema.tabs,
        advisory=advisory,
    )


class PostconditionStatus(str, Enum):
    """Postcondition verification state."""

    ALL = "ALL"
    NONE = "NONE"
    PARTIAL = "PARTIAL"


def evaluate_postconditions(
    state: SpreadsheetState,
    *,
    schema_version: int,
    tabs: Sequence[CanonicalTab],
) -> PostconditionStatus:
    """Evaluate intended postconditions without requiring full spreadsheet equality."""

    checks: list[bool] = []
    metadata = state.metadata_dict()
    checks.append(metadata.get(SCHEMA_VERSION_METADATA_KEY) == str(schema_version))
    existing_tabs = state.tabs_dict()
    for tab in tabs:
        headers = existing_tabs.get(tab.name)
        checks.append(headers is not None and headers[: len(tab.headers)] == list(tab.headers))
    if all(checks):
        return PostconditionStatus.ALL
    if not any(checks):
        return PostconditionStatus.NONE
    return PostconditionStatus.PARTIAL


def verify_postconditions(state: SpreadsheetState, plan: InitPlan) -> None:
    """Raise if read-back does not satisfy intended postconditions."""

    status = evaluate_postconditions(
        state,
        schema_version=plan.target_schema_version,
        tabs=plan.canonical_tabs,
    )
    if status != PostconditionStatus.ALL:
        raise InitExecutionError(
            "SHEETS_INIT_READBACK_MISMATCH",
            "read_back",
            "POSTCONDITION_MISMATCH",
            exit_code=EXIT_EXTERNAL_FAILURE,
        )


def init_error_from_backend(error: SheetsBackendError) -> InitExecutionError:
    """Convert backend errors to initializer errors."""

    if error.code == "SHEETS_LOCK_STORAGE_UNAVAILABLE":
        return InitExecutionError(
            "SHEETS_INIT_BOOTSTRAP_REQUIRED",
            error.path,
            error.reason,
            exit_code=EXIT_POLICY_BLOCKED,
        )
    if error.code == "SHEETS_BATCH_INVALID":
        return InitExecutionError(
            error.code, error.path, error.reason, exit_code=EXIT_ARGUMENT_OR_SCHEMA
        )
    return InitExecutionError(
        "SHEETS_BACKEND_IO_FAILED", error.path, error.reason, exit_code=EXIT_EXTERNAL_FAILURE
    )


def init_error_from_lock(error: SheetsLockError) -> InitExecutionError:
    """Convert lock errors to initializer errors."""

    if error.code == "SHEETS_LOCK_STORAGE_UNAVAILABLE":
        return InitExecutionError(
            "SHEETS_INIT_BOOTSTRAP_REQUIRED",
            error.path,
            error.reason,
            exit_code=EXIT_POLICY_BLOCKED,
        )
    return InitExecutionError(error.code, error.path, error.reason, exit_code=error.exit_code)


def init_error_from_wal(error: WalError) -> InitExecutionError:
    """Convert WAL errors to initializer errors."""

    return InitExecutionError(
        "SHEETS_INIT_WAL_FAILED", error.path, error.reason, exit_code=EXIT_EXTERNAL_FAILURE
    )


class SheetsInitializer:
    """Sheets initializer orchestrating lock, WAL, atomic batch and read-back."""

    def __init__(
        self,
        *,
        backend: SheetsBackend,
        lock_manager: SheetsLockManager,
        wal: AppendOnlyWal,
        operation_id_factory: Callable[[], str],
    ) -> None:
        self.backend = backend
        self.lock_manager = lock_manager
        self.wal = wal
        self.operation_id_factory = operation_id_factory

    def plan_read_only(self, schema: CanonicalSheetsSchema) -> InitPlan:
        """Read current state and generate an advisory plan without side effects."""

        state = self.backend.read_spreadsheet()
        return generate_init_plan(schema, state, advisory=True)

    def execute(self, schema: CanonicalSheetsSchema, policy: InitPolicy) -> InitResult:
        """Execute initialization against an existing spreadsheet."""

        lease = None
        result: InitResult | None = None
        primary_error: InitExecutionError | None = None
        try:
            self._check_lock_storage()
            lease = self.lock_manager.acquire(
                run_id=policy.run_id,
                ttl=timedelta(seconds=policy.ttl_seconds),
                commit_sha=policy.commit_sha,
            )
            state = self.backend.read_spreadsheet()
            plan = generate_init_plan(schema, state)
            base = InitResult(
                outcome=plan.outcome,
                plan_fingerprint=plan.fingerprint,
                planned_mutation_count=len(plan.mutations),
                noop=plan.noop,
            )
            if plan.noop:
                result = base
                return result

            self._confirm_lock_owner(lease.owner_token, lease.run_id)
            payload = plan.to_payload(state.spreadsheet_id)
            operation_id = self.operation_id_factory()
            try:
                self.wal.append_pending(INIT_OPERATION, payload, operation_id=operation_id)
            except WalError as exc:
                raise init_error_from_wal(exc) from None

            pending_result = InitResult(
                outcome=InitOutcome.READY,
                plan_fingerprint=plan.fingerprint,
                planned_mutation_count=len(plan.mutations),
                wal_pending_written=True,
            )
            try:
                self.backend.apply_batch(plan.mutations)
            except SheetsBackendError as exc:
                if exc.reason == "UNKNOWN_WRITE_RESULT":
                    result = self._handle_ambiguous_write(plan, operation_id, pending_result)
                    return result
                raise init_error_from_backend(exc) from None

            try:
                read_back = self.backend.read_spreadsheet()
            except SheetsBackendError as exc:
                raise init_error_from_backend(exc) from None
            verify_postconditions(read_back, plan)
            result = self._append_ack_result(plan, operation_id, recovered=False)
            return result
        except InitExecutionError as exc:
            primary_error = exc
            raise
        except SheetsLockError as exc:
            primary_error = init_error_from_lock(exc)
            raise primary_error from None
        except SheetsBackendError as exc:
            primary_error = init_error_from_backend(exc)
            raise primary_error from None
        finally:
            if lease is not None:
                try:
                    self.lock_manager.release(lease)
                    if result is not None:
                        object.__setattr__(result, "lock_released", True)
                except SheetsLockError as release_error:
                    if primary_error is not None:
                        pass
                    elif result is not None:
                        object.__setattr__(
                            result,
                            "outcome",
                            InitOutcome.LOCK_RELEASE_FAILED,
                        )
                        object.__setattr__(result, "lock_released", False)
                        object.__setattr__(result, "reason_code", release_error.code)

    def reconcile_pending(self, schema: CanonicalSheetsSchema, policy: InitPolicy) -> InitResult:
        """Reconcile pending WAL entries without resending them."""

        lease = None
        result: InitResult | None = None
        primary_error: InitExecutionError | None = None
        try:
            self._check_lock_storage()
            lease = self.lock_manager.acquire(
                run_id=policy.run_id,
                ttl=timedelta(seconds=policy.ttl_seconds),
                commit_sha=policy.commit_sha,
            )
            pending = [
                entry for entry in self.wal.replay_plan() if entry.operation == INIT_OPERATION
            ]
            if not pending:
                result = InitResult(
                    outcome=InitOutcome.NOOP,
                    plan_fingerprint="sha256:none",
                    planned_mutation_count=0,
                    noop=True,
                )
                return result
            entry = pending[0]
            version, tabs = parse_postconditions(entry.payload)
            try:
                state = self.backend.read_spreadsheet()
            except SheetsBackendError as exc:
                raise init_error_from_backend(exc) from None
            status = evaluate_postconditions(state, schema_version=version, tabs=tabs)
            fingerprint = (
                "sha256:"
                + hashlib.sha256(canonical_json(entry.payload).encode("utf-8")).hexdigest()
            )
            if status == PostconditionStatus.ALL:
                try:
                    self.wal.append_ack(entry.operation_id)
                except WalError as exc:
                    raise init_error_from_wal(exc) from None
                result = InitResult(
                    outcome=InitOutcome.ALREADY_APPLIED,
                    plan_fingerprint=fingerprint,
                    planned_mutation_count=int(entry.payload.get("mutation_count", 0)),
                    wal_pending_written=True,
                    wal_acknowledged=True,
                    read_back_verified=True,
                    recovered_after_ambiguous_write=True,
                )
                return result
            if status == PostconditionStatus.NONE:
                result = InitResult(
                    outcome=InitOutcome.RETRY_REQUIRED,
                    plan_fingerprint=fingerprint,
                    planned_mutation_count=int(entry.payload.get("mutation_count", 0)),
                    wal_pending_written=True,
                )
                return result
            raise InitExecutionError(
                "SHEETS_INIT_RECONCILIATION_CONFLICT",
                "reconciliation",
                "PARTIAL_POSTCONDITION",
                exit_code=EXIT_POLICY_BLOCKED,
            )
        except InitExecutionError as exc:
            primary_error = exc
            raise
        except SheetsLockError as exc:
            primary_error = init_error_from_lock(exc)
            raise primary_error from None
        except WalError as exc:
            primary_error = init_error_from_wal(exc)
            raise primary_error from None
        finally:
            if lease is not None:
                try:
                    self.lock_manager.release(lease)
                    if result is not None:
                        object.__setattr__(result, "lock_released", True)
                except SheetsLockError as release_error:
                    if primary_error is not None:
                        pass
                    elif result is not None:
                        object.__setattr__(
                            result,
                            "outcome",
                            InitOutcome.LOCK_RELEASE_FAILED,
                        )
                        object.__setattr__(result, "lock_released", False)
                        object.__setattr__(result, "reason_code", release_error.code)

    def _check_lock_storage(self) -> None:
        try:
            self.backend.read_metadata([SCHEMA_VERSION_METADATA_KEY])
        except SheetsBackendError as exc:
            raise init_error_from_backend(exc) from None

    def _confirm_lock_owner(self, owner_token: str, run_id: str) -> None:
        try:
            lock = self.backend.read_metadata([LOCK_OWNER, LOCK_RUN_ID])
        except SheetsBackendError as exc:
            raise init_error_from_backend(exc) from None
        if lock.get(LOCK_OWNER) != owner_token or lock.get(LOCK_RUN_ID) != run_id:
            raise InitExecutionError(
                "SHEETS_LOCK_OWNER_MISMATCH",
                "lock",
                "OWNER_MISMATCH",
                exit_code=EXIT_LOCK_CONFLICT,
            )

    def _append_ack_result(
        self, plan: InitPlan, operation_id: str, *, recovered: bool
    ) -> InitResult:
        try:
            self.wal.append_ack(operation_id)
        except WalError:
            return InitResult(
                outcome=InitOutcome.WAL_ACK_FAILED,
                plan_fingerprint=plan.fingerprint,
                planned_mutation_count=len(plan.mutations),
                applied_mutation_count=len(plan.mutations),
                applied=True,
                read_back_verified=True,
                wal_pending_written=True,
                wal_acknowledged=False,
                recovered_after_ambiguous_write=recovered,
                reason_code="SHEETS_INIT_WAL_FAILED",
            )
        return InitResult(
            outcome=InitOutcome.APPLIED,
            plan_fingerprint=plan.fingerprint,
            planned_mutation_count=len(plan.mutations),
            applied_mutation_count=len(plan.mutations),
            applied=True,
            read_back_verified=True,
            wal_pending_written=True,
            wal_acknowledged=True,
            recovered_after_ambiguous_write=recovered,
        )

    def _handle_ambiguous_write(
        self,
        plan: InitPlan,
        operation_id: str,
        pending_result: InitResult,
    ) -> InitResult:
        try:
            read_back = self.backend.read_spreadsheet()
        except SheetsBackendError as exc:
            raise init_error_from_backend(exc) from None
        status = evaluate_postconditions(
            read_back,
            schema_version=plan.target_schema_version,
            tabs=plan.canonical_tabs,
        )
        if status == PostconditionStatus.ALL:
            return self._append_ack_result(plan, operation_id, recovered=True)
        if status == PostconditionStatus.NONE:
            raise InitExecutionError(
                "SHEETS_INIT_OUTCOME_UNKNOWN",
                "backend",
                "UNKNOWN_WRITE_RESULT",
                exit_code=EXIT_EXTERNAL_FAILURE,
                result=pending_result,
            )
        raise InitExecutionError(
            "SHEETS_INIT_RECONCILIATION_CONFLICT",
            "backend",
            "PARTIAL_POSTCONDITION",
            exit_code=EXIT_POLICY_BLOCKED,
            result=pending_result,
        )

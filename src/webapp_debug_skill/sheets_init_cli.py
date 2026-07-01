"""CLI orchestration for safe Google Sheets initialization."""

from __future__ import annotations

import argparse
import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from webapp_debug_skill.cli import CliResult, Issue, emit_result
from webapp_debug_skill.config import DEFAULT_CONFIG_SCHEMA, load_yaml_file, validate_config
from webapp_debug_skill.config_writer import ConfigWriteError, ConfigWriter
from webapp_debug_skill.errors import (
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_EXTERNAL_FAILURE,
    EXIT_OK,
    EXIT_POLICY_BLOCKED,
    EXIT_UNEXPECTED,
)
from webapp_debug_skill.google_credentials import (
    GoogleCredentialError,
    build_sheets_service,
    load_service_account_credentials,
)
from webapp_debug_skill.google_sheets_backend import (
    METADATA_HEADERS,
    METADATA_TAB,
    GoogleSheetsBackend,
)
from webapp_debug_skill.redaction import secret_findings
from webapp_debug_skill.sheets_bootstrap import (
    BOOTSTRAP_OPERATION,
    CREATE_OPERATION,
    SheetsBootstrapError,
    SheetsBootstrapper,
)
from webapp_debug_skill.sheets_client import InitialSheetSpec, SheetsBackendError
from webapp_debug_skill.sheets_init import (
    INIT_OPERATION,
    InitExecutionError,
    InitOutcome,
    InitPlanningError,
    InitPolicy,
    SheetsInitializer,
    load_canonical_schema,
)
from webapp_debug_skill.sheets_lock import SheetsLockError, SheetsLockManager
from webapp_debug_skill.wal import (
    AppendOnlyWal,
    WalError,
    canonical_json,
    default_clock,
    default_wal_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = Path(".webapp-debug/config.yml")
DEFAULT_SCHEMA = REPO_ROOT / "skills/webapp-debug/assets/google-sheets-schema.json"
RUN_ID_PATTERN = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


class InitSheetsCliError(RuntimeError):
    """Safe CLI orchestration error."""

    def __init__(
        self,
        code: str,
        path: str = "init_sheets",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_ARGUMENT_OR_SCHEMA,
        data: dict[str, Any] | None = None,
    ) -> None:
        safe_code = "INIT_SHEETS_FAILED" if secret_findings(code) else code
        super().__init__(safe_code)
        self.code = safe_code
        self.path = "init_sheets" if secret_findings(path) else path
        self.reason = "FAILED" if secret_findings(reason) else reason
        self.exit_code = exit_code
        self.data = data or {}


@dataclass(frozen=True)
class InitSheetsDependencies:
    """Injectable dependencies for tests and production."""

    credential_loader: Callable[..., Any] = load_service_account_credentials
    service_builder: Callable[..., Any] = build_sheets_service
    backend_factory: Callable[..., GoogleSheetsBackend] = GoogleSheetsBackend
    wal_factory: Callable[..., AppendOnlyWal] = AppendOnlyWal
    config_writer_factory: Callable[..., ConfigWriter] = ConfigWriter
    run_id_factory: Callable[[], str] = lambda: f"run-{uuid.uuid4().hex}"
    operation_id_factory: Callable[[], str] = lambda: str(uuid.uuid4())
    clock: Callable[[], str] = default_clock


def build_parser() -> argparse.ArgumentParser:
    """Build init_sheets parser."""

    parser = argparse.ArgumentParser(description="Initialize webapp-debug Google Sheets state.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--title")
    parser.add_argument("--bootstrap-lock-storage", action="store_true")
    parser.add_argument("--confirm-spreadsheet-id")
    parser.add_argument("--write-config", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--wal", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None, deps: InitSheetsDependencies | None = None) -> int:
    """Run init_sheets CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    dependencies = deps or InitSheetsDependencies()
    try:
        result = run(args, dependencies)
    except (
        GoogleCredentialError,
        SheetsBackendError,
        SheetsBootstrapError,
        SheetsLockError,
        InitExecutionError,
        InitPlanningError,
        WalError,
        ConfigWriteError,
    ) as exc:
        result = CliResult(
            ok=False,
            code=getattr(exc, "code", "INIT_SHEETS_FAILED"),
            message="Sheets initialization failed.",
            details=[
                Issue(
                    getattr(exc, "path", "init_sheets"),
                    getattr(exc, "reason", "FAILED"),
                )
            ],
            data=getattr(exc, "data", {}),
        )
        emit_result(result, args.format)
        return getattr(exc, "exit_code", EXIT_EXTERNAL_FAILURE)
    except InitSheetsCliError as exc:
        result = CliResult(
            ok=False,
            code=exc.code,
            message="Sheets initialization failed.",
            details=[Issue(exc.path, exc.reason)],
            data=exc.data,
        )
        emit_result(result, args.format)
        return exc.exit_code
    except Exception:
        result = CliResult(
            ok=False,
            code="INIT_SHEETS_UNEXPECTED",
            message="Unexpected initialization failure.",
            details=[Issue("init_sheets", "UNEXPECTED")],
        )
        emit_result(result, args.format)
        return EXIT_UNEXPECTED
    emit_result(result, args.format)
    return EXIT_OK if result.ok else EXIT_POLICY_BLOCKED


def run(args: argparse.Namespace, deps: InitSheetsDependencies) -> CliResult:
    """Run orchestration and return a safe result."""

    validate_args(args)
    config_path = args.config.resolve()
    schema_path = args.schema.resolve()
    config, config_validation = load_and_validate_config(config_path)
    schema = load_canonical_schema(schema_path)
    repository_root = resolve_repository_root(config_path, config)
    sheets_config = config.get("sheets", {})
    if not isinstance(sheets_config, Mapping):
        raise InitSheetsCliError("CONFIG_VALIDATION_FAILED", "sheets", "INVALID")
    spreadsheet_id = str(sheets_config.get("spreadsheet_id", ""))
    if args.create and spreadsheet_id:
        raise InitSheetsCliError("ARGUMENT_INVALID", "create", "SPREADSHEET_ID_ALREADY_SET")
    if not args.create and not spreadsheet_id:
        raise InitSheetsCliError(
            "SHEETS_INIT_BOOTSTRAP_REQUIRED",
            "sheets.spreadsheet_id",
            "EMPTY",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    if spreadsheet_id:
        validate_spreadsheet_id(spreadsheet_id)

    env_name = str(sheets_config.get("service_account_credentials_env", ""))
    credential_result = deps.credential_loader(env_name=env_name, repository_root=repository_root)
    run_id = args.run_id or deps.run_id_factory()
    validate_run_id(run_id)
    base_data: dict[str, Any] = {
        "dry_run": args.dry_run,
        "run_id": run_id,
        "config_validation": config_validation.code,
    }

    if args.resume:
        return resume_existing(
            args, deps, schema, credential_result, spreadsheet_id, run_id, base_data
        )
    if args.create:
        return create_and_initialize(
            args,
            deps,
            schema,
            config,
            config_path,
            repository_root,
            credential_result,
            run_id,
            base_data,
        )
    return initialize_existing(
        args,
        deps,
        schema,
        config,
        credential_result,
        spreadsheet_id,
        run_id,
        base_data,
    )


def validate_args(args: argparse.Namespace) -> None:
    """Validate argument combinations before side effects."""

    if args.create and args.resume:
        raise InitSheetsCliError("ARGUMENT_INVALID", "create", "CREATE_WITH_RESUME")
    if args.create and args.bootstrap_lock_storage:
        raise InitSheetsCliError("ARGUMENT_INVALID", "bootstrap", "CREATE_WITH_BOOTSTRAP")
    if args.title and not args.create:
        raise InitSheetsCliError("ARGUMENT_INVALID", "title", "TITLE_WITHOUT_CREATE")
    if args.write_config and not args.create:
        raise InitSheetsCliError("ARGUMENT_INVALID", "write_config", "WRITE_CONFIG_WITHOUT_CREATE")
    if args.confirm_spreadsheet_id and not args.bootstrap_lock_storage:
        raise InitSheetsCliError(
            "ARGUMENT_INVALID", "confirm_spreadsheet_id", "CONFIRM_WITHOUT_BOOTSTRAP"
        )
    if args.resume and args.wal is None:
        raise InitSheetsCliError("INIT_RESUME_WAL_REQUIRED", "wal", "REQUIRED")
    if args.resume and args.dry_run:
        raise InitSheetsCliError("ARGUMENT_INVALID", "resume", "RESUME_WITH_DRY_RUN")
    if args.resume and args.write_config:
        raise InitSheetsCliError("ARGUMENT_INVALID", "resume", "RESUME_WITH_WRITE_CONFIG")
    if args.run_id:
        validate_run_id(args.run_id)


def load_and_validate_config(config_path: Path) -> tuple[dict[str, Any], CliResult]:
    """Load config and validate init mode."""

    validation = validate_config(config_path, "init", schema_path=DEFAULT_CONFIG_SCHEMA)
    if not validation.ok:
        raise InitSheetsCliError(
            validation.code,
            validation.details[0].path if validation.details else "config",
            validation.details[0].reason if validation.details else "INVALID",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    config, issues = load_yaml_file(config_path)
    if issues or config is None:
        issue = issues[0] if issues else Issue("config", "READ_FAILED")
        raise InitSheetsCliError("CONFIG_INVALID", issue.path, issue.reason)
    return config, validation


def resolve_repository_root(config_path: Path, config: Mapping[str, Any]) -> Path:
    """Resolve repository root from config, not only cwd."""

    project = config.get("project", {})
    root_value = project.get("repository_root", ".") if isinstance(project, Mapping) else "."
    root = Path(str(root_value))
    if not root.is_absolute():
        base = (
            config_path.parent.parent
            if config_path.parent.name == ".webapp-debug"
            else config_path.parent
        )
        root = (base / root).resolve()
    return root


def service_from_credentials(deps: InitSheetsDependencies, credentials: Any) -> Any:
    """Build injected Google Sheets service."""

    return deps.service_builder(credentials.credentials)


def make_backend(
    deps: InitSheetsDependencies, spreadsheet_id: str, service: Any
) -> GoogleSheetsBackend:
    """Build backend."""

    return deps.backend_factory(spreadsheet_id=spreadsheet_id, service=service)


def make_wal(
    args: argparse.Namespace,
    deps: InitSheetsDependencies,
    repository_root: Path,
    run_id: str,
) -> AppendOnlyWal:
    """Create WAL unless dry-run path avoids it."""

    wal_path = args.wal.resolve() if args.wal else default_wal_path(repository_root, run_id)
    return deps.wal_factory(wal_path, run_id, clock=deps.clock)


def initialize_existing(
    args: argparse.Namespace,
    deps: InitSheetsDependencies,
    schema: Any,
    config: Mapping[str, Any],
    credential_result: Any,
    spreadsheet_id: str,
    run_id: str,
    base_data: dict[str, Any],
) -> CliResult:
    """Initialize an existing Spreadsheet."""

    service = service_from_credentials(deps, credential_result)
    backend = make_backend(deps, spreadsheet_id, service)
    inspection = backend.inspect_metadata_storage()
    if args.dry_run:
        data = {
            **base_data,
            "action": "plan_existing",
            "spreadsheet_id": spreadsheet_id,
            "bootstrap_required": inspection.status == "MISSING",
            "bootstrapped": False,
            "config_write_requested": False,
            "config_written": False,
            "advisory": True,
        }
        if inspection.status == "READY":
            initializer = build_initializer(args, deps, config, backend, None, run_id)
            plan = initializer.plan_read_only(schema)
            data.update(
                {
                    "outcome": plan.outcome.value,
                    "plan_fingerprint": plan.fingerprint,
                    "planned_mutation_count": len(plan.mutations),
                    "initialization_noop": plan.noop,
                }
            )
        else:
            data.update({"outcome": "BLOCKED", "reason_code": inspection.reason_code})
        return ok_result("SHEETS_INIT_PLAN", "Sheets initialization plan generated.", data)

    if inspection.status == "MISSING":
        if not args.bootstrap_lock_storage:
            raise InitSheetsCliError(
                "SHEETS_INIT_BOOTSTRAP_REQUIRED",
                "Metadata",
                "TAB_MISSING",
                exit_code=EXIT_POLICY_BLOCKED,
                data={**base_data, "spreadsheet_id": spreadsheet_id, "bootstrap_required": True},
            )
        if args.confirm_spreadsheet_id is None:
            raise InitSheetsCliError(
                "SHEETS_BOOTSTRAP_CONFIRMATION_REQUIRED",
                "confirm_spreadsheet_id",
                "REQUIRED",
                exit_code=EXIT_POLICY_BLOCKED,
            )
        if args.confirm_spreadsheet_id != spreadsheet_id:
            raise InitSheetsCliError(
                "SHEETS_BOOTSTRAP_CONFIRMATION_MISMATCH",
                "confirm_spreadsheet_id",
                "MISMATCH",
                exit_code=EXIT_POLICY_BLOCKED,
            )
        wal = make_wal(
            args, deps, resolve_repository_root(Path(args.config).resolve(), config), run_id
        )
        bootstrapper = SheetsBootstrapper(
            backend=backend,
            wal=wal,
            operation_id_factory=deps.operation_id_factory,
        )
        bootstrap_result = bootstrapper.bootstrap()
    elif inspection.status != "READY":
        raise InitSheetsCliError(
            inspection.reason_code or "SHEETS_BOOTSTRAP_CONFLICT",
            "Metadata",
            "BOOTSTRAP_BLOCKED",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    else:
        wal = make_wal(
            args, deps, resolve_repository_root(Path(args.config).resolve(), config), run_id
        )
        bootstrap_result = None

    initializer = build_initializer(args, deps, config, backend, wal, run_id)
    init_result = initializer.execute(schema, init_policy(config, run_id))
    data = {
        **base_data,
        "action": "initialize_existing",
        "outcome": init_result.outcome.value,
        "spreadsheet_id": spreadsheet_id,
        "created": False,
        "bootstrap_required": bootstrap_result is not None,
        "bootstrapped": bool(bootstrap_result and bootstrap_result.bootstrapped),
        "initialization_noop": init_result.noop,
        "plan_fingerprint": init_result.plan_fingerprint,
        "planned_mutation_count": init_result.planned_mutation_count,
        "applied_mutation_count": init_result.applied_mutation_count,
        "read_back_verified": init_result.read_back_verified,
        "wal_pending_written": init_result.wal_pending_written,
        "wal_acknowledged": init_result.wal_acknowledged,
        "lock_released": init_result.lock_released,
        "config_write_requested": False,
        "config_written": False,
    }
    return ok_result("SHEETS_INIT_OK", "Sheets initialization completed.", data)


def create_and_initialize(
    args: argparse.Namespace,
    deps: InitSheetsDependencies,
    schema: Any,
    config: Mapping[str, Any],
    config_path: Path,
    repository_root: Path,
    credential_result: Any,
    run_id: str,
    base_data: dict[str, Any],
) -> CliResult:
    """Create a Spreadsheet with Metadata and initialize schema tabs."""

    sheets = config.get("sheets", {})
    if not isinstance(sheets, Mapping) or sheets.get("create_policy") != "init-only":
        raise InitSheetsCliError(
            "SHEETS_CREATE_POLICY_BLOCKED",
            "sheets.create_policy",
            "INIT_ONLY_REQUIRED",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    title = resolve_title(args, config)
    if args.dry_run:
        data = {
            **base_data,
            "action": "create_plan",
            "dry_run": True,
            "created": False,
            "bootstrap_required": False,
            "bootstrapped": False,
            "planned_mutation_count": 0,
            "config_write_requested": args.write_config,
            "config_written": False,
            "advisory": True,
            "warnings": create_warnings(),
        }
        return ok_result("SHEETS_INIT_PLAN", "Create plan generated.", data)

    service = service_from_credentials(deps, credential_result)
    create_backend = make_backend(deps, "", service)
    wal = make_wal(args, deps, repository_root, run_id)
    payload = create_payload(title, schema)
    operation_id = deps.operation_id_factory()
    try:
        wal.append_pending(CREATE_OPERATION, payload, operation_id=operation_id)
    except WalError as exc:
        raise InitSheetsCliError(
            "SHEETS_INIT_WAL_FAILED",
            exc.path,
            exc.reason,
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None
    try:
        created = create_backend.create_spreadsheet(
            title,
            initial_tabs=(InitialSheetSpec(METADATA_TAB, METADATA_HEADERS),),
        )
    except SheetsBackendError as exc:
        code = "SHEETS_CREATE_MANUAL_RECONCILIATION_REQUIRED" if exc.may_have_applied else exc.code
        raise InitSheetsCliError(
            code,
            exc.path,
            exc.reason,
            exit_code=exc.exit_code,
            data={**base_data, "created": False, "wal_pending_written": True},
        ) from None
    try:
        wal.append_ack(operation_id)
    except WalError as exc:
        raise InitSheetsCliError(
            "SHEETS_INIT_WAL_FAILED",
            exc.path,
            exc.reason,
            exit_code=EXIT_EXTERNAL_FAILURE,
            data={**base_data, "spreadsheet_id": created.spreadsheet_id, "created": True},
        ) from None

    backend = make_backend(deps, created.spreadsheet_id, service)
    inspection = backend.inspect_metadata_storage()
    if inspection.status != "READY":
        raise InitSheetsCliError(
            inspection.reason_code or "SHEETS_BOOTSTRAP_CONFLICT",
            "Metadata",
            "CREATE_METADATA_INVALID",
            exit_code=EXIT_POLICY_BLOCKED,
            data={**base_data, "spreadsheet_id": created.spreadsheet_id, "created": True},
        )
    initializer = build_initializer(args, deps, config, backend, wal, run_id)
    init_result = initializer.execute(schema, init_policy(config, run_id))

    config_result = None
    if (
        args.write_config
        and init_result.lock_released
        and init_result.outcome
        in {
            InitOutcome.APPLIED,
            InitOutcome.NOOP,
        }
    ):
        writer = deps.config_writer_factory(
            repository_root=repository_root,
            schema_path=DEFAULT_CONFIG_SCHEMA,
            clock=lambda: deps.clock().replace(":", "").replace("-", ""),
        )
        try:
            config_result = writer.write_spreadsheet_id(config_path, created.spreadsheet_id)
        except ConfigWriteError as exc:
            raise InitSheetsCliError(
                exc.code,
                exc.path,
                exc.reason,
                exit_code=exc.exit_code,
                data={**base_data, "spreadsheet_id": created.spreadsheet_id, "created": True},
            ) from None
    data = {
        **base_data,
        "action": "create",
        "outcome": init_result.outcome.value,
        "spreadsheet_id": created.spreadsheet_id,
        "created": True,
        "bootstrap_required": False,
        "bootstrapped": True,
        "initialization_noop": init_result.noop,
        "plan_fingerprint": init_result.plan_fingerprint,
        "planned_mutation_count": init_result.planned_mutation_count,
        "applied_mutation_count": init_result.applied_mutation_count,
        "read_back_verified": init_result.read_back_verified,
        "wal_pending_written": init_result.wal_pending_written,
        "wal_acknowledged": init_result.wal_acknowledged,
        "lock_released": init_result.lock_released,
        "config_write_requested": args.write_config,
        "config_written": bool(config_result and config_result.config_written),
        "backup_created": bool(config_result and config_result.backup_created),
        "warnings": create_warnings(),
    }
    return ok_result("SHEETS_INIT_OK", "Spreadsheet created and initialized.", data)


def resume_existing(
    args: argparse.Namespace,
    deps: InitSheetsDependencies,
    schema: Any,
    credential_result: Any,
    spreadsheet_id: str,
    run_id: str,
    base_data: dict[str, Any],
) -> CliResult:
    """Resume pending WAL without blind resend."""

    service = service_from_credentials(deps, credential_result)
    backend = make_backend(deps, spreadsheet_id, service)
    wal = deps.wal_factory(args.wal.resolve(), run_id, clock=deps.clock)
    pending = wal.replay_plan()
    if not pending:
        return ok_result(
            "SHEETS_INIT_RESUME_OK",
            "No pending WAL entries.",
            {**base_data, "action": "resume", "outcome": "NOOP", "config_written": False},
        )
    first = pending[0]
    if first.operation == CREATE_OPERATION:
        raise InitSheetsCliError(
            "SHEETS_CREATE_MANUAL_RECONCILIATION_REQUIRED",
            "wal",
            "CREATE_PENDING",
            exit_code=EXIT_POLICY_BLOCKED,
            data={**base_data, "action": "resume", "config_written": False},
        )
    if first.operation == BOOTSTRAP_OPERATION:
        result = SheetsBootstrapper(
            backend=backend,
            wal=wal,
            operation_id_factory=deps.operation_id_factory,
        ).reconcile()
        data = {
            **base_data,
            "action": "resume",
            "outcome": result.outcome.value,
            "bootstrap_required": result.bootstrap_required,
            "bootstrapped": result.bootstrapped,
            "wal_acknowledged": result.wal_acknowledged,
            "config_written": False,
        }
        return ok_result("SHEETS_INIT_RESUME_OK", "Bootstrap WAL reconciled.", data)
    if first.operation == INIT_OPERATION:
        initializer = build_initializer(
            args, deps, {"sheets": {"lock_ttl_minutes": 1}}, backend, wal, run_id
        )
        result = initializer.reconcile_pending(
            schema, InitPolicy(run_id=run_id, ttl_seconds=60, commit_sha="resume")
        )
        data = {
            **base_data,
            "action": "resume",
            "outcome": result.outcome.value,
            "wal_acknowledged": result.wal_acknowledged,
            "read_back_verified": result.read_back_verified,
            "config_written": False,
        }
        return ok_result("SHEETS_INIT_RESUME_OK", "Initializer WAL reconciled.", data)
    raise InitSheetsCliError(
        "INIT_RESUME_UNSUPPORTED_OPERATION",
        "wal.operation",
        "UNSUPPORTED",
        exit_code=EXIT_ARGUMENT_OR_SCHEMA,
    )


def build_initializer(
    args: argparse.Namespace,
    deps: InitSheetsDependencies,
    config: Mapping[str, Any],
    backend: Any,
    wal: AppendOnlyWal | None,
    run_id: str,
) -> SheetsInitializer:
    """Build SheetsInitializer."""

    active_wal = (
        wal if wal is not None else deps.wal_factory(Path("/dev/null"), run_id, clock=deps.clock)
    )
    return SheetsInitializer(
        backend=backend,
        lock_manager=SheetsLockManager(backend),
        wal=active_wal,
        operation_id_factory=deps.operation_id_factory,
    )


def init_policy(config: Mapping[str, Any], run_id: str) -> InitPolicy:
    """Build initializer policy from config."""

    sheets = config.get("sheets", {})
    ttl_minutes = sheets.get("lock_ttl_minutes", 120) if isinstance(sheets, Mapping) else 120
    return InitPolicy(run_id=run_id, ttl_seconds=int(ttl_minutes) * 60, commit_sha="unknown")


def resolve_title(args: argparse.Namespace, config: Mapping[str, Any]) -> str:
    """Resolve and validate create title."""

    project = config.get("project", {})
    title = args.title or (
        project.get("name") if isinstance(project, Mapping) and project.get("name") else None
    )
    if not title and isinstance(project, Mapping):
        title = project.get("project_id")
    if not isinstance(title, str) or title == "":
        raise InitSheetsCliError(
            "SHEETS_CREATE_TITLE_INVALID",
            "title",
            "EMPTY",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    if len(title) > 200 or any(ord(char) < 32 for char in title) or secret_findings(title):
        raise InitSheetsCliError(
            "SHEETS_CREATE_TITLE_INVALID",
            "title",
            "UNSAFE",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    return title


def create_payload(title: str, schema: Any) -> dict[str, Any]:
    """Return safe create WAL payload."""

    schema_material = {
        "schema_version": schema.schema_version,
        "tabs": [{"name": tab.name, "headers": list(tab.headers)} for tab in schema.tabs],
    }
    material = {
        "title_fingerprint": "sha256:" + hashlib.sha256(title.encode("utf-8")).hexdigest(),
        "schema_fingerprint": "sha256:"
        + hashlib.sha256(canonical_json(schema_material).encode("utf-8")).hexdigest(),
        "initial_tabs": [METADATA_TAB],
    }
    material["operation_id_scope"] = CREATE_OPERATION
    return material


def validate_run_id(value: str) -> None:
    """Validate run id."""

    if not value or len(value) > 128 or any(char not in RUN_ID_PATTERN for char in value):
        raise InitSheetsCliError("ARGUMENT_INVALID", "run_id", "INVALID_RUN_ID")


def validate_spreadsheet_id(value: str) -> None:
    """Validate spreadsheet id shape."""

    if len(value) < 5 or len(value) > 256 or any(char not in RUN_ID_PATTERN for char in value):
        raise InitSheetsCliError("ARGUMENT_INVALID", "spreadsheet_id", "INVALID_ID")


def ok_result(code: str, message: str, data: dict[str, Any]) -> CliResult:
    """Build success result."""

    return CliResult(ok=True, code=code, message=message, details=[], data=data)


def create_warnings() -> list[str]:
    """Return create warnings."""

    return [
        "SERVICE_ACCOUNT_SPREADSHEET_NOT_AUTOSHARED",
        "DRIVE_SHARING_NOT_PERFORMED",
        "MANUAL_DRIVE_ACCESS_CONFIGURATION_REQUIRED",
    ]

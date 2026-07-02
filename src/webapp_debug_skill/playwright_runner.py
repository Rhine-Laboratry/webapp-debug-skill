"""Safe Playwright runner preflight and execution planning."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from webapp_debug_skill.cli import CliResult, Issue, emit_result
from webapp_debug_skill.config import DEFAULT_CONFIG_SCHEMA, load_yaml_file, validate_config
from webapp_debug_skill.errors import (
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_EXTERNAL_FAILURE,
    EXIT_OK,
    EXIT_POLICY_BLOCKED,
    EXIT_UNEXPECTED,
)
from webapp_debug_skill.playwright_generator import GENERATION_MANIFEST_NAME
from webapp_debug_skill.playwright_project import (
    DEFAULT_PROJECT_DIR,
    GENERATED_MARKER,
    MANIFEST_NAME,
    safe_relative_path,
    sha256_bytes,
)
from webapp_debug_skill.redaction import secret_findings

EXECUTION_OPT_IN_ENV = "WEBAPP_DEBUG_RUN_PLAYWRIGHT"
PROJECT_MANIFEST_VERSION = 1
GENERATION_MANIFEST_VERSION = 1
MAX_GENERATED_FILE_BYTES = 2_000_000
FORBIDDEN_GENERATED_TOKENS = (
    "storageState",
    ".auth",
    "Cookie" + ":",
    "Authorization" + ":",
    "test.only",
    "describe.only",
    "page.pause",
    "child_process",
    "exec(",
    "spawn(",
)


class PlaywrightRunnerError(RuntimeError):
    """Safe Playwright runner error."""

    def __init__(
        self,
        code: str,
        path: str = "playwright_runner",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_POLICY_BLOCKED,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        safe_code = "PLAYWRIGHT_RUNNER_FAILED" if secret_findings(code) else code
        super().__init__(safe_code)
        self.code = safe_code
        self.path = "playwright_runner" if secret_findings(path) else path
        self.reason = "FAILED" if secret_findings(reason) else reason
        self.exit_code = exit_code
        self.data = dict(data or {})


@dataclass(frozen=True)
class RunnerDependencies:
    """Injectable runner dependencies for deterministic tests."""

    environ: Mapping[str, str] | None = None
    command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None


@dataclass(frozen=True)
class GeneratedValidation:
    """Result of generated Playwright artifact validation."""

    generated_count: int
    blocked_count: int
    manifest_file_count: int


@dataclass(frozen=True)
class RunnerPlan:
    """Safe execution plan for Playwright tests."""

    root: Path
    project_dir: Path
    command: tuple[str, ...]
    generated: GeneratedValidation
    package_manager: str
    base_url: str
    base_url_status: str
    auth_state_status: str
    browser_status: str
    db_guard_status: str

    def to_safe_dict(
        self,
        *,
        dry_run: bool,
        execute_requested: bool,
        execution_performed: bool = False,
        return_code: int | None = None,
    ) -> dict[str, Any]:
        """Return safe CLI data."""

        data: dict[str, Any] = {
            "dry_run": dry_run,
            "execute_requested": execute_requested,
            "execution_performed": execution_performed,
            "project_dir": safe_relative_path(self.root, self.project_dir),
            "command": list(self.command),
            "package_manager": self.package_manager,
            "generated_count": self.generated.generated_count,
            "blocked_count": self.generated.blocked_count,
            "manifest_file_count": self.generated.manifest_file_count,
            "db_guard": self.db_guard_status,
            "auth_state_policy": self.auth_state_status,
            "network_policy": self.base_url_status,
            "browser_policy": self.browser_status,
        }
        if return_code is not None:
            data["playwright_return_code"] = return_code
        return data


def build_parser() -> argparse.ArgumentParser:
    """Build run_playwright_tests parser."""

    parser = argparse.ArgumentParser(
        description="Preflight and optionally run generated Playwright tests safely."
    )
    parser.add_argument("--config", type=Path, default=Path(".webapp-debug/config.yml"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--project-dir", type=Path, default=DEFAULT_PROJECT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run Playwright only after all gates pass and opt-in environment is set.",
    )
    parser.add_argument(
        "--confirm-db-runtime-guard",
        action="store_true",
        help="Confirm the DB runtime guard was verified outside this dry-run-safe CLI.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(
    argv: list[str] | None = None,
    deps: RunnerDependencies | None = None,
) -> int:
    """Run run_playwright_tests CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args, deps or RunnerDependencies())
    except PlaywrightRunnerError as exc:
        result = CliResult(
            ok=False,
            code=exc.code,
            message="Playwright runner preflight failed.",
            details=[Issue(exc.path, exc.reason)],
            data=exc.data,
        )
        emit_result(result, args.format)
        return exc.exit_code
    except Exception:
        result = CliResult(
            ok=False,
            code="PLAYWRIGHT_RUNNER_UNEXPECTED",
            message="Unexpected Playwright runner failure.",
            details=[Issue("playwright_runner", "UNEXPECTED")],
        )
        emit_result(result, args.format)
        return EXIT_UNEXPECTED
    emit_result(result, args.format)
    if result.ok:
        return EXIT_OK
    return EXIT_POLICY_BLOCKED


def run(args: argparse.Namespace, deps: RunnerDependencies) -> CliResult:
    """Build a runner preflight plan and optionally execute Playwright."""

    if args.dry_run and args.execute:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_ARGUMENT_INVALID",
            "execute",
            "DRY_RUN_CONFLICT",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )

    root = resolve_root(args.root)
    project_dir = resolve_project_dir(root, args.project_dir)
    config_path = resolve_config_path(root, args.config)
    config = read_validated_config(config_path)
    plan = build_runner_plan(root, project_dir, config)

    if args.dry_run or not args.execute:
        return CliResult(
            ok=True,
            code="PLAYWRIGHT_RUNNER_DRY_RUN" if args.dry_run else "PLAYWRIGHT_RUNNER_PREFLIGHT_OK",
            message=(
                "Playwright runner dry-run plan generated."
                if args.dry_run
                else "Playwright runner preflight passed; execution was not requested."
            ),
            details=[],
            data=plan.to_safe_dict(
                dry_run=args.dry_run,
                execute_requested=bool(args.execute),
            ),
        )

    enforce_execution_opt_in(args, deps)
    process = execute_playwright(plan, deps)
    if process.returncode != 0:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_EXECUTION_FAILED",
            "playwright",
            "NONZERO_EXIT",
            exit_code=EXIT_EXTERNAL_FAILURE,
            data=plan.to_safe_dict(
                dry_run=False,
                execute_requested=True,
                execution_performed=True,
                return_code=process.returncode,
            ),
        )
    return CliResult(
        ok=True,
        code="PLAYWRIGHT_RUNNER_EXECUTED",
        message="Playwright execution completed.",
        details=[],
        data=plan.to_safe_dict(
            dry_run=False,
            execute_requested=True,
            execution_performed=True,
            return_code=process.returncode,
        ),
    )


def resolve_root(path: Path) -> Path:
    """Resolve repository root."""

    try:
        root = path.expanduser().resolve(strict=True)
    except OSError:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_ROOT_INVALID",
            "root",
            "MISSING",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        ) from None
    if not root.is_dir():
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_ROOT_INVALID",
            "root",
            "NOT_DIRECTORY",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return root


def resolve_project_dir(root: Path, value: Path) -> Path:
    """Resolve generated Playwright project dir below root."""

    if secret_findings(value.as_posix()):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_PROJECT_DIR_UNSAFE",
            "project_dir",
            "SECRET_DETECTED",
        )
    candidate = value.expanduser() if value.is_absolute() else root / value
    project_dir = Path(os.path.normpath(os.fspath(candidate)))
    ensure_within_root(root, project_dir, "project_dir")
    ensure_no_symlink_path(root, project_dir)
    return project_dir


def resolve_config_path(root: Path, value: Path) -> Path:
    """Resolve config path without exposing its contents."""

    if secret_findings(value.as_posix()):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_CONFIG_UNSAFE",
            "config",
            "SECRET_DETECTED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    candidate = value.expanduser() if value.is_absolute() else root / value
    try:
        config_path = candidate.resolve(strict=True)
    except OSError:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_CONFIG_INVALID",
            "config",
            "MISSING",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        ) from None
    if config_path.is_symlink() or not config_path.is_file():
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_CONFIG_INVALID",
            "config",
            "NOT_REGULAR_FILE",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return config_path


def read_validated_config(config_path: Path) -> dict[str, Any]:
    """Validate and load config for test execution mode."""

    validation = validate_config(config_path, mode="test", schema_path=DEFAULT_CONFIG_SCHEMA)
    if not validation.ok:
        if validation.code == "CONFIG_DB_GUARD_INCOMPLETE":
            raise PlaywrightRunnerError(
                "PLAYWRIGHT_RUNNER_DB_GUARD_INCOMPLETE",
                first_issue_path(validation.details, "database"),
                first_issue_reason(validation.details, "INCOMPLETE"),
                data={"capabilities": validation.data.get("capabilities", [])},
            )
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_CONFIG_INVALID",
            first_issue_path(validation.details, "config"),
            first_issue_reason(validation.details, validation.code),
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )

    config, issues = load_yaml_file(config_path)
    if issues:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_CONFIG_INVALID",
            first_issue_path(issues, "config"),
            first_issue_reason(issues, "READ_FAILED"),
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    assert config is not None
    return config


def build_runner_plan(root: Path, project_dir: Path, config: Mapping[str, Any]) -> RunnerPlan:
    """Build a fully validated runner plan."""

    generated = validate_generated_artifacts(root, project_dir)
    network_status, base_url = validate_network_policy(config)
    auth_status = validate_auth_state_policy(root, config)
    browser_status = validate_browser_policy(config)
    package_manager = read_package_manager(project_dir)
    command = runner_command(package_manager)
    return RunnerPlan(
        root=root,
        project_dir=project_dir,
        command=command,
        generated=generated,
        package_manager=package_manager,
        base_url=base_url,
        base_url_status=network_status,
        auth_state_status=auth_status,
        browser_status=browser_status,
        db_guard_status="READY_CONFIG_ONLY",
    )


def validate_generated_artifacts(root: Path, project_dir: Path) -> GeneratedValidation:
    """Validate generated files by manifest and checksum before running."""

    manifest_path = project_dir / MANIFEST_NAME
    manifest = read_json_mapping(manifest_path, MANIFEST_NAME)
    if manifest.get("schema_version") != PROJECT_MANIFEST_VERSION:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            MANIFEST_NAME,
            "MANIFEST_SCHEMA_INVALID",
        )
    if manifest.get("generator") != "webapp-debug":
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            MANIFEST_NAME,
            "GENERATOR_INVALID",
        )
    files = manifest.get("files")
    if not isinstance(files, list) or not all(isinstance(item, Mapping) for item in files):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            MANIFEST_NAME,
            "FILES_INVALID",
        )

    manifest_entries: dict[str, Mapping[str, Any]] = {}
    for item in files:
        path = item.get("path")
        if not isinstance(path, str) or secret_findings(path):
            raise PlaywrightRunnerError(
                "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
                MANIFEST_NAME,
                "PATH_INVALID",
            )
        manifest_entries[path] = item
        validate_manifest_file_entry(root, path, item)

    generation_path = project_dir / "generated" / GENERATION_MANIFEST_NAME
    generation = read_json_mapping(generation_path, GENERATION_MANIFEST_NAME)
    if generation.get("schema_version") != GENERATION_MANIFEST_VERSION:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            GENERATION_MANIFEST_NAME,
            "GENERATION_SCHEMA_INVALID",
        )
    if generation.get("generator") != "webapp-debug":
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            GENERATION_MANIFEST_NAME,
            "GENERATOR_INVALID",
        )
    scenarios = generation.get("scenarios")
    if not isinstance(scenarios, list) or not all(isinstance(item, Mapping) for item in scenarios):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            GENERATION_MANIFEST_NAME,
            "SCENARIOS_INVALID",
        )

    generated_count = 0
    blocked_count = 0
    for index, scenario in enumerate(scenarios):
        status = scenario.get("status")
        if status == "BLOCKED":
            blocked_count += 1
            continue
        if status != "GENERATED":
            raise PlaywrightRunnerError(
                "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
                f"scenarios.[{index}].status",
                "STATUS_INVALID",
            )
        generated_count += 1
        validate_generated_scenario(root, scenario, manifest_entries, index)

    if generated_count == 0:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            GENERATION_MANIFEST_NAME,
            "NO_GENERATED_SCENARIOS",
        )
    return GeneratedValidation(
        generated_count=generated_count,
        blocked_count=blocked_count,
        manifest_file_count=len(files),
    )


def validate_manifest_file_entry(
    root: Path,
    relative_path: str,
    entry: Mapping[str, Any],
) -> None:
    """Validate one project manifest file entry."""

    target = root / relative_path
    ensure_within_root(root, target, relative_path)
    ensure_no_symlink_path(root, target)
    if not target.is_file():
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            safe_manifest_path(relative_path),
            "FILE_MISSING",
        )
    try:
        content = target.read_bytes()
    except OSError:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_READ_FAILED",
            safe_manifest_path(relative_path),
            "READ_FAILED",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None
    if len(content) > MAX_GENERATED_FILE_BYTES:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            safe_manifest_path(relative_path),
            "FILE_TOO_LARGE",
        )
    expected_sha = entry.get("sha256")
    if not isinstance(expected_sha, str) or sha256_bytes(content) != expected_sha:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            safe_manifest_path(relative_path),
            "CHECKSUM_MISMATCH",
        )
    text = content.decode("utf-8", errors="replace")
    if entry.get("generated_marker_required") is True and GENERATED_MARKER not in text:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            safe_manifest_path(relative_path),
            "GENERATED_MARKER_MISSING",
        )
    if secret_findings(text):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            safe_manifest_path(relative_path),
            "SECRET_DETECTED",
        )
    if relative_path.endswith((".ts", ".tsx")):
        validate_generated_typescript(text, relative_path)


def validate_generated_scenario(
    root: Path,
    scenario: Mapping[str, Any],
    manifest_entries: Mapping[str, Mapping[str, Any]],
    index: int,
) -> None:
    """Validate one generated scenario manifest row."""

    scenario_id = scenario.get("scenario_id")
    test_file = scenario.get("test_file")
    if not isinstance(scenario_id, str) or not isinstance(test_file, str):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            f"scenarios.[{index}]",
            "SCENARIO_METADATA_INVALID",
        )
    if secret_findings(scenario_id) or secret_findings(test_file):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            f"scenarios.[{index}]",
            "SECRET_DETECTED",
        )
    entry = manifest_entries.get(test_file)
    if entry is None:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            "generation_manifest",
            "TEST_FILE_NOT_IN_PROJECT_MANIFEST",
        )
    metadata = entry.get("metadata")
    if not isinstance(metadata, Mapping) or metadata.get("scenario_id") != scenario_id:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            safe_manifest_path(test_file),
            "SCENARIO_METADATA_MISMATCH",
        )
    target = root / test_file
    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_READ_FAILED",
            safe_manifest_path(test_file),
            "READ_FAILED",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None
    if f"scenario_id: {scenario_id}" not in content:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            safe_manifest_path(test_file),
            "SCENARIO_ID_MISSING",
        )


def validate_generated_typescript(content: str, relative_path: str) -> None:
    """Validate generated TypeScript stays static and runner-safe."""

    for token in FORBIDDEN_GENERATED_TOKENS:
        if token in content:
            raise PlaywrightRunnerError(
                "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
                safe_manifest_path(relative_path),
                "FORBIDDEN_TOKEN",
            )
    if content.count("{") != content.count("}"):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            safe_manifest_path(relative_path),
            "BRACE_MISMATCH",
        )


def validate_network_policy(config: Mapping[str, Any]) -> tuple[str, str]:
    """Validate base URL and host allowlist without network access."""

    app = require_mapping(config, "runtime.app")
    operations = require_mapping(config, "operations")
    base_url = app.get("base_url")
    readiness_url = app.get("readiness_url")
    allowed_hosts = operations.get("allowed_hosts")
    call_external_api = operations.get("call_external_api")
    if not isinstance(base_url, str) or secret_findings(base_url):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_NETWORK_POLICY_BLOCKED",
            "runtime.app.base_url",
            "BASE_URL_INVALID",
        )
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_NETWORK_POLICY_BLOCKED",
            "runtime.app.base_url",
            "BASE_URL_INVALID",
        )
    if not isinstance(allowed_hosts, list) or not all(
        isinstance(item, str) for item in allowed_hosts
    ):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_NETWORK_POLICY_BLOCKED",
            "operations.allowed_hosts",
            "HOST_ALLOWLIST_INVALID",
        )
    if secret_findings(allowed_hosts):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_NETWORK_POLICY_BLOCKED",
            "operations.allowed_hosts",
            "SECRET_DETECTED",
        )
    if parsed.hostname not in set(allowed_hosts):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_NETWORK_POLICY_BLOCKED",
            "runtime.app.base_url",
            "HOST_NOT_ALLOWLISTED",
        )
    if call_external_api not in {"allowlisted-only", "deny"}:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_NETWORK_POLICY_BLOCKED",
            "operations.call_external_api",
            "EXTERNAL_POLICY_INVALID",
        )
    if not isinstance(readiness_url, str) or not readiness_url.startswith("/"):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_NETWORK_POLICY_BLOCKED",
            "runtime.app.readiness_url",
            "READINESS_URL_UNSAFE",
        )
    return "ALLOWLISTED", base_url


def validate_auth_state_policy(root: Path, config: Mapping[str, Any]) -> str:
    """Validate auth state location without reading credential material."""

    auth = require_mapping(config, "authentication")
    strategy_order = auth.get("strategy_order")
    storage_state_dir = auth.get("storage_state_dir")
    if not isinstance(strategy_order, list) or "storage-state" not in strategy_order:
        return "NO_STORAGE_STATE_STRATEGY"
    if not isinstance(storage_state_dir, str) or secret_findings(storage_state_dir):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_AUTH_STATE_UNSAFE",
            "authentication.storage_state_dir",
            "PATH_INVALID",
        )
    value = Path(storage_state_dir)
    candidate = value.expanduser() if value.is_absolute() else root / value
    path = Path(os.path.normpath(os.fspath(candidate)))
    try:
        ensure_within_root(root, path, "authentication.storage_state_dir")
        ensure_no_symlink_path(root, path)
    except PlaywrightRunnerError as exc:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_AUTH_STATE_UNSAFE",
            "authentication.storage_state_dir",
            exc.reason,
        ) from None
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_AUTH_STATE_UNSAFE",
            "authentication.storage_state_dir",
            "NOT_DIRECTORY",
        )
    if any(part in {".git", "node_modules", "vendor"} for part in path.relative_to(root).parts):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_AUTH_STATE_UNSAFE",
            "authentication.storage_state_dir",
            "FORBIDDEN_DIRECTORY",
        )
    return "PATH_SAFE_CONTENT_NOT_READ"


def validate_browser_policy(config: Mapping[str, Any]) -> str:
    """Validate browser policy fields remain constrained."""

    playwright = require_mapping(config, "playwright")
    if playwright.get("browser") != "chromium":
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_BROWSER_POLICY_BLOCKED",
            "playwright.browser",
            "UNSUPPORTED_BROWSER",
        )
    if playwright.get("workers") != 1:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_BROWSER_POLICY_BLOCKED",
            "playwright.workers",
            "WORKERS_UNSAFE",
        )
    if playwright.get("retries") != 1:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_BROWSER_POLICY_BLOCKED",
            "playwright.retries",
            "RETRIES_UNSAFE",
        )
    return "CHROMIUM_SINGLE_WORKER"


def read_package_manager(project_dir: Path) -> str:
    """Read the generated package manager from the bootstrap manifest."""

    manifest = read_json_mapping(project_dir / MANIFEST_NAME, MANIFEST_NAME)
    package_manager = manifest.get("package_manager")
    if package_manager not in {"npm", "pnpm", "yarn"}:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            MANIFEST_NAME,
            "PACKAGE_MANAGER_INVALID",
        )
    return str(package_manager)


def runner_command(package_manager: str) -> tuple[str, ...]:
    """Return a deterministic command plan without executing it."""

    if package_manager == "yarn":
        return ("yarn", "test")
    if package_manager == "pnpm":
        return ("pnpm", "test")
    return ("npm", "test")


def enforce_execution_opt_in(args: argparse.Namespace, deps: RunnerDependencies) -> None:
    """Require explicit opt-in before any external command."""

    environ = deps.environ if deps.environ is not None else os.environ
    if environ.get(EXECUTION_OPT_IN_ENV) != "1":
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_EXECUTION_OPT_IN_REQUIRED",
            "environment",
            "OPT_IN_ENV_MISSING",
        )
    if not args.confirm_db_runtime_guard:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_DB_RUNTIME_UNCONFIRMED",
            "database.runtime_guard",
            "CONFIRMATION_REQUIRED",
        )


def execute_playwright(
    plan: RunnerPlan,
    deps: RunnerDependencies,
) -> subprocess.CompletedProcess[str]:
    """Execute Playwright after preflight and opt-in gates."""

    command_runner = deps.command_runner or subprocess.run
    base_env = deps.environ if deps.environ is not None else os.environ
    env = dict(base_env)
    env["WEBAPP_DEBUG_BASE_URL"] = plan.base_url
    try:
        return command_runner(
            list(plan.command),
            cwd=plan.project_dir,
            check=False,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except OSError:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_EXECUTION_FAILED",
            "playwright",
            "COMMAND_FAILED",
            exit_code=EXIT_EXTERNAL_FAILURE,
            data=plan.to_safe_dict(dry_run=False, execute_requested=True),
        ) from None


def read_json_mapping(path: Path, label: str) -> dict[str, Any]:
    """Read a JSON mapping with safe diagnostics."""

    if path.is_symlink() or not path.is_file():
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            label,
            "MISSING",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            label,
            "JSON_INVALID",
        ) from None
    except OSError:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_READ_FAILED",
            label,
            "READ_FAILED",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None
    if not isinstance(data, dict):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            label,
            "OBJECT_REQUIRED",
        )
    if secret_findings(data):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID",
            label,
            "SECRET_DETECTED",
        )
    return data


def require_mapping(value: Mapping[str, Any], path: str) -> Mapping[str, Any]:
    """Return a nested mapping or raise a safe policy error."""

    current: Any = value
    for part in path.split("."):
        if not isinstance(current, Mapping):
            break
        current = current.get(part)
    if not isinstance(current, Mapping):
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_CONFIG_INVALID",
            path,
            "MAPPING_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return current


def ensure_within_root(root: Path, path: Path, label: str) -> None:
    """Ensure path stays inside the repository root."""

    try:
        path.relative_to(root)
    except ValueError:
        raise PlaywrightRunnerError(
            "PLAYWRIGHT_RUNNER_PATH_UNSAFE",
            "path" if secret_findings(label) else label,
            "OUTSIDE_ROOT",
        ) from None


def ensure_no_symlink_path(root: Path, target: Path) -> None:
    """Reject symlink components from root to target."""

    try:
        relative_parts = target.relative_to(root).parts
    except ValueError:
        return
    current = root
    for part in relative_parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise PlaywrightRunnerError(
                "PLAYWRIGHT_RUNNER_PATH_UNSAFE",
                safe_relative_path(root, current),
                "SYMLINK_REJECTED",
            )


def first_issue_path(issues: Sequence[Issue], fallback: str) -> str:
    """Return the first safe issue path."""

    if not issues:
        return fallback
    path = issues[0].path
    return fallback if secret_findings(path) else path


def first_issue_reason(issues: Sequence[Issue], fallback: str) -> str:
    """Return the first safe issue reason."""

    if not issues:
        return fallback
    reason = issues[0].reason
    return fallback if secret_findings(reason) else reason


def safe_manifest_path(relative_path: str) -> str:
    """Return a safe generated manifest path."""

    return "generated_file" if secret_findings(relative_path) else relative_path

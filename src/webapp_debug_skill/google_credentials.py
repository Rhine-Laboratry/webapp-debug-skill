"""Safe service account credential loading for Google Sheets."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from webapp_debug_skill.errors import (
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_EXTERNAL_FAILURE,
    EXIT_POLICY_BLOCKED,
)
from webapp_debug_skill.redaction import secret_findings

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DEFAULT_CREDENTIAL_ENV = "WEBAPP_DEBUG_GOOGLE_SERVICE_ACCOUNT"
MAX_CREDENTIAL_BYTES = 128 * 1024


def safe_text(value: object, fallback: str) -> str:
    """Return diagnostic text only when it is safe to display."""

    rendered = str(value)
    return fallback if secret_findings(rendered) else rendered


class GoogleCredentialError(RuntimeError):
    """Safe credential loading error."""

    def __init__(
        self,
        code: str,
        path: str = "google_credentials",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_ARGUMENT_OR_SCHEMA,
    ) -> None:
        safe_code = safe_text(code, "GOOGLE_CREDENTIAL_LOAD_FAILED")
        super().__init__(safe_code)
        self.code = safe_code
        self.path = safe_text(path, "google_credentials")
        self.reason = safe_text(reason, "FAILED")
        self.exit_code = exit_code


@dataclass(frozen=True)
class GoogleCredentialLoadResult:
    """Credential loading result without source path or credential contents."""

    credentials: Any = field(repr=False)
    scopes: tuple[str, ...]
    source_type: str = "service_account_file"


CredentialFactory = Callable[[Mapping[str, Any], tuple[str, ...]], Any]


def default_credential_factory(info: Mapping[str, Any], scopes: tuple[str, ...]) -> Any:
    """Build Google service account credentials with lazy dependency import."""

    try:
        from google.oauth2 import service_account
    except ModuleNotFoundError:
        raise GoogleCredentialError(
            "GOOGLE_DEPENDENCY_MISSING",
            "dependency",
            "GOOGLE_AUTH_MISSING",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None
    try:
        return service_account.Credentials.from_service_account_info(dict(info), scopes=scopes)
    except Exception:
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_LOAD_FAILED",
            "credential",
            "CONSTRUCTOR_FAILED",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None


def load_service_account_credentials(
    *,
    env_name: str = DEFAULT_CREDENTIAL_ENV,
    env: Mapping[str, str] | None = None,
    repository_root: Path | None = None,
    max_size: int = MAX_CREDENTIAL_BYTES,
    credential_factory: CredentialFactory = default_credential_factory,
) -> GoogleCredentialLoadResult:
    """Load service account credentials from a safe file path stored in an env var."""

    if not env_name:
        raise GoogleCredentialError("GOOGLE_CREDENTIAL_ENV_MISSING", "env", "EMPTY_ENV_NAME")
    env_mapping = os.environ if env is None else env
    if env_name not in env_mapping:
        raise GoogleCredentialError("GOOGLE_CREDENTIAL_ENV_MISSING", "env", "NOT_SET")
    raw_path = env_mapping.get(env_name, "")
    if raw_path == "":
        raise GoogleCredentialError("GOOGLE_CREDENTIAL_ENV_MISSING", "env", "EMPTY_VALUE")

    path = Path(raw_path)
    root = repository_root or Path.cwd()
    info = _read_credential_info(path, repository_root=root, max_size=max_size)
    credentials = credential_factory(info, (SHEETS_SCOPE,))
    return GoogleCredentialLoadResult(credentials=credentials, scopes=(SHEETS_SCOPE,))


def _read_credential_info(path: Path, *, repository_root: Path, max_size: int) -> dict[str, Any]:
    """Read and validate a service account credential JSON object."""

    try:
        stat_result = path.lstat()
    except OSError:
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_PATH_INVALID", "credential_path", "NOT_FOUND"
        ) from None

    mode = stat_result.st_mode
    if stat.S_ISLNK(mode):
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_FILE_UNSAFE",
            "credential_path",
            "SYMLINK_REJECTED",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    if not stat.S_ISREG(mode):
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_PATH_INVALID", "credential_path", "NOT_REGULAR_FILE"
        )
    if stat_result.st_size > max_size:
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_FILE_TOO_LARGE",
            "credential_file",
            "TOO_LARGE",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    if os.name == "posix" and stat.S_IMODE(mode) & 0o077:
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_FILE_UNSAFE",
            "credential_file",
            "UNSAFE_PERMISSIONS",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    try:
        resolved_path = path.resolve(strict=True)
        resolved_root = repository_root.resolve(strict=True)
    except OSError:
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_PATH_INVALID", "credential_path", "RESOLVE_FAILED"
        ) from None
    if resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root):
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_FILE_UNSAFE",
            "credential_path",
            "REPOSITORY_PATH_REJECTED",
            exit_code=EXIT_POLICY_BLOCKED,
        )

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_LOAD_FAILED",
            "credential_file",
            "READ_FAILED",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_FORMAT_INVALID",
            "credential_json",
            "JSON_INVALID",
        ) from None
    if not isinstance(parsed, dict):
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_FORMAT_INVALID",
            "credential_json",
            "OBJECT_REQUIRED",
        )
    if parsed.get("type") != "service_account":
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_FORMAT_INVALID",
            "credential_json.type",
            "SERVICE_ACCOUNT_REQUIRED",
        )
    return parsed


def build_sheets_service(
    credentials: Any,
    *,
    service_builder: Callable[..., Any] | None = None,
) -> Any:
    """Build a Google Sheets v4 service without discovery cache."""

    if service_builder is None:
        try:
            from googleapiclient.discovery import build
        except ModuleNotFoundError:
            raise GoogleCredentialError(
                "GOOGLE_DEPENDENCY_MISSING",
                "dependency",
                "GOOGLE_API_CLIENT_MISSING",
                exit_code=EXIT_EXTERNAL_FAILURE,
            ) from None
        service_builder = build
    try:
        return service_builder(
            "sheets",
            "v4",
            credentials=credentials,
            cache_discovery=False,
        )
    except Exception:
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_LOAD_FAILED",
            "google_service",
            "SERVICE_BUILD_FAILED",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None

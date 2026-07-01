"""Safe config update helpers for init_sheets."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from webapp_debug_skill.config import DEFAULT_CONFIG_SCHEMA, load_yaml_module, validate_config
from webapp_debug_skill.errors import (
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_EXTERNAL_FAILURE,
    EXIT_LOCK_CONFLICT,
    EXIT_POLICY_BLOCKED,
)
from webapp_debug_skill.redaction import secret_findings


class ConfigWriteError(RuntimeError):
    """Safe config write failure."""

    def __init__(
        self,
        code: str,
        path: str = "config",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_EXTERNAL_FAILURE,
    ) -> None:
        safe_code = "CONFIG_WRITE_FAILED" if secret_findings(code) else code
        super().__init__(safe_code)
        self.code = safe_code
        self.path = "config" if secret_findings(path) else path
        self.reason = "FAILED" if secret_findings(reason) else reason
        self.exit_code = exit_code


@dataclass(frozen=True)
class ConfigWriteResult:
    """Safe config write result."""

    config_written: bool
    backup_created: bool
    backup_name: str | None = None
    noop: bool = False


def sha256_bytes(data: bytes) -> str:
    """Return sha256 hex digest."""

    return hashlib.sha256(data).hexdigest()


def validate_config_target(path: Path, *, repository_root: Path) -> None:
    """Reject unsafe config update targets."""

    try:
        stat_result = path.lstat()
    except OSError:
        raise ConfigWriteError(
            "CONFIG_TARGET_UNSAFE",
            "config",
            "NOT_FOUND",
            exit_code=EXIT_POLICY_BLOCKED,
        ) from None
    if stat.S_ISLNK(stat_result.st_mode):
        raise ConfigWriteError(
            "CONFIG_TARGET_UNSAFE",
            "config",
            "SYMLINK_REJECTED",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    if not stat.S_ISREG(stat_result.st_mode):
        raise ConfigWriteError(
            "CONFIG_TARGET_UNSAFE",
            "config",
            "NOT_REGULAR_FILE",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    resolved = path.resolve(strict=True)
    example = (
        repository_root / "skills/webapp-debug/assets/webapp-debug.config.example.yml"
    ).resolve()
    assets_dir = (repository_root / "skills/webapp-debug/assets").resolve()
    if resolved == example or resolved.is_relative_to(assets_dir):
        raise ConfigWriteError(
            "CONFIG_TARGET_UNSAFE",
            "config",
            "CANONICAL_ASSET_REJECTED",
            exit_code=EXIT_POLICY_BLOCKED,
        )


class ConfigWriter:
    """Atomic writer for sheets.spreadsheet_id."""

    def __init__(
        self,
        *,
        repository_root: Path,
        schema_path: Path = DEFAULT_CONFIG_SCHEMA,
        clock: Callable[[], str] = lambda: "unknown-time",
        fsync_func: Callable[[int], None] = os.fsync,
        replace_func: Callable[[Path, Path], None] = os.replace,
    ) -> None:
        self.repository_root = repository_root
        self.schema_path = schema_path
        self.clock = clock
        self.fsync_func = fsync_func
        self.replace_func = replace_func

    def write_spreadsheet_id(
        self,
        config_path: Path,
        spreadsheet_id: str,
        *,
        dry_run: bool = False,
    ) -> ConfigWriteResult:
        """Set sheets.spreadsheet_id if currently empty or already same."""

        validate_config_target(config_path, repository_root=self.repository_root)
        try:
            original = config_path.read_bytes()
            stat_before = config_path.stat()
        except OSError:
            raise ConfigWriteError("CONFIG_WRITE_FAILED", "config", "READ_FAILED") from None
        yaml_module = load_yaml_module()
        try:
            loaded = yaml_module.safe_load(original.decode("utf-8"))
        except Exception:
            raise ConfigWriteError(
                "CONFIG_WRITE_FAILED",
                "config",
                "YAML_INVALID",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            ) from None
        if not isinstance(loaded, dict):
            raise ConfigWriteError(
                "CONFIG_WRITE_FAILED",
                "config",
                "YAML_MAPPING_REQUIRED",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
        sheets = loaded.get("sheets")
        if not isinstance(sheets, dict):
            raise ConfigWriteError(
                "CONFIG_WRITE_FAILED",
                "config.sheets",
                "REQUIRED",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
        current = sheets.get("spreadsheet_id", "")
        if current == spreadsheet_id:
            return ConfigWriteResult(config_written=False, backup_created=False, noop=True)
        if current not in {"", None}:
            raise ConfigWriteError(
                "CONFIG_SPREADSHEET_ID_CONFLICT",
                "sheets.spreadsheet_id",
                "DIFFERENT_ID_PRESENT",
                exit_code=EXIT_POLICY_BLOCKED,
            )
        if dry_run:
            return ConfigWriteResult(config_written=False, backup_created=False)

        updated = dict(loaded)
        updated_sheets = dict(sheets)
        updated_sheets["spreadsheet_id"] = spreadsheet_id
        updated["sheets"] = updated_sheets
        rendered = yaml_module.safe_dump(updated, sort_keys=False, allow_unicode=True).encode(
            "utf-8"
        )

        temp_validation_path = config_path.with_name(f".{config_path.name}.validation.tmp")
        try:
            temp_validation_path.write_bytes(rendered)
            validation = validate_config(temp_validation_path, "init", schema_path=self.schema_path)
        finally:
            temp_validation_path.unlink(missing_ok=True)
        if not validation.ok:
            raise ConfigWriteError(
                "CONFIG_WRITE_FAILED",
                "config",
                validation.code,
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )

        backup_path = self._backup_path(config_path, original)
        try:
            shutil.copyfile(config_path, backup_path)
            os.chmod(backup_path, 0o600)
        except OSError:
            raise ConfigWriteError("CONFIG_BACKUP_FAILED", "config", "BACKUP_FAILED") from None
        if backup_path.read_bytes() != original:
            raise ConfigWriteError("CONFIG_BACKUP_FAILED", "config", "BACKUP_VERIFY_FAILED")

        tmp_path: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(rendered)
                handle.flush()
                self.fsync_func(handle.fileno())
            try:
                os.chmod(tmp_path, stat.S_IMODE(stat_before.st_mode))
            except OSError:
                pass
            current_bytes = config_path.read_bytes()
            stat_current = config_path.stat()
            if (
                stat_current.st_mtime_ns != stat_before.st_mtime_ns
                or stat_current.st_size != stat_before.st_size
                or sha256_bytes(current_bytes) != sha256_bytes(original)
            ):
                raise ConfigWriteError(
                    "CONFIG_CONCURRENT_MODIFICATION",
                    "config",
                    "MODIFIED_BEFORE_REPLACE",
                    exit_code=EXIT_LOCK_CONFLICT,
                )
            self.replace_func(tmp_path, config_path)
            tmp_path = None
            self._fsync_dir(config_path.parent)
        except ConfigWriteError:
            raise
        except OSError:
            raise ConfigWriteError("CONFIG_WRITE_FAILED", "config", "WRITE_FAILED") from None
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

        check = validate_config(config_path, "init", schema_path=self.schema_path)
        if not check.ok:
            raise ConfigWriteError(
                "CONFIG_WRITE_FAILED",
                "config",
                "POST_WRITE_VALIDATION_FAILED",
            )
        loaded_after = yaml_module.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded_after.get("sheets", {}).get("spreadsheet_id") != spreadsheet_id:
            raise ConfigWriteError("CONFIG_WRITE_FAILED", "config", "POST_WRITE_VERIFY_FAILED")
        return ConfigWriteResult(
            config_written=True,
            backup_created=True,
            backup_name=backup_path.name,
        )

    def _backup_path(self, config_path: Path, original: bytes) -> Path:
        prefix = f"{config_path.name}.bak.{self.clock()}.{sha256_bytes(original)[:8]}"
        candidate = config_path.with_name(prefix)
        index = 1
        while candidate.exists():
            candidate = config_path.with_name(f"{prefix}.{index}")
            index += 1
        return candidate

    def _fsync_dir(self, directory: Path) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            self.fsync_func(fd)
        finally:
            os.close(fd)

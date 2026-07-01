from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from webapp_debug_skill.google_credentials import (
    SHEETS_SCOPE,
    GoogleCredentialError,
    build_sheets_service,
    load_service_account_credentials,
)

PRIVATE_KEY_MARKER = "-----BEGIN PRIVATE KEY-----SECRET_MARKER_GOOGLE_KEY"
CREDENTIAL_PATH_MARKER = "SECRET_MARKER_CREDENTIAL_PATH"


class FakeCredentials:
    def __repr__(self) -> str:
        return f"FakeCredentials({PRIVATE_KEY_MARKER})"


def safe_info() -> dict[str, str]:
    return {
        "type": "service_account",
        "private_key": PRIVATE_KEY_MARKER,
        "private_key_id": "SECRET_MARKER_PRIVATE_KEY_ID",
        "client_email": "service-account@example.test",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def write_credential(path: Path, payload: Any | None = None, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(safe_info() if payload is None else payload), encoding="utf-8")
    os.chmod(path, mode)
    return path


def fake_factory(info: dict[str, Any], scopes: tuple[str, ...]) -> FakeCredentials:
    assert info["private_key"] == PRIVATE_KEY_MARKER
    assert scopes == (SHEETS_SCOPE,)
    return FakeCredentials()


def assert_no_secret(*values: object) -> None:
    for value in values:
        rendered = str(value)
        assert PRIVATE_KEY_MARKER not in rendered
        assert CREDENTIAL_PATH_MARKER not in rendered


def assert_error(code: str, exc_info: pytest.ExceptionInfo[GoogleCredentialError]) -> None:
    assert exc_info.value.code == code
    assert_no_secret(exc_info.value, exc_info.value.path, exc_info.value.reason)


def test_env_name_missing_unset_and_empty(tmp_path: Path) -> None:
    credential = write_credential(tmp_path / "credential.json")

    with pytest.raises(GoogleCredentialError) as empty_name:
        load_service_account_credentials(
            env_name="",
            env={"GOOGLE": str(credential)},
            repository_root=Path.cwd(),
            credential_factory=fake_factory,
        )
    with pytest.raises(GoogleCredentialError) as unset:
        load_service_account_credentials(
            env_name="GOOGLE",
            env={},
            repository_root=Path.cwd(),
            credential_factory=fake_factory,
        )
    with pytest.raises(GoogleCredentialError) as empty_value:
        load_service_account_credentials(
            env_name="GOOGLE",
            env={"GOOGLE": ""},
            repository_root=Path.cwd(),
            credential_factory=fake_factory,
        )

    assert_error("GOOGLE_CREDENTIAL_ENV_MISSING", empty_name)
    assert_error("GOOGLE_CREDENTIAL_ENV_MISSING", unset)
    assert_error("GOOGLE_CREDENTIAL_ENV_MISSING", empty_value)


def test_path_rejections(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    directory = tmp_path / "dir"
    directory.mkdir()
    target = write_credential(tmp_path / "target.json")
    symlink = tmp_path / "link.json"
    symlink.symlink_to(target)

    for path, code in [
        (missing, "GOOGLE_CREDENTIAL_PATH_INVALID"),
        (directory, "GOOGLE_CREDENTIAL_PATH_INVALID"),
        (symlink, "GOOGLE_CREDENTIAL_FILE_UNSAFE"),
    ]:
        with pytest.raises(GoogleCredentialError) as exc_info:
            load_service_account_credentials(
                env_name="GOOGLE",
                env={"GOOGLE": str(path)},
                repository_root=Path.cwd(),
                credential_factory=fake_factory,
            )
        assert_error(code, exc_info)


def test_repository_path_file_size_and_permission_rejections(tmp_path: Path) -> None:
    repo_credential = write_credential(Path.cwd() / ".tmp-google-credential.json")
    too_large = write_credential(tmp_path / "large.json", {"type": "service_account"})
    too_large.write_text("x" * 140_000, encoding="utf-8")
    os.chmod(too_large, 0o600)
    unsafe = write_credential(tmp_path / "unsafe.json", mode=0o644)

    try:
        with pytest.raises(GoogleCredentialError) as repo_exc:
            load_service_account_credentials(
                env_name="GOOGLE",
                env={"GOOGLE": str(repo_credential)},
                repository_root=Path.cwd(),
                credential_factory=fake_factory,
            )
        with pytest.raises(GoogleCredentialError) as size_exc:
            load_service_account_credentials(
                env_name="GOOGLE",
                env={"GOOGLE": str(too_large)},
                repository_root=Path.cwd(),
                credential_factory=fake_factory,
            )
        with pytest.raises(GoogleCredentialError) as perm_exc:
            load_service_account_credentials(
                env_name="GOOGLE",
                env={"GOOGLE": str(unsafe)},
                repository_root=Path.cwd(),
                credential_factory=fake_factory,
            )
    finally:
        repo_credential.unlink(missing_ok=True)

    assert_error("GOOGLE_CREDENTIAL_FILE_UNSAFE", repo_exc)
    assert_error("GOOGLE_CREDENTIAL_FILE_TOO_LARGE", size_exc)
    assert_error("GOOGLE_CREDENTIAL_FILE_UNSAFE", perm_exc)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("{not json", "JSON_INVALID"),
        ([{"type": "service_account"}], "OBJECT_REQUIRED"),
        ({"type": "authorized_user"}, "SERVICE_ACCOUNT_REQUIRED"),
    ],
)
def test_credential_format_rejections(tmp_path: Path, payload: Any, reason: str) -> None:
    path = tmp_path / "credential.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
        os.chmod(path, 0o600)
    else:
        write_credential(path, payload)

    with pytest.raises(GoogleCredentialError) as exc_info:
        load_service_account_credentials(
            env_name="GOOGLE",
            env={"GOOGLE": str(path)},
            repository_root=Path.cwd(),
            credential_factory=fake_factory,
        )

    assert exc_info.value.code == "GOOGLE_CREDENTIAL_FORMAT_INVALID"
    assert exc_info.value.reason == reason
    assert_no_secret(exc_info.value)


def test_constructor_failure_is_safe(tmp_path: Path) -> None:
    path = write_credential(tmp_path / "credential.json")

    def failing_factory(_info: dict[str, Any], _scopes: tuple[str, ...]) -> Any:
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_LOAD_FAILED",
            "credential",
            PRIVATE_KEY_MARKER,
            exit_code=4,
        )

    with pytest.raises(GoogleCredentialError) as exc_info:
        load_service_account_credentials(
            env_name="GOOGLE",
            env={"GOOGLE": str(path)},
            repository_root=Path.cwd(),
            credential_factory=failing_factory,
        )

    assert_error("GOOGLE_CREDENTIAL_LOAD_FAILED", exc_info)


def test_success_returns_credentials_scope_and_safe_repr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_credential(tmp_path / "credential.json")

    result = load_service_account_credentials(
        env_name="GOOGLE",
        env={"GOOGLE": str(path)},
        repository_root=Path.cwd(),
        credential_factory=fake_factory,
    )

    captured = capsys.readouterr()
    assert isinstance(result.credentials, FakeCredentials)
    assert result.scopes == (SHEETS_SCOPE,)
    assert result.source_type == "service_account_file"
    assert_no_secret(result, captured.out, captured.err)


def test_raw_read_oserror_is_not_exposed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_credential(tmp_path / "credential.json")

    def fail_read_text(self: Path, *_args: object, **_kwargs: object) -> str:
        if self == path:
            raise OSError(CREDENTIAL_PATH_MARKER)
        return ""

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    with pytest.raises(GoogleCredentialError) as exc_info:
        load_service_account_credentials(
            env_name="GOOGLE",
            env={"GOOGLE": str(path)},
            repository_root=Path.cwd(),
            credential_factory=fake_factory,
        )

    assert_error("GOOGLE_CREDENTIAL_LOAD_FAILED", exc_info)


def test_build_sheets_service_uses_expected_arguments() -> None:
    calls: list[dict[str, Any]] = []

    def builder(*args: object, **kwargs: object) -> object:
        calls.append({"args": args, "kwargs": kwargs})
        return object()

    credentials = object()
    service = build_sheets_service(credentials, service_builder=builder)

    assert service is not None
    assert calls == [
        {
            "args": ("sheets", "v4"),
            "kwargs": {"credentials": credentials, "cache_discovery": False},
        }
    ]

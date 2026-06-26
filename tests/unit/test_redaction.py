from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from webapp_debug_skill.redaction import RedactionError, redact_artifact


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/redact_artifact.py"
SECRET = "SECRET_MARKER_DO_NOT_LEAK"


def run_redact(
    input_path: Path,
    output_path: Path,
    *extra_args: str,
    output_format: str = "json",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(SCRIPT),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        "--format-output",
        output_format,
        *extra_args,
    ]
    return subprocess.run(command, check=False, text=True, capture_output=True, env=env)


def assert_no_secret(*values: str | bytes) -> None:
    for value in values:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        assert SECRET not in value


def load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location("redact_artifact_script", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_text_redacts_each_secret_key_and_headers(tmp_path: Path) -> None:
    input_path = tmp_path / "input.log"
    output_path = tmp_path / "output.log"
    report_path = tmp_path / "report.json"
    input_path.write_text(
        "\n".join(
            [
                f"password={SECRET}",
                f"passwd={SECRET}",
                f"secret={SECRET}",
                f"token={SECRET}",
                f"authorization={SECRET}",
                f"cookie={SECRET}",
                f"set-cookie={SECRET}",
                f"api_key={SECRET}",
                f"private_key={SECRET}",
                f"client_secret={SECRET}",
                f"dsn={SECRET}",
                f"Authorization: Bearer {SECRET}",
                f"Cookie: session={SECRET}",
                f"Set-Cookie: session={SECRET}; HttpOnly",
            ]
        ),
        encoding="utf-8",
    )

    process = run_redact(input_path, output_path, "--report", str(report_path))

    assert process.returncode == 0, process.stdout + process.stderr
    output = output_path.read_text(encoding="utf-8")
    report = report_path.read_text(encoding="utf-8")
    assert "<REDACTED:PASSWORD>" in output
    assert "<REDACTED:COOKIE>" in output
    assert_no_secret(process.stdout, process.stderr, output, report)


def test_json_jsonl_yaml_and_har_are_redacted(tmp_path: Path) -> None:
    cases: list[tuple[str, str, str]] = []
    json_path = tmp_path / "input.json"
    json_path.write_text(
        json.dumps({"api-key": SECRET, "nested": {"client_secret": SECRET}}),
        encoding="utf-8",
    )
    cases.append(("json", str(json_path), "--format"))

    jsonl_path = tmp_path / "input.jsonl"
    jsonl_path.write_text(
        "\n".join(
            [
                json.dumps({"token": SECRET}),
                json.dumps({"normal": f"Bearer {SECRET}"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cases.append(("jsonl", str(jsonl_path), "--format"))

    yaml_path = tmp_path / "input.yml"
    yaml_path.write_text(yaml.safe_dump({"private-key": SECRET}), encoding="utf-8")
    cases.append(("yaml", str(yaml_path), "--format"))

    har_path = tmp_path / "input.har"
    har_path.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {
                                "headers": [{"name": "Authorization", "value": f"Bearer {SECRET}"}],
                                "cookies": [{"name": "session_token", "value": SECRET}],
                                "queryString": [{"name": "api_key", "value": SECRET}],
                                "postData": {"text": f"password={SECRET}"},
                            }
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    cases.append(("har", str(har_path), "--format"))

    for artifact_format, input_name, flag in cases:
        output_path = tmp_path / f"out-{artifact_format}"
        process = run_redact(Path(input_name), output_path, flag, artifact_format)

        assert process.returncode == 0, process.stdout + process.stderr
        assert_no_secret(process.stdout, process.stderr, output_path.read_bytes())


def test_auth_schemes_url_userinfo_and_secret_query_params(tmp_path: Path) -> None:
    input_path = tmp_path / "input.txt"
    output_path = tmp_path / "output.txt"
    input_path.write_text(
        "\n".join(
            [
                f"Bearer {SECRET}",
                f"Basic {SECRET}",
                f"https://user:{SECRET}@example.test/path",
                f"https://example.test/path?access_token={SECRET}&safe=1",
            ]
        ),
        encoding="utf-8",
    )

    process = run_redact(input_path, output_path)

    assert process.returncode == 0
    output = output_path.read_text(encoding="utf-8")
    assert "<REDACTED:AUTHORIZATION>" in output
    assert "<REDACTED:URL_USERINFO>" in output
    assert "<REDACTED:QUERY_PARAM>" in output
    assert_no_secret(process.stdout, process.stderr, output)


def test_secret_env_replaces_value_without_printing_value(tmp_path: Path) -> None:
    input_path = tmp_path / "input.txt"
    output_path = tmp_path / "output.txt"
    input_path.write_text(f"body {SECRET}", encoding="utf-8")
    env = os.environ.copy()
    env["WAD_SECRET"] = SECRET

    process = run_redact(input_path, output_path, "--secret-env", "WAD_SECRET", env=env)

    assert process.returncode == 0
    output = output_path.read_text(encoding="utf-8")
    assert "<REDACTED:ENV>" in output
    assert_no_secret(process.stdout, process.stderr, output)


def test_unset_secret_env_does_not_leak_surrounding_data(tmp_path: Path) -> None:
    input_path = tmp_path / "input.txt"
    output_path = tmp_path / "output.txt"
    input_path.write_text("safe text", encoding="utf-8")
    env = os.environ.copy()
    env.pop("WAD_UNSET_SECRET", None)

    process = run_redact(input_path, output_path, "--secret-env", "WAD_UNSET_SECRET", env=env)

    assert process.returncode == 0
    assert "WAD_UNSET_SECRET" not in process.stdout
    assert process.stderr == ""


def test_case_hyphen_and_underscore_key_normalization(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(
        json.dumps({"CLIENT-SECRET": SECRET, "Api_Key": SECRET, "Set_Cookie": SECRET}),
        encoding="utf-8",
    )

    process = run_redact(input_path, output_path)

    assert process.returncode == 0
    assert_no_secret(process.stdout, process.stderr, output_path.read_text(encoding="utf-8"))


def test_existing_output_requires_force(tmp_path: Path) -> None:
    input_path = tmp_path / "input.txt"
    output_path = tmp_path / "output.txt"
    input_path.write_text(f"token={SECRET}", encoding="utf-8")
    output_path.write_text("existing", encoding="utf-8")

    blocked = run_redact(input_path, output_path)

    assert blocked.returncode == 3
    assert output_path.read_text(encoding="utf-8") == "existing"

    forced = run_redact(input_path, output_path, "--force")

    assert forced.returncode == 0
    assert_no_secret(forced.stdout, forced.stderr, output_path.read_text(encoding="utf-8"))


def test_input_output_same_path_and_symlink_are_rejected(tmp_path: Path) -> None:
    input_path = tmp_path / "input.txt"
    input_path.write_text(f"token={SECRET}", encoding="utf-8")

    same = run_redact(input_path, input_path, "--force")

    symlink_path = tmp_path / "link.txt"
    symlink_path.symlink_to(input_path)
    symlink = run_redact(input_path, symlink_path, "--force")

    assert same.returncode == 3
    assert symlink.returncode == 3
    assert_no_secret(same.stdout, same.stderr, symlink.stdout, symlink.stderr)


def test_invalid_json_and_yaml_leave_no_partial_output(tmp_path: Path) -> None:
    json_input = tmp_path / "bad.json"
    json_output = tmp_path / "bad.out"
    json_input.write_text("{bad", encoding="utf-8")

    yaml_input = tmp_path / "bad.yml"
    yaml_output = tmp_path / "bad-yaml.out"
    yaml_input.write_text("key: [unterminated", encoding="utf-8")

    json_process = run_redact(json_input, json_output)
    yaml_process = run_redact(yaml_input, yaml_output)

    assert json_process.returncode == 3
    assert yaml_process.returncode == 3
    assert not json_output.exists()
    assert not yaml_output.exists()


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("image.bin", b"\x00\x01binary"),
        ("document.pdf", b"%PDF-1.7 secret"),
        ("trace.zip", b"PK\x03\x04secret"),
    ],
)
def test_binary_pdf_and_zip_are_unsupported_without_output(
    tmp_path: Path, name: str, content: bytes
) -> None:
    input_path = tmp_path / name
    output_path = tmp_path / "out"
    input_path.write_bytes(content)

    process = run_redact(input_path, output_path)

    assert process.returncode == 3
    assert not output_path.exists()
    assert_no_secret(process.stdout, process.stderr)


def test_cli_text_output_and_json_output(tmp_path: Path) -> None:
    input_path = tmp_path / "input.txt"
    text_output = tmp_path / "text.out"
    json_output = tmp_path / "json.out"
    input_path.write_text(f"token={SECRET}", encoding="utf-8")

    text_process = run_redact(input_path, text_output, output_format="text")
    json_process = run_redact(input_path, json_output, output_format="json")

    assert text_process.returncode == 0
    assert "OK: Artifact redaction completed." in text_process.stdout
    assert json_process.returncode == 0
    assert json.loads(json_process.stdout)["ok"] is True
    assert_no_secret(
        text_process.stdout, json_process.stdout, text_output.read_text(), json_output.read_text()
    )


def test_cli_exit_codes_2_4_and_10(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_args = subprocess.run(
        [sys.executable, str(SCRIPT)], check=False, text=True, capture_output=True
    )
    assert missing_args.returncode == 2

    missing_input = run_redact(tmp_path / "missing.txt", tmp_path / "out.txt")
    assert missing_input.returncode == 4

    module = load_script_module()

    def boom(**_kwargs: Any) -> None:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(module, "redact_artifact", boom)
    exit_code = module.main(
        [
            "--input",
            str(tmp_path / "missing.txt"),
            "--output",
            str(tmp_path / "out.txt"),
            "--format-output",
            "json",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 10
    assert_no_secret(captured.out, captured.err, str(RuntimeError(SECRET)).replace(SECRET, ""))


def test_exception_string_from_redaction_error_is_safe() -> None:
    error = RedactionError("ARTIFACT_PARSE_FAILED", "input", SECRET)
    assert SECRET not in str(error)


def test_library_redaction_does_not_modify_input_file(tmp_path: Path) -> None:
    input_path = tmp_path / "input.txt"
    output_path = tmp_path / "output.txt"
    original = f"token={SECRET}"
    input_path.write_text(original, encoding="utf-8")

    report = redact_artifact(input_path, output_path)

    assert report.ok is True
    assert input_path.read_text(encoding="utf-8") == original
    assert_no_secret(output_path.read_text(encoding="utf-8"))

"""Secret redaction utilities for textual webapp-debug artifacts."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised through CLI dependency checks.
    yaml = None  # type: ignore[assignment]

REDACTION_PREFIX = "<REDACTED:"
REDACTION_SUFFIX = ">"
MAX_TEXT_BYTES = 20 * 1024 * 1024

SECRET_KEY_TYPES = {
    "password": "PASSWORD",
    "passwd": "PASSWORD",
    "secret": "SECRET",
    "token": "TOKEN",
    "authorization": "AUTHORIZATION",
    "cookie": "COOKIE",
    "setcookie": "COOKIE",
    "apikey": "API_KEY",
    "privatekey": "PRIVATE_KEY",
    "clientsecret": "CLIENT_SECRET",
    "dsn": "DSN",
}
SECRET_QUERY_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "private_key",
    "client_secret",
    "dsn",
}
UNSUPPORTED_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mp4",
    ".mov",
    ".webm",
    ".avi",
    ".pdf",
    ".zip",
    ".trace",
}
UNSUPPORTED_MAGIC = (b"%PDF", b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
SECRET_MARKERS = ("SECRET_MARKER", "BEGIN PRIVATE KEY", "PRIVATE KEY-----")


class RedactionError(RuntimeError):
    """Safe redaction failure with a reason code."""

    def __init__(self, code: str, path: str, reason: str) -> None:
        super().__init__(code)
        self.code = code
        self.path = path
        self.reason = reason


@dataclass
class RedactionReport:
    """Safe redaction report."""

    ok: bool
    format: str
    replacements: dict[str, int] = field(default_factory=dict)
    input: dict[str, str] = field(default_factory=dict)
    output: dict[str, str] = field(default_factory=dict)

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable safe report."""

        return {
            "ok": self.ok,
            "format": self.format,
            "replacements": dict(sorted(self.replacements.items())),
            "input": self.input,
            "output": self.output,
        }


@dataclass
class RedactionResult:
    """Redacted artifact bytes and safe report."""

    content: bytes
    report: RedactionReport


def redacted(kind: str) -> str:
    """Return a standard redaction marker."""

    return f"{REDACTION_PREFIX}{kind}{REDACTION_SUFFIX}"


def normalize_key(key: str) -> str:
    """Normalize key names for secret matching."""

    return re.sub(r"[^a-z0-9]", "", key.lower())


def secret_type_for_key(key: str) -> str | None:
    """Return the redaction type for a possibly-sensitive key."""

    normalized = normalize_key(key)
    for marker, kind in SECRET_KEY_TYPES.items():
        if marker in normalized:
            return kind
    return None


def is_redacted_value(value: Any) -> bool:
    """Return whether a value is already redacted."""

    if isinstance(value, str):
        return value.startswith(REDACTION_PREFIX) and value.endswith(REDACTION_SUFFIX)
    if isinstance(value, Mapping):
        return all(is_redacted_value(child) for child in value.values())
    if isinstance(value, list):
        return all(is_redacted_value(child) for child in value)
    return False


def redact_secret_value(value: Any, kind: str) -> Any:
    """Redact a value while preserving container shape where possible."""

    if isinstance(value, Mapping):
        return {key: redact_secret_value(child, kind) for key, child in value.items()}
    if isinstance(value, list):
        return [redact_secret_value(child, kind) for child in value]
    return redacted(kind)


def replace_env_secrets(text: str, env_values: Mapping[str, str], counts: Counter[str]) -> str:
    """Replace explicit environment secret values in text."""

    for value in env_values.values():
        if value:
            occurrences = text.count(value)
            if occurrences:
                text = text.replace(value, redacted("ENV"))
                counts["ENV"] += occurrences
    return text


def redact_url(value: str, counts: Counter[str]) -> str:
    """Redact URL userinfo and secret query parameters inside a string."""

    def replace_url(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        try:
            parsed = urlsplit(raw_url)
        except ValueError:
            return raw_url
        if not parsed.scheme or not parsed.netloc:
            return raw_url

        netloc = parsed.netloc
        if "@" in netloc:
            host = netloc.rsplit("@", 1)[1]
            netloc = f"{redacted('URL_USERINFO')}@{host}"
            counts["URL_USERINFO"] += 1

        query_items = []
        changed_query = False
        for key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
            if normalize_key(key) in {normalize_key(item) for item in SECRET_QUERY_KEYS}:
                query_items.append((key, redacted("QUERY_PARAM")))
                counts["QUERY_PARAM"] += 1
                changed_query = True
            else:
                query_items.append((key, item_value))
        query = urlencode(query_items, doseq=True) if changed_query else parsed.query
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))

    url_pattern = re.compile(r"https?://[^\s\"'<>]+")
    return url_pattern.sub(replace_url, value)


def redact_inline_text(value: str, counts: Counter[str], env_values: Mapping[str, str]) -> str:
    """Redact secrets embedded in text/log/header content."""

    value = replace_env_secrets(value, env_values, counts)

    def header_repl(match: re.Match[str]) -> str:
        key = match.group("key")
        kind = secret_type_for_key(key) or "SECRET"
        counts[kind] += 1
        return f"{match.group('prefix')}{redacted(kind)}"

    header_pattern = re.compile(
        r"(?im)^(?P<prefix>\s*(?P<key>authorization|cookie|set-cookie|x-api-key|api-key|token|password|passwd|secret|dsn)\s*:\s*)(?P<value>.+)$"
    )
    value = header_pattern.sub(header_repl, value)

    def key_value_repl(match: re.Match[str]) -> str:
        key = match.group("key")
        kind = secret_type_for_key(key) or "SECRET"
        counts[kind] += 1
        return f"{match.group('prefix')}{redacted(kind)}"

    key_pattern = re.compile(
        r"(?i)(?P<prefix>\b(?P<key>password|passwd|secret|token|authorization|cookie|set-cookie|api[-_]?key|private[-_]?key|client[-_]?secret|dsn)\b\s*[=:]\s*)(?P<value>[^\s,;]+)"
    )
    value = key_pattern.sub(key_value_repl, value)

    bearer_pattern = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
    bearer_count = len(bearer_pattern.findall(value))
    if bearer_count:
        value = bearer_pattern.sub(redacted("AUTHORIZATION"), value)
        counts["AUTHORIZATION"] += bearer_count

    basic_pattern = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=:_-]+")
    basic_count = len(basic_pattern.findall(value))
    if basic_count:
        value = basic_pattern.sub(redacted("AUTHORIZATION"), value)
        counts["AUTHORIZATION"] += basic_count

    query_pattern = re.compile(
        r"(?i)(?P<prefix>[?&](?:password|passwd|secret|token|access_token|refresh_token|api_key|apikey|private_key|client_secret|dsn)=)(?P<value>[^&\s\"']+)"
    )
    query_count = len(query_pattern.findall(value))
    if query_count:
        value = query_pattern.sub(
            lambda match: f"{match.group('prefix')}{redacted('QUERY_PARAM')}", value
        )
        counts["QUERY_PARAM"] += query_count

    return redact_url(value, counts)


def redact_data(value: Any, counts: Counter[str], env_values: Mapping[str, str]) -> Any:
    """Recursively redact structured JSON/YAML/HAR data."""

    if isinstance(value, Mapping):
        name = value.get("name")
        if isinstance(name, str):
            kind = secret_type_for_key(name)
            if kind is not None and "value" in value:
                updated = {
                    key: redact_data(child, counts, env_values) for key, child in value.items()
                }
                updated["value"] = redact_secret_value(value.get("value"), kind)
                counts[kind] += 1
                return updated

        redacted_mapping: dict[Any, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            kind = secret_type_for_key(key_text)
            if kind is not None:
                redacted_mapping[key] = redact_secret_value(child, kind)
                counts[kind] += 1
            else:
                redacted_mapping[key] = redact_data(child, counts, env_values)
        return redacted_mapping

    if isinstance(value, list):
        return [redact_data(child, counts, env_values) for child in value]

    if isinstance(value, str):
        return redact_inline_text(value, counts, env_values)

    return value


def detect_artifact_format(path: Path, requested_format: str) -> str:
    """Resolve artifact format from CLI request and path suffix."""

    if requested_format != "auto":
        return requested_format
    suffix = path.suffix.lower()
    if suffix == ".har":
        return "har"
    if suffix == ".json":
        return "json"
    if suffix == ".jsonl":
        return "jsonl"
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    return "text"


def read_text_artifact(path: Path) -> str:
    """Read a supported text artifact, rejecting binary content."""

    if path.suffix.lower() in UNSUPPORTED_EXTENSIONS:
        raise RedactionError("ARTIFACT_UNSUPPORTED", "input", "UNSUPPORTED_FORMAT")
    try:
        content = path.read_bytes()
    except OSError:
        raise RedactionError("ARTIFACT_READ_FAILED", "input", "READ_FAILED") from None
    if len(content) > MAX_TEXT_BYTES:
        raise RedactionError("ARTIFACT_UNSUPPORTED", "input", "TOO_LARGE")
    if content.startswith(UNSUPPORTED_MAGIC) or b"\x00" in content:
        raise RedactionError("ARTIFACT_UNSUPPORTED", "input", "BINARY_UNSUPPORTED")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise RedactionError("ARTIFACT_UNSUPPORTED", "input", "BINARY_UNSUPPORTED") from None


def output_bytes_for_format(value: Any, artifact_format: str) -> bytes:
    """Serialize redacted content for the selected format."""

    if artifact_format in {"json", "har"}:
        return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if artifact_format == "jsonl":
        return (
            "\n".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value
            ).encode("utf-8")
            + b"\n"
        )
    if artifact_format == "yaml":
        if yaml is None:
            raise RedactionError("DEPENDENCY_MISSING", "dependency", "PYYAML_MISSING")
        return yaml.safe_dump(value, allow_unicode=True, sort_keys=False).encode("utf-8")
    return str(value).encode("utf-8")


def parse_and_redact(
    path: Path,
    artifact_format: str,
    env_values: Mapping[str, str],
) -> RedactionResult:
    """Parse and redact an artifact in memory."""

    text = read_text_artifact(path)
    counts: Counter[str] = Counter()

    if artifact_format in {"json", "har"}:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            raise RedactionError("ARTIFACT_PARSE_FAILED", "input", "JSON_INVALID") from None
        redacted_value = redact_data(parsed, counts, env_values)
    elif artifact_format == "jsonl":
        redacted_lines: list[Any] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                redacted_lines.append(redact_data(json.loads(line), counts, env_values))
            except json.JSONDecodeError:
                raise RedactionError("ARTIFACT_PARSE_FAILED", "input", "JSONL_INVALID") from None
        redacted_value = redacted_lines
    elif artifact_format == "yaml":
        if yaml is None:
            raise RedactionError("DEPENDENCY_MISSING", "dependency", "PYYAML_MISSING")
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            raise RedactionError("ARTIFACT_PARSE_FAILED", "input", "YAML_INVALID") from None
        redacted_value = redact_data(parsed, counts, env_values)
    elif artifact_format == "text":
        redacted_value = redact_inline_text(text, counts, env_values)
    else:
        raise RedactionError("ARTIFACT_UNSUPPORTED", "format", "UNSUPPORTED_FORMAT")

    report = RedactionReport(
        ok=True,
        format=artifact_format,
        replacements=dict(counts),
        input={"suffix": path.suffix.lower() or "<none>"},
    )
    return RedactionResult(output_bytes_for_format(redacted_value, artifact_format), report)


def safe_real_path(path: Path, must_exist: bool) -> Path:
    """Resolve a path for symlink-safe comparison."""

    if must_exist:
        return path.resolve(strict=True)
    parent = path.parent.resolve(strict=True)
    return parent / path.name


def ensure_distinct_paths(
    input_path: Path, output_path: Path, report_path: Path | None = None
) -> None:
    """Reject in-place writes, including symlink aliases."""

    try:
        input_real = safe_real_path(input_path, must_exist=True)
        output_real = safe_real_path(output_path, must_exist=output_path.exists())
    except OSError:
        raise RedactionError("ARTIFACT_PATH_INVALID", "path", "PATH_INVALID") from None
    if input_real == output_real:
        raise RedactionError("ARTIFACT_PATH_BLOCKED", "output", "INPUT_OUTPUT_SAME")
    if report_path is not None:
        try:
            report_real = safe_real_path(report_path, must_exist=report_path.exists())
        except OSError:
            raise RedactionError("ARTIFACT_PATH_INVALID", "report", "PATH_INVALID") from None
        if report_real in {input_real, output_real}:
            raise RedactionError("ARTIFACT_PATH_BLOCKED", "report", "REPORT_PATH_CONFLICT")


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Write bytes via same-directory temp file and atomic replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temp_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except OSError:
        if temp_name is not None:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise RedactionError("ARTIFACT_WRITE_FAILED", "output", "WRITE_FAILED") from None


def redact_artifact(
    input_path: Path,
    output_path: Path,
    artifact_format: str = "auto",
    secret_env_names: Sequence[str] = (),
    force: bool = False,
    report_path: Path | None = None,
) -> RedactionReport:
    """Redact a supported artifact and atomically write output/report."""

    if not input_path.exists():
        raise RedactionError("ARTIFACT_READ_FAILED", "input", "READ_FAILED")
    ensure_distinct_paths(input_path, output_path, report_path)
    if output_path.exists() and not force:
        raise RedactionError("ARTIFACT_PATH_BLOCKED", "output", "OUTPUT_EXISTS")
    if report_path is not None and report_path.exists() and not force:
        raise RedactionError("ARTIFACT_PATH_BLOCKED", "report", "REPORT_EXISTS")

    resolved_format = detect_artifact_format(input_path, artifact_format)
    env_values = {
        name: value
        for name in secret_env_names
        if (value := os.environ.get(name)) is not None and value != ""
    }
    result = parse_and_redact(input_path, resolved_format, env_values)
    result.report.output = {"suffix": output_path.suffix.lower() or "<none>"}

    report_bytes: bytes | None = None
    if report_path is not None:
        report_bytes = (
            json.dumps(result.report.to_safe_dict(), ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

    atomic_write(output_path, result.content)
    if report_path is not None and report_bytes is not None:
        atomic_write(report_path, report_bytes)
    return result.report


def secret_findings(value: Any, path: str = "$") -> list[tuple[str, str]]:
    """Detect raw secret-like keys or values in a payload."""

    findings: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path != "$" else key_text
            if secret_type_for_key(key_text) is not None and not is_redacted_value(child):
                findings.append((child_path, "SECRET_KEY_UNREDACTED"))
            findings.extend(secret_findings(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(secret_findings(child, f"{path}.[{index}]"))
    elif isinstance(value, str):
        if any(marker in value for marker in SECRET_MARKERS):
            findings.append((path, "SECRET_MARKER_PRESENT"))
        if re.search(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=:-]+", value):
            findings.append((path, "AUTHORIZATION_PRESENT"))
        if "://" in value:
            parsed = urlsplit(value)
            if parsed.username or parsed.password:
                findings.append((path, "URL_USERINFO_PRESENT"))
            for key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
                if normalize_key(key) in {
                    normalize_key(item) for item in SECRET_QUERY_KEYS
                } and not is_redacted_value(item_value):
                    findings.append((path, "SECRET_QUERY_PARAM_PRESENT"))
    return findings

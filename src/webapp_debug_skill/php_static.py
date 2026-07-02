"""Small read-only PHP source scanners used by CakePHP discovery."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from webapp_debug_skill.redaction import secret_findings


@dataclass(frozen=True)
class SourceReadResult:
    """A safe source read result."""

    path: Path
    text: str | None
    warning: str | None = None


@dataclass(frozen=True)
class PhpMethod:
    """A PHP method declaration with a bounded body snippet."""

    name: str
    visibility: str
    line: int
    body: str


@dataclass(frozen=True)
class PhpClass:
    """A PHP class declaration."""

    name: str
    namespace: str
    line: int
    methods: tuple[PhpMethod, ...]


CLASS_RE = re.compile(
    r"(?m)^[ \t]*(?:abstract[ \t]+|final[ \t]+)?class[ \t]+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b"
)
NAMESPACE_RE = re.compile(r"(?m)^[ \t]*namespace[ \t]+(?P<name>[^;]+);")
METHOD_RE = re.compile(
    r"(?m)^[ \t]*(?P<visibility>public|protected|private)[ \t]+function[ \t]+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*\("
)
STRING_RE = re.compile(r"['\"]([^'\"]+)['\"]")


def safe_read_text(path: Path, *, max_bytes: int = 2_000_000) -> SourceReadResult:
    """Read UTF-8 source without surfacing raw content in errors."""

    try:
        if path.stat().st_size > max_bytes:
            return SourceReadResult(path=path, text=None, warning="FILE_TOO_LARGE")
        data = path.read_bytes()
    except OSError:
        return SourceReadResult(path=path, text=None, warning="READ_FAILED")
    if b"\x00" in data:
        return SourceReadResult(path=path, text=None, warning="BINARY_FILE")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return SourceReadResult(path=path, text=None, warning="INVALID_ENCODING")
    if secret_findings(text):
        return SourceReadResult(
            path=path, text=redact_secret_markers(text), warning="SECRET_REDACTED"
        )
    return SourceReadResult(path=path, text=text)


def redact_secret_markers(text: str) -> str:
    """Remove test secret marker values from source text before scanning."""

    return re.sub(r"SECRET_MARKER[A-Za-z0-9_:-]*", "<REDACTED:SECRET>", text)


def line_number(text: str, offset: int) -> int:
    """Return 1-based line number for a character offset."""

    return text.count("\n", 0, offset) + 1


def extract_php_classes(text: str) -> tuple[PhpClass, ...]:
    """Extract PHP classes and declared methods with lightweight brace matching."""

    namespace_match = NAMESPACE_RE.search(text)
    namespace = namespace_match.group("name").strip() if namespace_match else ""
    classes: list[PhpClass] = []
    class_matches = list(CLASS_RE.finditer(text))
    for index, class_match in enumerate(class_matches):
        class_name = class_match.group("name")
        class_start = class_match.end()
        next_class_start = (
            class_matches[index + 1].start() if index + 1 < len(class_matches) else len(text)
        )
        class_text = text[class_start:next_class_start]
        methods = extract_php_methods(class_text, base_offset=class_start, full_text=text)
        classes.append(
            PhpClass(
                name=class_name,
                namespace=namespace,
                line=line_number(text, class_match.start()),
                methods=tuple(methods),
            )
        )
    return tuple(classes)


def extract_php_methods(class_text: str, *, base_offset: int, full_text: str) -> list[PhpMethod]:
    """Extract declared methods from one class text."""

    methods: list[PhpMethod] = []
    for method_match in METHOD_RE.finditer(class_text):
        absolute_start = base_offset + method_match.start()
        brace_index = full_text.find("{", base_offset + method_match.end())
        if brace_index == -1:
            body = ""
        else:
            end_index = matching_brace(full_text, brace_index)
            body = full_text[brace_index + 1 : end_index] if end_index is not None else ""
        methods.append(
            PhpMethod(
                name=method_match.group("name"),
                visibility=method_match.group("visibility"),
                line=line_number(full_text, absolute_start),
                body=body,
            )
        )
    return methods


def matching_brace(text: str, open_index: int) -> int | None:
    """Return matching closing brace offset for a simple PHP block."""

    depth = 0
    index = open_index
    in_string: str | None = None
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
        elif char in {"'", '"'}:
            in_string = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def string_literals(text: str) -> tuple[str, ...]:
    """Return quoted string literals from a PHP snippet."""

    return tuple(match.group(1) for match in STRING_RE.finditer(text))


def contains_any(text: str, needles: Iterable[str]) -> bool:
    """Return whether any needle exists case-insensitively."""

    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)

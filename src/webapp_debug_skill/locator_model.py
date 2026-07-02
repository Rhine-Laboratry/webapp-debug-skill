"""Locator candidate model for Playwright skeleton generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from webapp_debug_skill.redaction import secret_findings


class LocatorModelError(RuntimeError):
    """Safe locator model validation error."""

    def __init__(self, code: str, path: str = "locator", reason: str = "INVALID") -> None:
        safe_code = "LOCATOR_MODEL_INVALID" if secret_findings(code) else code
        super().__init__(safe_code)
        self.code = safe_code
        self.path = "locator" if secret_findings(path) else path
        self.reason = "INVALID" if secret_findings(reason) else reason


class LocatorKind(str, Enum):
    """Supported locator candidate kinds in priority order."""

    ROLE = "ROLE"
    LABEL = "LABEL"
    PLACEHOLDER = "PLACEHOLDER"
    TEXT = "TEXT"
    TEST_ID = "TEST_ID"
    ID = "ID"
    NAME = "NAME"
    CSS = "CSS"
    XPATH = "XPATH"


class LocatorConfidence(str, Enum):
    """Locator confidence levels."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


LOCATOR_PRIORITY = {
    LocatorKind.ROLE: 10,
    LocatorKind.LABEL: 20,
    LocatorKind.PLACEHOLDER: 30,
    LocatorKind.TEXT: 40,
    LocatorKind.TEST_ID: 50,
    LocatorKind.ID: 60,
    LocatorKind.NAME: 70,
    LocatorKind.CSS: 80,
    LocatorKind.XPATH: 90,
}
LOW_CONFIDENCE_KINDS = {LocatorKind.CSS, LocatorKind.XPATH}


@dataclass(frozen=True)
class LocatorCandidate:
    """One locator candidate from structured hints or a manual override."""

    kind: LocatorKind
    value: str
    role: str = ""
    name: str = ""
    source: str = ""
    confidence: LocatorConfidence | None = None
    manual_override: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, LocatorKind):
            raise LocatorModelError("LOCATOR_KIND_INVALID", "locator.kind", "UNKNOWN")
        for field_name in ("value", "role", "name", "source"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise LocatorModelError(
                    "LOCATOR_FIELD_INVALID", f"locator.{field_name}", "STRING_REQUIRED"
                )
            if secret_findings(value):
                raise LocatorModelError(
                    "LOCATOR_SECRET_DETECTED", f"locator.{field_name}", "SECRET_DETECTED"
                )
        if self.kind == LocatorKind.ROLE:
            if not self.role.strip() or not (self.name or self.value).strip():
                raise LocatorModelError(
                    "LOCATOR_FIELD_INVALID", "locator.role", "ROLE_AND_NAME_REQUIRED"
                )
        elif not self.value.strip():
            raise LocatorModelError("LOCATOR_FIELD_INVALID", "locator.value", "EMPTY")

    @property
    def effective_confidence(self) -> LocatorConfidence:
        """Return explicit confidence or policy default."""

        if self.confidence is not None:
            return self.confidence
        if self.kind in LOW_CONFIDENCE_KINDS:
            return LocatorConfidence.LOW
        if self.kind == LocatorKind.TEXT:
            return LocatorConfidence.MEDIUM
        return LocatorConfidence.HIGH

    @property
    def locator_stability(self) -> str:
        """Return Sheets-compatible locator stability."""

        return self.effective_confidence.value

    @property
    def requires_review(self) -> bool:
        """Return whether this candidate should block runnable generation."""

        return self.effective_confidence != LocatorConfidence.HIGH

    def to_payload(self) -> dict[str, Any]:
        """Return safe JSON payload."""

        return {
            "kind": self.kind.value,
            "value": self.value,
            "role": self.role,
            "name": self.name,
            "source": self.source,
            "confidence": self.effective_confidence.value,
            "manual_override": self.manual_override,
        }


@dataclass(frozen=True)
class LocatorChoice:
    """Selected locator candidate and any non-selected alternatives."""

    candidate: LocatorCandidate
    alternatives: tuple[LocatorCandidate, ...] = ()

    @property
    def locator_stability(self) -> str:
        """Return selected locator stability."""

        return self.candidate.locator_stability

    @property
    def requires_review(self) -> bool:
        """Return whether selected locator requires review."""

        return self.candidate.requires_review

    def to_payload(self) -> dict[str, Any]:
        """Return safe JSON payload."""

        return {
            "selected": self.candidate.to_payload(),
            "alternatives": [candidate.to_payload() for candidate in self.alternatives],
        }


def parse_locator_candidates(value: str, *, path: str) -> tuple[LocatorCandidate, ...]:
    """Parse a structured locator target string.

    Plain strings intentionally produce no candidates. Phase 8C does not infer locators
    from prose-like labels; later phases may add richer static discovery hints.
    """

    if not isinstance(value, str) or not value.strip().startswith(("{", "[")):
        return ()
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        raise LocatorModelError("LOCATOR_JSON_INVALID", path, "JSON_INVALID") from None
    if isinstance(loaded, dict):
        loaded = [loaded]
    if not isinstance(loaded, list) or not loaded:
        raise LocatorModelError("LOCATOR_JSON_INVALID", path, "LIST_REQUIRED")
    candidates: list[LocatorCandidate] = []
    for index, item in enumerate(loaded):
        if not isinstance(item, dict):
            raise LocatorModelError("LOCATOR_JSON_INVALID", f"{path}.[{index}]", "OBJECT_REQUIRED")
        candidates.append(locator_candidate_from_mapping(item, path=f"{path}.[{index}]"))
    return tuple(candidates)


def locator_candidate_from_mapping(value: dict[str, Any], *, path: str) -> LocatorCandidate:
    """Build one locator candidate from a mapping."""

    kind = normalize_kind(value.get("kind"), f"{path}.kind")
    confidence = normalize_confidence(value.get("confidence"), f"{path}.confidence")
    manual_override = parse_bool(value.get("manual_override", False), f"{path}.manual_override")
    return LocatorCandidate(
        kind=kind,
        value=safe_text(value.get("value", ""), f"{path}.value"),
        role=safe_text(value.get("role", ""), f"{path}.role"),
        name=safe_text(value.get("name", ""), f"{path}.name"),
        source=safe_text(value.get("source", ""), f"{path}.source"),
        confidence=confidence,
        manual_override=manual_override,
    )


def choose_locator(candidates: tuple[LocatorCandidate, ...]) -> LocatorChoice | None:
    """Choose a candidate using manual override and documented locator priority."""

    if not candidates:
        return None
    manual = [candidate for candidate in candidates if candidate.manual_override]
    pool = manual or list(candidates)
    selected = sorted(
        pool,
        key=lambda candidate: (
            candidate.effective_confidence != LocatorConfidence.HIGH,
            LOCATOR_PRIORITY[candidate.kind],
        ),
    )[0]
    alternatives = tuple(candidate for candidate in candidates if candidate != selected)
    return LocatorChoice(selected, alternatives)


def normalize_kind(value: Any, path: str) -> LocatorKind:
    """Normalize locator kind."""

    if not isinstance(value, str):
        raise LocatorModelError("LOCATOR_KIND_INVALID", path, "STRING_REQUIRED")
    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return LocatorKind(normalized)
    except ValueError:
        raise LocatorModelError("LOCATOR_KIND_INVALID", path, "UNKNOWN") from None


def normalize_confidence(value: Any, path: str) -> LocatorConfidence | None:
    """Normalize optional confidence."""

    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise LocatorModelError("LOCATOR_CONFIDENCE_INVALID", path, "STRING_REQUIRED")
    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return LocatorConfidence(normalized)
    except ValueError:
        raise LocatorModelError("LOCATOR_CONFIDENCE_INVALID", path, "UNKNOWN") from None


def parse_bool(value: Any, path: str) -> bool:
    """Parse a bool for manual override."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no", ""}:
            return False
    raise LocatorModelError("LOCATOR_FIELD_INVALID", path, "BOOLEAN_REQUIRED")


def safe_text(value: Any, path: str) -> str:
    """Return safe text."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise LocatorModelError("LOCATOR_FIELD_INVALID", path, "STRING_REQUIRED")
    if secret_findings(value):
        raise LocatorModelError("LOCATOR_SECRET_DETECTED", path, "SECRET_DETECTED")
    return value.strip()

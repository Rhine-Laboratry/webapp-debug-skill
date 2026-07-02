"""Typed Feature / Story / Scenario contracts for later generation phases."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

from webapp_debug_skill.inventory_model import safe_text
from webapp_debug_skill.redaction import secret_findings
from webapp_debug_skill.risk import RiskLevel, normalize_risk_value
from webapp_debug_skill.wal import canonical_json

SCENARIO_MANUAL_COLUMNS = frozenset(
    {
        "review_status",
        "manual_override",
        "manual_expected_behavior",
        "manual_exclusion_reason",
        "manual_priority",
        "notes",
    }
)
SCENARIO_STRUCTURED_COLUMNS = (
    "inventory_ids",
    "structured_actions",
    "structured_assertions",
    "data_requirements",
    "expectation_source_refs",
)
ID_PATTERNS = {
    "feature_id": re.compile(r"^FEAT-\d{6}$"),
    "story_id": re.compile(r"^STORY-\d{6}$"),
    "scenario_id": re.compile(r"^SCN-\d{6}$"),
}
INVENTORY_ID_PATTERN = re.compile(r"^INV-[A-Za-z0-9_-]{1,120}$")
FORMULA_PREFIXES = ("=", "+", "-", "@")

EnumT = TypeVar("EnumT", bound=Enum)


class ScenarioModelError(RuntimeError):
    """Safe Scenario model validation error."""

    def __init__(self, code: str, path: str = "scenario", reason: str = "INVALID") -> None:
        safe_code = "SCENARIO_MODEL_INVALID" if secret_findings(code) else code
        super().__init__(safe_code)
        self.code = safe_code
        self.path = "scenario" if secret_findings(path) else path
        self.reason = "INVALID" if secret_findings(reason) else reason


class ExpectationStatus(str, Enum):
    """Source quality for expected behavior."""

    PROVISIONAL_CODE = "PROVISIONAL_CODE"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    CONFLICT_CODE_TEST = "CONFLICT_CODE_TEST"
    OBSERVED_ONLY = "OBSERVED_ONLY"


class ExpectationSourceType(str, Enum):
    """Where the expectation came from."""

    CODE = "CODE"
    TEST = "TEST"
    DOCUMENTATION = "DOCUMENTATION"
    OBSERVATION = "OBSERVATION"
    MANUAL = "MANUAL"


class ScenarioDepth(str, Enum):
    """Scenario depth levels."""

    B = "B"
    C = "C"
    D = "D"


class ScenarioTestScope(str, Enum):
    """How a Scenario should be tested."""

    E2E_PLAYWRIGHT = "E2E_PLAYWRIGHT"
    OTHER_TEST_REQUIRED = "OTHER_TEST_REQUIRED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    BLOCKED = "BLOCKED"


class ScenarioLifecycleStatus(str, Enum):
    """Lifecycle status for a Scenario row."""

    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    MERGED = "MERGED"
    BLOCKED = "BLOCKED"


class ScenarioGenerationStatus(str, Enum):
    """Generation status for a Scenario row."""

    PENDING = "PENDING"
    GENERATED = "GENERATED"
    BLOCKED = "BLOCKED"
    STALE = "STALE"


class ScenarioLatestTestStatus(str, Enum):
    """Latest execution status for a Scenario row."""

    NOT_RUN = "NOT_RUN"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    FLAKY = "FLAKY"


class ScenarioActionKind(str, Enum):
    """Structured action kinds supported by the model."""

    NAVIGATE = "NAVIGATE"
    CLICK = "CLICK"
    FILL = "FILL"
    SELECT = "SELECT"
    SUBMIT = "SUBMIT"
    UPLOAD = "UPLOAD"
    DOWNLOAD = "DOWNLOAD"
    WAIT = "WAIT"
    CUSTOM = "CUSTOM"


class ScenarioAssertionKind(str, Enum):
    """Structured assertion kinds supported by the model."""

    VISIBLE = "VISIBLE"
    TEXT = "TEXT"
    URL = "URL"
    STATE = "STATE"
    DOWNLOAD = "DOWNLOAD"
    EMAIL = "EMAIL"
    CUSTOM = "CUSTOM"


class DataRequirementKind(str, Enum):
    """Declarative data requirements; no DB or seed command is executed here."""

    NONE = "NONE"
    SEEDED_ACCOUNT = "SEEDED_ACCOUNT"
    TEST_RECORD = "TEST_RECORD"
    UPLOAD_FILE = "UPLOAD_FILE"
    MAILBOX = "MAILBOX"
    PERMISSION = "PERMISSION"


@dataclass(frozen=True)
class SourceReference:
    """Structured source reference for expected behavior provenance."""

    source_type: ExpectationSourceType
    path: str
    symbol: str = ""
    line_start: int | None = None
    line_end: int | None = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        require_enum_member(self.source_type, ExpectationSourceType, "source_type")
        required_text(self.path, "source_ref.path")
        optional_text(self.symbol, "source_ref.symbol")
        optional_text(self.fingerprint, "source_ref.fingerprint")
        if self.line_start is not None and self.line_start < 1:
            raise ScenarioModelError("SCENARIO_SOURCE_REF_INVALID", "line_start", "BELOW_MINIMUM")
        if self.line_end is not None and self.line_end < 1:
            raise ScenarioModelError("SCENARIO_SOURCE_REF_INVALID", "line_end", "BELOW_MINIMUM")
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ScenarioModelError(
                "SCENARIO_SOURCE_REF_INVALID", "line_end", "LINE_RANGE_INVALID"
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, path: str) -> "SourceReference":
        """Build and validate a source reference from a mapping."""

        line_start = optional_positive_int(value.get("line_start"), f"{path}.line_start")
        line_end = optional_positive_int(value.get("line_end"), f"{path}.line_end")
        if line_start is not None and line_end is not None and line_end < line_start:
            raise ScenarioModelError("SCENARIO_SOURCE_REF_INVALID", path, "LINE_RANGE_INVALID")
        return cls(
            source_type=normalize_enum(
                value.get("source_type"),
                ExpectationSourceType,
                path=f"{path}.source_type",
            ),
            path=required_text(value.get("path"), f"{path}.path"),
            symbol=optional_text(value.get("symbol", ""), f"{path}.symbol"),
            line_start=line_start,
            line_end=line_end,
            fingerprint=optional_text(value.get("fingerprint", ""), f"{path}.fingerprint"),
        )

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""

        return {
            "source_type": self.source_type.value,
            "path": self.path,
            "symbol": self.symbol,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class ScenarioAction:
    """One structured action that later generators may translate."""

    kind: ScenarioActionKind
    description: str
    target: str = ""
    value: str = ""

    def __post_init__(self) -> None:
        require_enum_member(self.kind, ScenarioActionKind, "structured_actions.kind")
        required_text(self.description, "structured_actions.description")
        optional_text(self.target, "structured_actions.target")
        optional_text(self.value, "structured_actions.value")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, path: str) -> "ScenarioAction":
        """Build and validate an action from a mapping."""

        return cls(
            kind=normalize_enum(value.get("kind"), ScenarioActionKind, path=f"{path}.kind"),
            description=required_text(value.get("description"), f"{path}.description"),
            target=optional_text(value.get("target", ""), f"{path}.target"),
            value=optional_text(value.get("value", ""), f"{path}.value"),
        )

    def to_payload(self) -> dict[str, str]:
        """Return a JSON-safe payload."""

        return {
            "kind": self.kind.value,
            "description": self.description,
            "target": self.target,
            "value": self.value,
        }


@dataclass(frozen=True)
class ScenarioAssertion:
    """One structured assertion that later generators may translate."""

    kind: ScenarioAssertionKind
    description: str
    target: str = ""
    expected: str = ""

    def __post_init__(self) -> None:
        require_enum_member(self.kind, ScenarioAssertionKind, "structured_assertions.kind")
        required_text(self.description, "structured_assertions.description")
        optional_text(self.target, "structured_assertions.target")
        optional_text(self.expected, "structured_assertions.expected")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, path: str) -> "ScenarioAssertion":
        """Build and validate an assertion from a mapping."""

        return cls(
            kind=normalize_enum(value.get("kind"), ScenarioAssertionKind, path=f"{path}.kind"),
            description=required_text(value.get("description"), f"{path}.description"),
            target=optional_text(value.get("target", ""), f"{path}.target"),
            expected=optional_text(value.get("expected", ""), f"{path}.expected"),
        )

    def to_payload(self) -> dict[str, str]:
        """Return a JSON-safe payload."""

        return {
            "kind": self.kind.value,
            "description": self.description,
            "target": self.target,
            "expected": self.expected,
        }


@dataclass(frozen=True)
class DataRequirement:
    """Declarative data precondition for a Scenario."""

    kind: DataRequirementKind
    description: str
    ownership: str = "test_run_id"

    def __post_init__(self) -> None:
        require_enum_member(self.kind, DataRequirementKind, "data_requirements.kind")
        required_text(self.description, "data_requirements.description")
        required_text(self.ownership, "data_requirements.ownership")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, path: str) -> "DataRequirement":
        """Build and validate a data requirement from a mapping."""

        return cls(
            kind=normalize_enum(value.get("kind"), DataRequirementKind, path=f"{path}.kind"),
            description=required_text(value.get("description"), f"{path}.description"),
            ownership=optional_text(value.get("ownership", "test_run_id"), f"{path}.ownership"),
        )

    def to_payload(self) -> dict[str, str]:
        """Return a JSON-safe payload."""

        return {
            "kind": self.kind.value,
            "description": self.description,
            "ownership": self.ownership,
        }


@dataclass(frozen=True)
class ScenarioManualFields:
    """Human-editable Scenario fields used only to derive effective behavior."""

    review_status: str = ""
    manual_override: bool = False
    manual_expected_behavior: tuple[str, ...] = ()
    manual_exclusion_reason: str = ""
    manual_priority: RiskLevel | None = None
    notes: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ScenarioManualFields":
        """Parse human-editable fields without making them auto-writable."""

        return cls(
            review_status=optional_text(row.get("review_status", ""), "review_status"),
            manual_override=parse_bool(row.get("manual_override", False), "manual_override"),
            manual_expected_behavior=parse_optional_multiline(
                row.get("manual_expected_behavior", ""),
                "manual_expected_behavior",
            ),
            manual_exclusion_reason=optional_text(
                row.get("manual_exclusion_reason", ""),
                "manual_exclusion_reason",
            ),
            manual_priority=normalize_optional_risk(row.get("manual_priority"), "manual_priority"),
            notes=optional_text(row.get("notes", ""), "notes"),
        )


@dataclass(frozen=True)
class ScenarioContract:
    """A typed Scenario row independent of Sheets or Playwright SDK types."""

    scenario_id: str
    scenario_version: int
    feature_id: str
    feature_name: str
    story_id: str
    story_title: str
    scenario_title: str
    actor: str
    inventory_ids: tuple[str, ...]
    preconditions: tuple[str, ...]
    actions: tuple[ScenarioAction, ...]
    assertions: tuple[ScenarioAssertion, ...]
    data_requirements: tuple[DataRequirement, ...]
    expectation_status: ExpectationStatus
    expectation_source_type: ExpectationSourceType
    source_refs: tuple[SourceReference, ...]
    conflict_detected: bool
    test_scope: ScenarioTestScope
    scenario_depth: ScenarioDepth
    risk: RiskLevel
    priority: RiskLevel
    route_or_url: str
    source_fingerprint: str
    lifecycle_status: ScenarioLifecycleStatus = ScenarioLifecycleStatus.ACTIVE
    generation_status: ScenarioGenerationStatus = ScenarioGenerationStatus.PENDING
    latest_test_status: ScenarioLatestTestStatus = ScenarioLatestTestStatus.NOT_RUN

    def __post_init__(self) -> None:
        validate_id("scenario_id", self.scenario_id)
        validate_id("feature_id", self.feature_id)
        validate_id("story_id", self.story_id)
        if self.scenario_version < 1:
            raise ScenarioModelError(
                "SCENARIO_VERSION_INVALID", "scenario_version", "BELOW_MINIMUM"
            )
        if not self.inventory_ids:
            raise ScenarioModelError("SCENARIO_INVENTORY_MAPPING_INVALID", "inventory_ids", "EMPTY")
        for index, inventory_id in enumerate(self.inventory_ids):
            validate_inventory_id(inventory_id, f"inventory_ids.[{index}]")
        for field_name in (
            "feature_name",
            "story_title",
            "scenario_title",
            "actor",
            "source_fingerprint",
        ):
            required_text(getattr(self, field_name), field_name)
        if not self.preconditions:
            raise ScenarioModelError("SCENARIO_PRECONDITION_INVALID", "preconditions", "EMPTY")
        if not self.actions:
            raise ScenarioModelError("SCENARIO_ACTION_INVALID", "structured_actions", "EMPTY")
        if not self.assertions:
            raise ScenarioModelError("SCENARIO_ASSERTION_INVALID", "structured_assertions", "EMPTY")
        ensure_safe_sequence(self.preconditions, "preconditions")
        if not self.source_refs:
            raise ScenarioModelError(
                "SCENARIO_SOURCE_REF_INVALID", "expectation_source_refs", "EMPTY"
            )
        if len(set(self.inventory_ids)) != len(self.inventory_ids):
            raise ScenarioModelError(
                "SCENARIO_INVENTORY_MAPPING_INVALID",
                "inventory_ids",
                "DUPLICATE",
            )

    def to_sheet_row(self) -> dict[str, Any]:
        """Return a Sheets-row mapping without human-editable columns."""

        row = {
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "feature_id": self.feature_id,
            "feature_name": self.feature_name,
            "story_id": self.story_id,
            "story_title": self.story_title,
            "scenario_title": self.scenario_title,
            "actor": self.actor,
            "inventory_ids": ",".join(self.inventory_ids),
            "preconditions": "\n".join(self.preconditions),
            "actions": "\n".join(action.description for action in self.actions),
            "structured_actions": serialize_structured(self.actions),
            "expected_results": "\n".join(assertion.description for assertion in self.assertions),
            "structured_assertions": serialize_structured(self.assertions),
            "data_requirements": serialize_structured(self.data_requirements),
            "expectation_status": self.expectation_status.value,
            "expectation_source_type": self.expectation_source_type.value,
            "expectation_source_refs": serialize_structured(self.source_refs),
            "conflict_detected": self.conflict_detected,
            "conflict_details": "",
            "test_scope": self.test_scope.value,
            "scenario_depth": self.scenario_depth.value,
            "risk": self.risk.value,
            "priority": self.priority.value,
            "route_or_url": self.route_or_url,
            "source_fingerprint": self.source_fingerprint,
            "lifecycle_status": self.lifecycle_status.value,
            "generation_status": self.generation_status.value,
            "generated_test_file": "",
            "generated_test_name": "",
            "locator_stability": "",
            "latest_test_status": self.latest_test_status.value,
            "latest_run_id": "",
            "last_tested_at": "",
        }
        ensure_no_manual_field_update(row)
        if secret_findings(row):
            raise ScenarioModelError("SCENARIO_SECRET_DETECTED", "scenario", "SECRET_DETECTED")
        return row

    @classmethod
    def from_sheet_row(cls, row: Mapping[str, Any]) -> "ScenarioContract":
        """Parse a ScenarioContract from a row mapping."""

        actions = parse_structured_items(
            row.get("structured_actions"),
            ScenarioAction.from_mapping,
            "structured_actions",
        )
        assertions = parse_structured_items(
            row.get("structured_assertions"),
            ScenarioAssertion.from_mapping,
            "structured_assertions",
        )
        data_requirements = parse_structured_items(
            row.get("data_requirements"),
            DataRequirement.from_mapping,
            "data_requirements",
        )
        return cls(
            scenario_id=required_text(row.get("scenario_id"), "scenario_id"),
            scenario_version=positive_int(row.get("scenario_version"), "scenario_version"),
            feature_id=required_text(row.get("feature_id"), "feature_id"),
            feature_name=required_text(row.get("feature_name"), "feature_name"),
            story_id=required_text(row.get("story_id"), "story_id"),
            story_title=required_text(row.get("story_title"), "story_title"),
            scenario_title=required_text(row.get("scenario_title"), "scenario_title"),
            actor=required_text(row.get("actor"), "actor"),
            inventory_ids=parse_string_list(row.get("inventory_ids"), "inventory_ids"),
            preconditions=parse_multiline(row.get("preconditions"), "preconditions"),
            actions=actions,
            assertions=assertions,
            data_requirements=data_requirements,
            expectation_status=normalize_enum(
                row.get("expectation_status"),
                ExpectationStatus,
                path="expectation_status",
            ),
            expectation_source_type=normalize_enum(
                row.get("expectation_source_type"),
                ExpectationSourceType,
                path="expectation_source_type",
            ),
            source_refs=parse_structured_items(
                row.get("expectation_source_refs"),
                SourceReference.from_mapping,
                "expectation_source_refs",
            ),
            conflict_detected=parse_bool(row.get("conflict_detected"), "conflict_detected"),
            test_scope=normalize_enum(row.get("test_scope"), ScenarioTestScope, path="test_scope"),
            scenario_depth=normalize_enum(
                row.get("scenario_depth"),
                ScenarioDepth,
                path="scenario_depth",
            ),
            risk=normalize_risk(row.get("risk"), "risk"),
            priority=normalize_risk(row.get("priority"), "priority"),
            route_or_url=optional_text(row.get("route_or_url", ""), "route_or_url"),
            source_fingerprint=required_text(row.get("source_fingerprint"), "source_fingerprint"),
            lifecycle_status=normalize_enum(
                row.get("lifecycle_status"),
                ScenarioLifecycleStatus,
                path="lifecycle_status",
            ),
            generation_status=normalize_enum(
                row.get("generation_status"),
                ScenarioGenerationStatus,
                path="generation_status",
            ),
            latest_test_status=normalize_enum(
                row.get("latest_test_status"),
                ScenarioLatestTestStatus,
                path="latest_test_status",
            ),
        )


def scenario_from_inventory_row(
    row: Mapping[str, Any],
    *,
    feature_id: str,
    story_id: str,
    scenario_id: str,
    scenario_version: int = 1,
) -> ScenarioContract:
    """Build a minimal typed Scenario contract from one Inventory row."""

    inventory_id = required_text(row.get("inventory_id"), "inventory.inventory_id")
    feature_name = first_text(
        row,
        ("feature_area", "feature_name", "name"),
        path="inventory.feature_name",
    )
    scenario_name = first_text(
        row, ("name", "feature_name", "route_or_trigger"), path="inventory.name"
    )
    actor = first_actor(row)
    route = optional_text(
        row.get("route_or_trigger") or row.get("route_or_url") or "",
        "inventory.route_or_trigger",
    )
    risk = normalize_risk(row.get("risk") or "LOW", "inventory.risk")
    depth = depth_for_risk(risk)
    action = ScenarioAction(
        ScenarioActionKind.NAVIGATE if route else ScenarioActionKind.CUSTOM,
        f"{scenario_name}を開く" if route else f"{scenario_name}を実行する",
        target=route,
    )
    assertion = ScenarioAssertion(
        ScenarioAssertionKind.VISIBLE,
        f"{scenario_name}の主要な結果を確認する",
    )
    data_requirement = DataRequirement(
        DataRequirementKind.TEST_RECORD,
        "このtest_run_idが所有するテストデータを使用する",
    )
    return ScenarioContract(
        scenario_id=scenario_id,
        scenario_version=scenario_version,
        feature_id=feature_id,
        feature_name=feature_name,
        story_id=story_id,
        story_title=f"{feature_name}を利用できる",
        scenario_title=f"{scenario_name}を確認する",
        actor=actor,
        inventory_ids=(inventory_id,),
        preconditions=(f"{actor}としてログインしている",),
        actions=(action,),
        assertions=(assertion,),
        data_requirements=(data_requirement,),
        expectation_status=ExpectationStatus.PROVISIONAL_CODE,
        expectation_source_type=ExpectationSourceType.CODE,
        source_refs=(
            SourceReference(
                source_type=ExpectationSourceType.CODE,
                path=required_text(row.get("source_path"), "inventory.source_path"),
                symbol=optional_text(row.get("source_symbol", ""), "inventory.source_symbol"),
                line_start=source_line(row.get("source_lines")),
                line_end=source_line(row.get("source_lines")),
                fingerprint=required_text(
                    row.get("source_fingerprint"),
                    "inventory.source_fingerprint",
                ),
            ),
        ),
        conflict_detected=False,
        test_scope=normalize_test_scope(row.get("test_scope")),
        scenario_depth=depth,
        risk=risk,
        priority=risk,
        route_or_url=route,
        source_fingerprint=required_text(
            row.get("source_fingerprint"),
            "inventory.source_fingerprint",
        ),
    )


def normalize_enum(value: Any, enum_type: type[EnumT], *, path: str) -> EnumT:
    """Normalize enum values case-insensitively with safe errors."""

    if not isinstance(value, str):
        raise ScenarioModelError("SCENARIO_ENUM_INVALID", path, "STRING_REQUIRED")
    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    if normalized == "":
        raise ScenarioModelError("SCENARIO_ENUM_INVALID", path, "EMPTY")
    try:
        return enum_type(normalized)
    except ValueError:
        raise ScenarioModelError("SCENARIO_ENUM_INVALID", path, "UNKNOWN") from None


def require_enum_member(value: Any, enum_type: type[Enum], path: str) -> None:
    """Require an already-normalized enum member."""

    if not isinstance(value, enum_type):
        raise ScenarioModelError("SCENARIO_ENUM_INVALID", path, "UNKNOWN")


def required_text(value: Any, path: str) -> str:
    """Return a safe non-empty string."""

    text = optional_text(value, path).strip()
    if text == "":
        raise ScenarioModelError("SCENARIO_FIELD_INVALID", path, "EMPTY")
    return text


def optional_text(value: Any, path: str) -> str:
    """Return safe single-line or multiline text."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise ScenarioModelError("SCENARIO_FIELD_INVALID", path, "STRING_REQUIRED")
    if secret_findings(value):
        raise ScenarioModelError("SCENARIO_SECRET_DETECTED", path, "SECRET_DETECTED")
    if formula_like(value):
        raise ScenarioModelError("SCENARIO_FORMULA_REJECTED", path, "FORMULA_LIKE_VALUE")
    return safe_text(value)


def formula_like(value: str) -> bool:
    """Return whether a value may be interpreted as a spreadsheet formula."""

    stripped = value.lstrip(" \t\r\n\v\f\u0000")
    return stripped != "" and stripped[0] in FORMULA_PREFIXES


def validate_id(path: str, value: str) -> None:
    """Validate stable Feature/Story/Scenario IDs."""

    if not ID_PATTERNS[path].fullmatch(value):
        raise ScenarioModelError("SCENARIO_ID_INVALID", path, "INVALID_FORMAT")


def validate_inventory_id(value: str, path: str) -> None:
    """Validate Inventory ID references."""

    if not INVENTORY_ID_PATTERN.fullmatch(value):
        raise ScenarioModelError("SCENARIO_INVENTORY_MAPPING_INVALID", path, "INVALID_FORMAT")


def positive_int(value: Any, path: str) -> int:
    """Parse a positive integer."""

    if isinstance(value, bool):
        raise ScenarioModelError("SCENARIO_FIELD_INVALID", path, "INTEGER_REQUIRED")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isdecimal():
        parsed = int(value)
    else:
        raise ScenarioModelError("SCENARIO_FIELD_INVALID", path, "INTEGER_REQUIRED")
    if parsed < 1:
        raise ScenarioModelError("SCENARIO_FIELD_INVALID", path, "BELOW_MINIMUM")
    return parsed


def optional_positive_int(value: Any, path: str) -> int | None:
    """Parse an optional positive integer."""

    if value in {None, ""}:
        return None
    return positive_int(value, path)


def parse_bool(value: Any, path: str) -> bool:
    """Parse a bool from Sheets-compatible values."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no", ""}:
            return False
    raise ScenarioModelError("SCENARIO_FIELD_INVALID", path, "BOOLEAN_REQUIRED")


def parse_multiline(value: Any, path: str) -> tuple[str, ...]:
    """Parse a newline-delimited text cell."""

    if value is None or not isinstance(value, str):
        raise ScenarioModelError("SCENARIO_FIELD_INVALID", path, "STRING_REQUIRED")
    items = tuple(
        required_text(part, f"{path}.[{index}]")
        for index, part in enumerate(value.splitlines())
        if part.strip()
    )
    if not items:
        raise ScenarioModelError("SCENARIO_FIELD_INVALID", path, "EMPTY")
    return items


def parse_optional_multiline(value: Any, path: str) -> tuple[str, ...]:
    """Parse optional newline-delimited text."""

    if value in {None, ""}:
        return ()
    return parse_multiline(value, path)


def parse_string_list(value: Any, path: str) -> tuple[str, ...]:
    """Parse a string list from JSON array, sequence, or comma-delimited cell."""

    if isinstance(value, str) and value.strip().startswith("["):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            raise ScenarioModelError("SCENARIO_FIELD_INVALID", path, "JSON_INVALID") from None
        value = loaded
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        items = [required_text(item, f"{path}.[{index}]") for index, item in enumerate(value)]
    else:
        raise ScenarioModelError("SCENARIO_FIELD_INVALID", path, "LIST_REQUIRED")
    if not items:
        raise ScenarioModelError("SCENARIO_FIELD_INVALID", path, "EMPTY")
    return tuple(items)


def parse_structured_items(
    value: Any,
    factory: Callable[..., Any],
    path: str,
) -> tuple[Any, ...]:
    """Parse a JSON array of structured Scenario objects."""

    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            raise ScenarioModelError(
                "SCENARIO_STRUCTURED_FIELD_INVALID",
                path,
                "JSON_INVALID",
            ) from None
    else:
        loaded = value
    if not isinstance(loaded, list):
        raise ScenarioModelError("SCENARIO_STRUCTURED_FIELD_INVALID", path, "LIST_REQUIRED")
    parsed = []
    for index, item in enumerate(loaded):
        if not isinstance(item, Mapping):
            raise ScenarioModelError(
                "SCENARIO_STRUCTURED_FIELD_INVALID",
                f"{path}.[{index}]",
                "OBJECT_REQUIRED",
            )
        parsed.append(factory(item, path=f"{path}.[{index}]"))
    if not parsed:
        raise ScenarioModelError("SCENARIO_STRUCTURED_FIELD_INVALID", path, "EMPTY")
    return tuple(parsed)


def serialize_structured(items: Sequence[Any]) -> str:
    """Serialize structured Scenario objects deterministically."""

    return canonical_json([item.to_payload() for item in items])


def ensure_no_manual_field_update(values: Mapping[str, Any]) -> None:
    """Reject automatic updates to human-editable Scenario columns."""

    touched = sorted(set(values) & SCENARIO_MANUAL_COLUMNS)
    if touched:
        raise ScenarioModelError("SCENARIO_MANUAL_FIELD_PROTECTED", touched[0], "HUMAN_EDITABLE")


def automatic_scenario_columns(headers: Sequence[str]) -> tuple[str, ...]:
    """Return Scenario columns that may be automatically written."""

    return tuple(header for header in headers if header not in SCENARIO_MANUAL_COLUMNS)


def ensure_safe_sequence(values: Sequence[str], path: str) -> None:
    """Reject empty or secret-bearing sequence values."""

    if not values:
        raise ScenarioModelError("SCENARIO_FIELD_INVALID", path, "EMPTY")
    for index, value in enumerate(values):
        required_text(value, f"{path}.[{index}]")


def normalize_risk(value: Any, path: str) -> RiskLevel:
    """Normalize a required risk-like field."""

    risk = normalize_risk_value(value, path=path, strict=True)
    if risk is None:
        raise ScenarioModelError("SCENARIO_RISK_INVALID", path, "EMPTY")
    return risk


def normalize_optional_risk(value: Any, path: str) -> RiskLevel | None:
    """Normalize an optional risk-like field."""

    return normalize_risk_value(value, path=path, strict=False)


def effective_assertions(
    scenario: ScenarioContract,
    manual: ScenarioManualFields,
) -> tuple[ScenarioAssertion, ...]:
    """Return assertions used by later generators after manual override."""

    if not manual.manual_override or not manual.manual_expected_behavior:
        return scenario.assertions
    return tuple(
        ScenarioAssertion(
            kind=ScenarioAssertionKind.CUSTOM,
            description=line,
        )
        for line in manual.manual_expected_behavior
    )


def effective_priority(
    scenario: ScenarioContract,
    manual: ScenarioManualFields,
) -> RiskLevel:
    """Return priority after human override fields are applied."""

    return manual.manual_priority or scenario.priority


def source_line(value: Any) -> int | None:
    """Parse the first source line from Inventory source_lines."""

    if value is None:
        return None
    text = str(value).split("-", 1)[0].split(",", 1)[0].strip()
    if not text.isdecimal():
        return None
    parsed = int(text)
    return parsed if parsed > 0 else None


def normalize_test_scope(value: Any) -> ScenarioTestScope:
    """Normalize Inventory test scope into Scenario test scope."""

    if value in {None, ""}:
        return ScenarioTestScope.E2E_PLAYWRIGHT
    try:
        return normalize_enum(value, ScenarioTestScope, path="test_scope")
    except ScenarioModelError:
        return ScenarioTestScope.OTHER_TEST_REQUIRED


def depth_for_risk(risk: RiskLevel) -> ScenarioDepth:
    """Return default Scenario depth for a risk level."""

    if risk == RiskLevel.CRITICAL:
        return ScenarioDepth.D
    if risk == RiskLevel.HIGH:
        return ScenarioDepth.C
    return ScenarioDepth.B


def first_text(row: Mapping[str, Any], keys: Sequence[str], *, path: str) -> str:
    """Return the first non-empty text field from a row."""

    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return required_text(value, f"{path}.{key}")
    raise ScenarioModelError("SCENARIO_FIELD_INVALID", path, "EMPTY")


def first_actor(row: Mapping[str, Any]) -> str:
    """Return a safe actor from Inventory row fields."""

    roles = row.get("actor_roles")
    if isinstance(roles, Sequence) and not isinstance(roles, str | bytes | bytearray) and roles:
        return required_text(roles[0], "inventory.actor_roles.[0]")
    actor = row.get("actor")
    if isinstance(actor, str) and actor.strip():
        return required_text(actor, "inventory.actor")
    return "user"

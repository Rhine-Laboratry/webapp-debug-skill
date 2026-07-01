"""Bounded discovery coverage gate evaluation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from webapp_debug_skill.redaction import secret_findings
from webapp_debug_skill.risk import RiskAssessment, RiskError, RiskLevel, assess_inventory_risk
from webapp_debug_skill.status_model import (
    InventoryStatus,
    StatusModelError,
    is_closed_status,
    is_excluded_status,
    is_open_gap_status,
    normalize_inventory_status,
)


class CoverageMode(str, Enum):
    """Coverage policy modes."""

    STRICT = "strict"
    RISK_GATED = "risk-gated"


class CoverageOutcome(str, Enum):
    """Coverage gate outcomes."""

    PASS = "PASS"
    PASS_WITH_GAPS = "PASS_WITH_GAPS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class CoverageReason(str, Enum):
    """Stable reason codes emitted by the coverage evaluator."""

    PASS = "COVERAGE_PASS"
    PASS_WITH_GAPS = "COVERAGE_PASS_WITH_GAPS"
    NO_INVENTORY = "COVERAGE_NO_INVENTORY"
    POLICY_INVALID = "COVERAGE_POLICY_INVALID"
    CLOSURE_BELOW_MINIMUM = "COVERAGE_CLOSURE_BELOW_MINIMUM"
    OPEN_GAPS_EXCEEDED = "COVERAGE_OPEN_GAPS_EXCEEDED"
    BLOCKING_RISK_GAPS = "COVERAGE_BLOCKING_RISK_GAPS"
    MAX_PASSES_REACHED = "COVERAGE_MAX_PASSES_REACHED"
    UNKNOWN_STATUS = "COVERAGE_UNKNOWN_STATUS"
    UNKNOWN_RISK = "COVERAGE_UNKNOWN_RISK"
    INVALID_ROW = "COVERAGE_INVALID_ROW"
    STRICT_INCOMPLETE = "COVERAGE_STRICT_INCOMPLETE"


class CoverageError(RuntimeError):
    """Safe coverage policy/load error."""

    def __init__(
        self,
        code: str,
        path: str = "coverage",
        reason: str = "INVALID",
    ) -> None:
        safe_code = CoverageReason.POLICY_INVALID.value if secret_findings(code) else code
        super().__init__(safe_code)
        self.code = safe_code
        self.path = "coverage" if secret_findings(path) else path
        self.reason = "INVALID" if secret_findings(reason) else reason


@dataclass(frozen=True)
class CoveragePolicy:
    """Coverage gate policy."""

    mode: CoverageMode = CoverageMode.STRICT
    max_discovery_passes: int = 3
    minimum_inventory_closure_percent: float = 100.0
    maximum_open_discovery_gaps: int = 0
    block_open_gap_risks: tuple[RiskLevel, ...] = (
        RiskLevel.CRITICAL,
        RiskLevel.HIGH,
        RiskLevel.MEDIUM,
        RiskLevel.LOW,
    )
    current_discovery_pass: int = 1

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        current_pass: int | None = None,
    ) -> "CoveragePolicy":
        """Build a policy from a full config mapping."""

        raw = config.get("coverage")
        if not isinstance(raw, Mapping):
            raise CoverageError(CoverageReason.POLICY_INVALID.value, "coverage", "REQUIRED")
        return cls.from_mapping(raw, current_pass=current_pass)

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        current_pass: int | None = None,
    ) -> "CoveragePolicy":
        """Build and validate a policy from a coverage mapping."""

        try:
            mode = CoverageMode(str(raw.get("mode", "strict")))
        except ValueError:
            raise CoverageError(CoverageReason.POLICY_INVALID.value, "coverage.mode", "INVALID")
        risks = raw.get("block_open_gap_risks", ())
        if not isinstance(risks, Sequence) or isinstance(risks, str):
            raise CoverageError(
                CoverageReason.POLICY_INVALID.value,
                "coverage.block_open_gap_risks",
                "LIST_REQUIRED",
            )
        try:
            block_risks = tuple(RiskLevel(str(value).strip().upper()) for value in risks)
        except ValueError:
            raise CoverageError(
                CoverageReason.POLICY_INVALID.value,
                "coverage.block_open_gap_risks",
                "INVALID",
            ) from None
        policy = cls(
            mode=mode,
            max_discovery_passes=int(raw.get("max_discovery_passes", 3)),
            minimum_inventory_closure_percent=float(
                raw.get("minimum_inventory_closure_percent", 100)
            ),
            maximum_open_discovery_gaps=int(raw.get("maximum_open_discovery_gaps", 0)),
            block_open_gap_risks=block_risks,
            current_discovery_pass=1 if current_pass is None else int(current_pass),
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        """Validate policy values."""

        if self.max_discovery_passes < 1:
            raise CoverageError(
                CoverageReason.POLICY_INVALID.value,
                "coverage.max_discovery_passes",
                "BELOW_MINIMUM",
            )
        if not 0 <= self.minimum_inventory_closure_percent <= 100:
            raise CoverageError(
                CoverageReason.POLICY_INVALID.value,
                "coverage.minimum_inventory_closure_percent",
                "OUT_OF_RANGE",
            )
        if self.maximum_open_discovery_gaps < 0:
            raise CoverageError(
                CoverageReason.POLICY_INVALID.value,
                "coverage.maximum_open_discovery_gaps",
                "BELOW_MINIMUM",
            )
        if self.current_discovery_pass < 1:
            raise CoverageError(
                CoverageReason.POLICY_INVALID.value,
                "coverage.current_discovery_pass",
                "BELOW_MINIMUM",
            )


@dataclass(frozen=True)
class TopOpenGap:
    """A safe summary of an open gap row."""

    inventory_id: str
    status: str
    risk: str
    inferred_risk: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "inventory_id": self.inventory_id,
            "status": self.status,
            "risk": self.risk,
            "inferred_risk": self.inferred_risk,
        }


@dataclass(frozen=True)
class RiskGapSummary:
    """Counts for a single risk level."""

    risk: RiskLevel
    total: int = 0
    closed: int = 0
    open_gaps: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "risk": self.risk.value,
            "total": self.total,
            "closed": self.closed,
            "open_gaps": self.open_gaps,
        }


@dataclass(frozen=True)
class InventoryCoverageMetrics:
    """Coverage counters and derived values."""

    inventory_total: int
    effective_total: int
    closed_count: int
    open_gap_count: int
    closure_percent: float
    gap_count_by_risk: dict[str, int]
    closed_count_by_risk: dict[str, int]
    total_count_by_risk: dict[str, int]
    blocking_gap_count: int
    max_discovery_passes: int
    current_discovery_pass: int
    transition_allowed: bool
    strict_completion: bool
    risk_gated_completion: bool
    outcome: CoverageOutcome
    reason_codes: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    top_open_gaps: tuple[TopOpenGap, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "inventory_total": self.inventory_total,
            "effective_total": self.effective_total,
            "closed_count": self.closed_count,
            "open_gap_count": self.open_gap_count,
            "closure_percent": self.closure_percent,
            "gap_count_by_risk": self.gap_count_by_risk,
            "closed_count_by_risk": self.closed_count_by_risk,
            "total_count_by_risk": self.total_count_by_risk,
            "blocking_gap_count": self.blocking_gap_count,
            "max_discovery_passes": self.max_discovery_passes,
            "current_discovery_pass": self.current_discovery_pass,
            "transition_allowed": self.transition_allowed,
            "strict_completion": self.strict_completion,
            "risk_gated_completion": self.risk_gated_completion,
            "outcome": self.outcome.value,
            "reason_codes": list(self.reason_codes),
            "warnings": list(self.warnings),
            "top_open_gaps": [gap.to_dict() for gap in self.top_open_gaps],
        }


@dataclass(frozen=True)
class CoverageReport:
    """Coverage evaluation result."""

    policy: CoveragePolicy
    metrics: InventoryCoverageMetrics

    @property
    def outcome(self) -> CoverageOutcome:
        """Return the gate outcome."""

        return self.metrics.outcome

    @property
    def transition_allowed(self) -> bool:
        """Return whether test phase may start."""

        return self.metrics.transition_allowed

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """Return stable reason codes."""

        return self.metrics.reason_codes

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON payload."""

        return {
            "mode": self.policy.mode.value,
            "policy": {
                "max_discovery_passes": self.policy.max_discovery_passes,
                "minimum_inventory_closure_percent": (
                    self.policy.minimum_inventory_closure_percent
                ),
                "maximum_open_discovery_gaps": self.policy.maximum_open_discovery_gaps,
                "block_open_gap_risks": [risk.value for risk in self.policy.block_open_gap_risks],
                "current_discovery_pass": self.policy.current_discovery_pass,
            },
            "metrics": self.metrics.to_dict(),
        }


@dataclass(frozen=True)
class _RowClassification:
    status: InventoryStatus
    risk: RiskAssessment
    row: Mapping[str, Any]
    index: int


def evaluate_inventory(
    rows: Sequence[Mapping[str, Any]],
    policy: CoveragePolicy,
) -> CoverageReport:
    """Evaluate bounded discovery coverage for inventory rows."""

    policy.validate()
    reason_codes: list[str] = []
    warnings: list[str] = []
    classifications: list[_RowClassification] = []

    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            return blocked_report(
                policy,
                len(rows),
                CoverageReason.INVALID_ROW.value,
                "row_not_mapping",
            )
        try:
            status = normalize_inventory_status(
                row.get("status"), path=f"Inventory.[{index}].status"
            )
            risk = assess_inventory_risk(row, path=f"Inventory.[{index}]")
        except StatusModelError:
            return blocked_report(
                policy,
                len(rows),
                CoverageReason.UNKNOWN_STATUS.value,
                "unknown_status",
            )
        except RiskError:
            return blocked_report(
                policy,
                len(rows),
                CoverageReason.UNKNOWN_RISK.value,
                "unknown_risk",
            )
        classifications.append(_RowClassification(status, risk, row, index))

    effective = [item for item in classifications if not is_excluded_status(item.status)]
    if not effective:
        return blocked_report(
            policy,
            len(rows),
            CoverageReason.NO_INVENTORY.value,
            "no_effective_inventory",
        )

    total_by_risk: Counter[str] = Counter()
    closed_by_risk: Counter[str] = Counter()
    gaps_by_risk: Counter[str] = Counter()
    top_gaps: list[TopOpenGap] = []
    closed_count = 0
    open_gap_count = 0
    blocking_gap_count = 0
    block_risks = {risk.value for risk in policy.block_open_gap_risks}

    for item in effective:
        risk_value = item.risk.level.value
        total_by_risk[risk_value] += 1
        if is_closed_status(item.status):
            closed_count += 1
            closed_by_risk[risk_value] += 1
        elif is_open_gap_status(item.status):
            open_gap_count += 1
            gaps_by_risk[risk_value] += 1
            if risk_value in block_risks:
                blocking_gap_count += 1
            if len(top_gaps) < 10:
                top_gaps.append(
                    TopOpenGap(
                        inventory_id=safe_inventory_id(item.row, item.index),
                        status=item.status.value,
                        risk=risk_value,
                        inferred_risk=item.risk.inferred,
                    )
                )
        else:
            warnings.append("STATUS_NOT_COUNTED")

    closure_percent = round((closed_count / len(effective)) * 100, 2)
    strict_completion = closed_count == len(effective) and open_gap_count == 0
    risk_gated_completion = False
    transition_allowed = False
    outcome = CoverageOutcome.FAIL

    if policy.mode == CoverageMode.STRICT:
        if strict_completion:
            outcome = CoverageOutcome.PASS
            transition_allowed = True
            add_reason(reason_codes, CoverageReason.PASS.value)
        else:
            add_reason(reason_codes, CoverageReason.STRICT_INCOMPLETE.value)
            if closure_percent < 100:
                add_reason(reason_codes, CoverageReason.CLOSURE_BELOW_MINIMUM.value)
            if open_gap_count > 0:
                add_reason(reason_codes, CoverageReason.OPEN_GAPS_EXCEEDED.value)
    else:
        risk_gated_completion = (
            closure_percent >= policy.minimum_inventory_closure_percent
            and open_gap_count <= policy.maximum_open_discovery_gaps
            and blocking_gap_count == 0
        )
        if risk_gated_completion and strict_completion:
            outcome = CoverageOutcome.PASS
            transition_allowed = True
            add_reason(reason_codes, CoverageReason.PASS.value)
        elif risk_gated_completion:
            outcome = CoverageOutcome.PASS_WITH_GAPS
            transition_allowed = True
            add_reason(reason_codes, CoverageReason.PASS_WITH_GAPS.value)
        else:
            if closure_percent < policy.minimum_inventory_closure_percent:
                add_reason(reason_codes, CoverageReason.CLOSURE_BELOW_MINIMUM.value)
            if open_gap_count > policy.maximum_open_discovery_gaps:
                add_reason(reason_codes, CoverageReason.OPEN_GAPS_EXCEEDED.value)
            if blocking_gap_count > 0:
                add_reason(reason_codes, CoverageReason.BLOCKING_RISK_GAPS.value)

    if not transition_allowed and policy.current_discovery_pass >= policy.max_discovery_passes:
        add_reason(reason_codes, CoverageReason.MAX_PASSES_REACHED.value)
    if policy.current_discovery_pass > policy.max_discovery_passes:
        outcome = CoverageOutcome.BLOCKED
        transition_allowed = False
        add_reason(reason_codes, CoverageReason.MAX_PASSES_REACHED.value)

    metrics = InventoryCoverageMetrics(
        inventory_total=len(rows),
        effective_total=len(effective),
        closed_count=closed_count,
        open_gap_count=open_gap_count,
        closure_percent=closure_percent,
        gap_count_by_risk=counts_dict(gaps_by_risk),
        closed_count_by_risk=counts_dict(closed_by_risk),
        total_count_by_risk=counts_dict(total_by_risk),
        blocking_gap_count=blocking_gap_count,
        max_discovery_passes=policy.max_discovery_passes,
        current_discovery_pass=policy.current_discovery_pass,
        transition_allowed=transition_allowed,
        strict_completion=strict_completion,
        risk_gated_completion=risk_gated_completion,
        outcome=outcome,
        reason_codes=tuple(reason_codes),
        warnings=tuple(warnings),
        top_open_gaps=tuple(top_gaps),
    )
    return CoverageReport(policy=policy, metrics=metrics)


def blocked_report(
    policy: CoveragePolicy,
    inventory_total: int,
    reason: str,
    warning: str,
) -> CoverageReport:
    """Build a blocked report without row payloads."""

    metrics = InventoryCoverageMetrics(
        inventory_total=inventory_total,
        effective_total=0,
        closed_count=0,
        open_gap_count=0,
        closure_percent=0.0,
        gap_count_by_risk=counts_dict(Counter()),
        closed_count_by_risk=counts_dict(Counter()),
        total_count_by_risk=counts_dict(Counter()),
        blocking_gap_count=0,
        max_discovery_passes=policy.max_discovery_passes,
        current_discovery_pass=policy.current_discovery_pass,
        transition_allowed=False,
        strict_completion=False,
        risk_gated_completion=False,
        outcome=CoverageOutcome.BLOCKED,
        reason_codes=(reason,),
        warnings=(warning,),
        top_open_gaps=(),
    )
    return CoverageReport(policy=policy, metrics=metrics)


def counts_dict(counter: Counter[str]) -> dict[str, int]:
    """Return counts for all risks in stable order."""

    return {risk.value: int(counter.get(risk.value, 0)) for risk in RiskLevel}


def add_reason(reasons: list[str], reason: str) -> None:
    """Append a reason code only once."""

    if reason not in reasons:
        reasons.append(reason)


def safe_inventory_id(row: Mapping[str, Any], index: int) -> str:
    """Return a safe identifier for top gap output."""

    value = row.get("inventory_id") or row.get("id") or f"row-{index + 1}"
    text = str(value)
    if secret_findings(text):
        return f"row-{index + 1}"
    return text

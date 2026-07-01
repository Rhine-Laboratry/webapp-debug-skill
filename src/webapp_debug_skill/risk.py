"""Risk normalization and lightweight inference for Inventory rows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from webapp_debug_skill.redaction import secret_findings


class RiskLevel(str, Enum):
    """Supported risk levels."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RiskError(RuntimeError):
    """Safe risk normalization error."""

    def __init__(
        self,
        code: str,
        path: str = "risk",
        reason: str = "INVALID",
    ) -> None:
        safe_code = "COVERAGE_UNKNOWN_RISK" if secret_findings(code) else code
        super().__init__(safe_code)
        self.code = safe_code
        self.path = "risk" if secret_findings(path) else path
        self.reason = "INVALID" if secret_findings(reason) else reason


@dataclass(frozen=True)
class RiskAssessment:
    """A normalized risk value with provenance."""

    level: RiskLevel
    inferred: bool
    source: str


RISK_ORDER = {
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}
CRITICAL_KEYWORDS = (
    "認証",
    "ログイン",
    "権限",
    "認可",
    "管理者",
    "個人情報",
    "決済",
    "auth",
    "login",
    "permission",
    "authorization",
    "admin",
    "personal information",
    "pii",
    "payment",
)
HIGH_KEYWORDS = (
    "ユーザー停止",
    "削除",
    "取消",
    "状態変更",
    "csv",
    "pdf",
    "ファイルアップロード",
    "アップロード",
    "メール送信",
    "外部連携",
    "api",
    "バッチ",
    "delete",
    "cancel",
    "status change",
    "upload",
    "mail",
    "email",
    "external",
    "batch",
)
MEDIUM_KEYWORDS = (
    "設定",
    "検索",
    "一覧",
    "setting",
    "search",
    "list",
)


def normalize_risk_value(
    value: Any,
    *,
    path: str = "risk",
    strict: bool = True,
) -> RiskLevel | None:
    """Normalize a risk value, returning None for empty non-strict values."""

    if value is None:
        return None
    if not isinstance(value, str):
        if strict:
            raise RiskError("COVERAGE_UNKNOWN_RISK", path, "STRING_REQUIRED")
        return None
    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    if normalized == "":
        return None
    try:
        return RiskLevel(normalized)
    except ValueError:
        if strict:
            raise RiskError("COVERAGE_UNKNOWN_RISK", path, "UNKNOWN")
        return None


def assess_inventory_risk(row: Mapping[str, Any], *, path: str = "row") -> RiskAssessment:
    """Assess risk from explicit fields or safe keyword heuristics."""

    manual = normalize_risk_value(
        row.get("manual_priority"),
        path=f"{path}.manual_priority",
        strict=False,
    )
    if manual is not None:
        return RiskAssessment(manual, inferred=False, source="manual_priority")

    explicit = normalize_risk_value(row.get("risk"), path=f"{path}.risk", strict=True)
    if explicit is not None:
        return RiskAssessment(explicit, inferred=False, source="risk")

    text = row_text(row)
    lowered = text.lower()
    if any(keyword.lower() in lowered for keyword in CRITICAL_KEYWORDS):
        return RiskAssessment(RiskLevel.CRITICAL, inferred=True, source="heuristic")
    if any(keyword.lower() in lowered for keyword in HIGH_KEYWORDS):
        return RiskAssessment(RiskLevel.HIGH, inferred=True, source="heuristic")
    if any(keyword.lower() in lowered for keyword in MEDIUM_KEYWORDS):
        return RiskAssessment(RiskLevel.MEDIUM, inferred=True, source="heuristic")
    return RiskAssessment(RiskLevel.LOW, inferred=True, source="default")


def row_text(row: Mapping[str, Any]) -> str:
    """Collect non-secret row text for heuristic classification."""

    parts: list[str] = []
    for key in (
        "feature_area",
        "feature_name",
        "route_or_url",
        "notes",
        "description",
        "title",
        "scenario",
    ):
        value = row.get(key)
        if isinstance(value, str) and not secret_findings(value):
            parts.append(value)
    return " ".join(parts)

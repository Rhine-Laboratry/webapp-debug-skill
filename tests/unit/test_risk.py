from __future__ import annotations

import pytest

from webapp_debug_skill.risk import RiskError, RiskLevel, assess_inventory_risk

SECRET = "SECRET_MARKER_RISK"


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)


def test_explicit_risk_and_manual_priority_precedence() -> None:
    explicit = assess_inventory_risk({"risk": "HIGH", "feature_name": "ordinary"})
    manual = assess_inventory_risk(
        {"risk": "LOW", "manual_priority": "CRITICAL", "feature_name": "ordinary"}
    )

    assert explicit.level is RiskLevel.HIGH
    assert explicit.inferred is False
    assert explicit.source == "risk"
    assert manual.level is RiskLevel.CRITICAL
    assert manual.source == "manual_priority"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("admin login", RiskLevel.CRITICAL),
        ("認証と権限", RiskLevel.CRITICAL),
        ("delete user", RiskLevel.HIGH),
        ("CSV export", RiskLevel.HIGH),
        ("PDF report", RiskLevel.HIGH),
        ("file upload", RiskLevel.HIGH),
        ("メール送信", RiskLevel.HIGH),
        ("API endpoint", RiskLevel.HIGH),
        ("検索画面", RiskLevel.MEDIUM),
    ],
)
def test_keyword_heuristics(text: str, expected: RiskLevel) -> None:
    result = assess_inventory_risk({"feature_name": text})

    assert result.level is expected
    assert result.inferred is True


def test_unknown_or_empty_risk_defaults_low_when_not_explicit() -> None:
    result = assess_inventory_risk({"feature_name": "plain page"})

    assert result.level is RiskLevel.LOW
    assert result.inferred is True
    assert result.source == "default"


def test_unknown_explicit_risk_is_safe_error() -> None:
    with pytest.raises(RiskError) as exc_info:
        assess_inventory_risk({"risk": f"bad-{SECRET}", "feature_name": "plain"})

    assert exc_info.value.code == "COVERAGE_UNKNOWN_RISK"
    assert_no_secret(exc_info.value, exc_info.value.path, exc_info.value.reason)

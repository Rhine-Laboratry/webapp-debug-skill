from __future__ import annotations

import json

import pytest

from webapp_debug_skill.locator_model import (
    LocatorCandidate,
    LocatorConfidence,
    LocatorKind,
    LocatorModelError,
    choose_locator,
    parse_locator_candidates,
)

SECRET = "SECRET_MARKER_LOCATOR_MODEL"


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)


def test_role_label_testid_priority_is_deterministic() -> None:
    candidates = (
        LocatorCandidate(LocatorKind.TEST_ID, "save-button"),
        LocatorCandidate(LocatorKind.LABEL, "保存"),
        LocatorCandidate(LocatorKind.ROLE, "保存", role="button", name="保存"),
    )

    choice = choose_locator(candidates)

    assert choice is not None
    assert choice.candidate.kind == LocatorKind.ROLE
    assert choice.locator_stability == "HIGH"
    assert not choice.requires_review


def test_low_confidence_candidate_requires_review() -> None:
    choice = choose_locator((LocatorCandidate(LocatorKind.CSS, ".content > div:nth-child(2)"),))

    assert choice is not None
    assert choice.candidate.effective_confidence == LocatorConfidence.LOW
    assert choice.requires_review


def test_manual_override_preserves_selection_without_inflating_confidence() -> None:
    choice = choose_locator(
        (
            LocatorCandidate(LocatorKind.ROLE, "保存", role="button", name="保存"),
            LocatorCandidate(LocatorKind.CSS, ".legacy-save", manual_override=True),
        )
    )

    assert choice is not None
    assert choice.candidate.kind == LocatorKind.CSS
    assert choice.candidate.manual_override is True
    assert choice.locator_stability == "LOW"
    assert choice.requires_review


def test_parse_structured_locator_candidates() -> None:
    payload = json.dumps(
        [
            {"kind": "test_id", "value": "save-button"},
            {"kind": "placeholder", "value": "検索"},
        ],
        ensure_ascii=False,
    )

    parsed = parse_locator_candidates(payload, path="structured_actions.target")

    assert [candidate.kind for candidate in parsed] == [
        LocatorKind.TEST_ID,
        LocatorKind.PLACEHOLDER,
    ]


def test_secret_marker_never_surfaces_in_exception() -> None:
    payload = json.dumps({"kind": "label", "value": SECRET}, ensure_ascii=False)

    with pytest.raises(LocatorModelError) as exc_info:
        parse_locator_candidates(payload, path="structured_actions.target")

    assert exc_info.value.code == "LOCATOR_SECRET_DETECTED"
    assert_no_secret(exc_info.value, exc_info.value.path, exc_info.value.reason)

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from webapp_debug_skill.risk import RiskLevel
from webapp_debug_skill.scenario_model import (
    DataRequirement,
    DataRequirementKind,
    ExpectationSourceType,
    ExpectationStatus,
    ScenarioAction,
    ScenarioActionKind,
    ScenarioAssertion,
    ScenarioAssertionKind,
    ScenarioContract,
    ScenarioDepth,
    ScenarioGenerationStatus,
    ScenarioLatestTestStatus,
    ScenarioLifecycleStatus,
    ScenarioManualFields,
    ScenarioModelError,
    ScenarioTestScope,
    SourceReference,
    automatic_scenario_columns,
    effective_assertions,
    effective_priority,
    ensure_no_manual_field_update,
    normalize_enum,
    scenario_from_inventory_row,
)

SECRET = "SECRET_MARKER_SCENARIO_MODEL"


def contract() -> ScenarioContract:
    return ScenarioContract(
        scenario_id="SCN-000001",
        scenario_version=1,
        feature_id="FEAT-000001",
        feature_name="ユーザー管理",
        story_id="STORY-000001",
        story_title="ユーザーを管理できる",
        scenario_title="ユーザー一覧を表示できる",
        actor="admin",
        inventory_ids=("INV-001",),
        preconditions=("adminとしてログインしている",),
        actions=(
            ScenarioAction(
                ScenarioActionKind.NAVIGATE,
                "ユーザー一覧を開く",
                target="/users",
            ),
        ),
        assertions=(
            ScenarioAssertion(
                ScenarioAssertionKind.VISIBLE,
                "ユーザー一覧が表示される",
                target="ユーザー一覧",
            ),
        ),
        data_requirements=(
            DataRequirement(
                DataRequirementKind.TEST_RECORD,
                "このtest_run_idが所有するユーザーが存在する",
            ),
        ),
        expectation_status=ExpectationStatus.PROVISIONAL_CODE,
        expectation_source_type=ExpectationSourceType.CODE,
        source_refs=(
            SourceReference(
                ExpectationSourceType.CODE,
                "src/Controller/UsersController.php",
                symbol="Users::index",
                line_start=12,
                line_end=12,
                fingerprint="sha256:source",
            ),
        ),
        conflict_detected=False,
        test_scope=ScenarioTestScope.E2E_PLAYWRIGHT,
        scenario_depth=ScenarioDepth.B,
        risk=RiskLevel.MEDIUM,
        priority=RiskLevel.MEDIUM,
        route_or_url="/users",
        source_fingerprint="sha256:source",
        lifecycle_status=ScenarioLifecycleStatus.ACTIVE,
        generation_status=ScenarioGenerationStatus.PENDING,
        latest_test_status=ScenarioLatestTestStatus.NOT_RUN,
    )


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)


def test_enum_normalization_and_invalid_enum_is_safe() -> None:
    assert normalize_enum("generated", ScenarioGenerationStatus, path="generation_status") == (
        ScenarioGenerationStatus.GENERATED
    )
    with pytest.raises(ScenarioModelError) as exc_info:
        normalize_enum(SECRET, ScenarioGenerationStatus, path="generation_status")

    assert exc_info.value.code == "SCENARIO_ENUM_INVALID"
    assert exc_info.value.reason == "UNKNOWN"
    assert_no_secret(exc_info.value, exc_info.value.path, exc_info.value.reason)


def test_contract_renders_structured_json_without_manual_fields() -> None:
    row = contract().to_sheet_row()
    actions = json.loads(row["structured_actions"])
    assertions = json.loads(row["structured_assertions"])
    data_requirements = json.loads(row["data_requirements"])
    source_refs = json.loads(row["expectation_source_refs"])

    assert row["inventory_ids"] == "INV-001"
    assert actions[0]["kind"] == "NAVIGATE"
    assert assertions[0]["kind"] == "VISIBLE"
    assert data_requirements[0]["ownership"] == "test_run_id"
    assert source_refs[0]["path"] == "src/Controller/UsersController.php"
    assert not set(row) & {
        "review_status",
        "manual_override",
        "manual_expected_behavior",
        "manual_exclusion_reason",
        "manual_priority",
        "notes",
    }


def test_roundtrip_from_sheet_row_and_many_to_many_inventory_mapping() -> None:
    row = contract().to_sheet_row()
    row["inventory_ids"] = "INV-001,INV-002"

    parsed = ScenarioContract.from_sheet_row(row)

    assert parsed.inventory_ids == ("INV-001", "INV-002")
    assert parsed.actions[0].kind == ScenarioActionKind.NAVIGATE
    assert parsed.assertions[0].kind == ScenarioAssertionKind.VISIBLE
    assert parsed.source_refs[0].line_start == 12


def test_invalid_id_duplicate_mapping_structured_json_and_formula_are_rejected() -> None:
    with pytest.raises(ScenarioModelError) as bad_id:
        replace(contract(), scenario_id="bad")
    with pytest.raises(ScenarioModelError) as duplicate:
        replace(contract(), inventory_ids=("INV-001", "INV-001"))
    row = contract().to_sheet_row()
    row["structured_actions"] = "{bad json"
    with pytest.raises(ScenarioModelError) as bad_json:
        ScenarioContract.from_sheet_row(row)
    with pytest.raises(ScenarioModelError) as formula:
        ScenarioAction(ScenarioActionKind.CLICK, "=cmd")

    assert bad_id.value.code == "SCENARIO_ID_INVALID"
    assert duplicate.value.reason == "DUPLICATE"
    assert bad_json.value.reason == "JSON_INVALID"
    assert formula.value.code == "SCENARIO_FORMULA_REJECTED"


def test_manual_fields_drive_effective_behavior_but_are_not_auto_written() -> None:
    manual = ScenarioManualFields.from_row(
        {
            "manual_override": "true",
            "manual_expected_behavior": "手動期待を優先する",
            "manual_priority": "HIGH",
            "notes": "human note",
        }
    )

    assertions = effective_assertions(contract(), manual)
    priority = effective_priority(contract(), manual)

    assert assertions[0].description == "手動期待を優先する"
    assert priority == RiskLevel.HIGH
    with pytest.raises(ScenarioModelError):
        ensure_no_manual_field_update({"notes": "auto note"})
    assert "notes" not in automatic_scenario_columns(("scenario_id", "notes"))


def test_scenario_from_inventory_row_maps_inventory_and_depth() -> None:
    scenario = scenario_from_inventory_row(
        {
            "inventory_id": "INV-TEMP-NEW",
            "feature_area": "Users",
            "name": "Users::index",
            "actor_roles": ["admin"],
            "route_or_trigger": "/users",
            "source_path": "src/Controller/UsersController.php",
            "source_symbol": "Users::index",
            "source_lines": "6",
            "source_fingerprint": "sha256:new",
            "test_scope": "E2E_PLAYWRIGHT",
            "risk": "HIGH",
        },
        feature_id="FEAT-000001",
        story_id="STORY-000001",
        scenario_id="SCN-000001",
    )

    assert scenario.inventory_ids == ("INV-TEMP-NEW",)
    assert scenario.actor == "admin"
    assert scenario.scenario_depth == ScenarioDepth.C
    assert scenario.source_refs[0].line_start == 6


def test_secret_marker_never_surfaces_in_exception_or_output() -> None:
    with pytest.raises(ScenarioModelError) as exc_info:
        ScenarioAction(ScenarioActionKind.CLICK, f"click token={SECRET}")

    assert_no_secret(exc_info.value, exc_info.value.path, exc_info.value.reason)

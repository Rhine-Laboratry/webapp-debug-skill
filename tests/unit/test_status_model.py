from __future__ import annotations

import pytest

from webapp_debug_skill.status_model import (
    InventoryStatus,
    StatusModelError,
    is_excluded_status,
    normalize_inventory_status,
)

SECRET = "SECRET_MARKER_STATUS"


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)


def test_valid_inventory_status_normalization() -> None:
    assert normalize_inventory_status("MAPPED") is InventoryStatus.MAPPED
    assert (
        normalize_inventory_status("excluded-with-reason") is InventoryStatus.EXCLUDED_WITH_REASON
    )
    assert normalize_inventory_status("discovery gap") is InventoryStatus.DISCOVERY_GAP


def test_invalid_empty_and_case_statuses_are_safe() -> None:
    assert normalize_inventory_status("mapped") is InventoryStatus.MAPPED
    with pytest.raises(StatusModelError) as empty:
        normalize_inventory_status("")
    with pytest.raises(StatusModelError) as invalid:
        normalize_inventory_status(f"bad-{SECRET}")

    assert empty.value.code == "COVERAGE_UNKNOWN_STATUS"
    assert invalid.value.reason == "UNKNOWN"
    assert_no_secret(empty.value, invalid.value, invalid.value.path, invalid.value.reason)


def test_retired_and_merged_are_excluded() -> None:
    assert is_excluded_status(normalize_inventory_status("RETIRED")) is True
    assert is_excluded_status(normalize_inventory_status("MERGED")) is True
    assert is_excluded_status(normalize_inventory_status("MAPPED")) is False

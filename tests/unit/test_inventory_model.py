from __future__ import annotations

from datetime import UTC, datetime

from webapp_debug_skill.coverage import CoveragePolicy, evaluate_inventory
from webapp_debug_skill.inventory_model import (
    DiscoveryGap,
    InventoryCandidate,
    InventorySnapshotBuilder,
    dumps_snapshot,
    rfc3339_utc,
)


def test_inventory_id_is_deterministic_and_rows_are_coverage_compatible() -> None:
    candidate = InventoryCandidate(
        source_key="controller|Users|index",
        feature_area="Users",
        name="Users::index",
        item_type="UI_PAGE",
        actor="user",
        route_or_url="/users",
        source_path="src/Controller/UsersController.php",
        source_symbol="Users::index",
        source_line=12,
        discovery_source="cakephp_controller",
        notes=("safe note",),
    )
    same = InventoryCandidate(
        source_key="controller|Users|index",
        feature_area="Users",
        name="Users::index",
        item_type="UI_PAGE",
        actor="user",
        route_or_url="/users",
        source_path="src/Controller/UsersController.php",
        source_symbol="Users::index",
        source_line=12,
        discovery_source="cakephp_controller",
    )

    row = candidate.to_row(generated_at="2026-01-01T00:00:00Z")
    assert candidate.inventory_id == same.inventory_id
    assert row["status"] == "DISCOVERED"
    assert row["discovery_status"] == "DISCOVERED"
    assert row["source_code_reference"] == "src/Controller/UsersController.php:12"
    report = evaluate_inventory([row], CoveragePolicy())
    assert report.metrics.inventory_total == 1


def test_snapshot_builder_dedupes_and_includes_discovery_gaps() -> None:
    generated_at = rfc3339_utc(datetime(2026, 1, 1, tzinfo=UTC))
    builder = InventorySnapshotBuilder(
        generated_at=generated_at,
        source={"kind": "cakephp_static_discovery", "root": ".", "cakephp_version": "4"},
    )
    candidate = InventoryCandidate(
        source_key="controller|Users|index",
        feature_area="Users",
        name="Users::index",
        item_type="UI_PAGE",
        actor="user",
        route_or_url="/users",
        source_path="src/Controller/UsersController.php",
        source_symbol="Users::index",
        source_line=12,
        discovery_source="cakephp_controller",
    )
    builder.add_candidate(candidate)
    builder.add_candidate(candidate)
    builder.add_gap(
        DiscoveryGap(
            "DISCOVERY_DYNAMIC_ROUTE",
            "config/routes.php",
            3,
            "Dynamic route",
        )
    )

    payload = builder.payload(summary={"files_scanned": 2})

    assert payload["snapshot_schema_version"] == 1
    assert payload["summary"]["inventory_count"] == 2
    assert payload["summary"]["discovery_gaps"] == 1
    assert len(payload["Inventory"]) == 2
    assert any(row["status"] == "DISCOVERY_GAP" for row in payload["Inventory"])
    assert b"src/Controller/UsersController.php" in dumps_snapshot(payload)

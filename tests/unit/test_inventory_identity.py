from __future__ import annotations

from webapp_debug_skill.inventory_identity import (
    fingerprint_payload,
    match_existing_rows,
    operation_id,
    stable_source_fingerprint,
)

SECRET = "SECRET_MARKER_IDENTITY"


def row(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "inventory_id": "INV-1",
        "source_key": "controller|Users|index",
        "source_fingerprint": "sha256:abc",
        "discovery_source": "cakephp_controller",
        "source_code_reference": "src/Controller/UsersController.php:6",
        "feature_name": "Users::index",
        "route_or_url": "/users",
        "actor": "user",
    }
    base.update(kwargs)
    return base


def test_match_priority_source_fingerprint_source_key_route_and_inventory_id() -> None:
    snapshot = [
        row(
            inventory_id="INV-fp",
            source_key="fingerprint-only",
            source_fingerprint="sha256:abc",
            route_or_url="/anchor",
        ),
        row(
            inventory_id="INV-key",
            source_fingerprint="sha256:other",
            feature_name="Key::index",
            route_or_url="/key",
        ),
        row(
            inventory_id="INV-route",
            source_key="other",
            source_fingerprint="sha256:x",
            source_code_reference="src/Controller/OtherController.php:6",
        ),
        row(
            source_key="z",
            source_fingerprint="sha256:y",
            source_code_reference="src/Controller/IdController.php:6",
            feature_name="Id::index",
            route_or_url="/id",
        ),
    ]

    priority, indexes, _ = match_existing_rows(row(), snapshot)
    assert priority == "source_fingerprint"
    assert indexes == [0]

    priority, indexes, _ = match_existing_rows(row(source_fingerprint=""), snapshot)
    assert priority == "source_key"
    assert indexes == [1]

    priority, indexes, _ = match_existing_rows(
        row(source_fingerprint="", source_key="", inventory_id="none"), snapshot
    )
    assert priority == "source_anchor"
    assert indexes == [0]

    route_only = row(
        source_fingerprint="",
        source_key="",
        discovery_source="",
        source_code_reference="",
        inventory_id="none",
    )
    priority, indexes, _ = match_existing_rows(route_only, snapshot)
    assert priority == "route_actor_feature"
    assert indexes == [2]

    id_only = row(
        source_fingerprint="",
        source_key="",
        discovery_source="",
        source_code_reference="",
        route_or_url="",
        actor="",
    )
    priority, indexes, _ = match_existing_rows(id_only, snapshot)
    assert priority == "inventory_id"
    assert indexes == [3]


def test_ambiguous_match_deterministic_ids_and_no_secret() -> None:
    snapshot = [row(inventory_id="INV-1"), row(inventory_id="INV-2")]
    priority, indexes, match_key = match_existing_rows(row(), snapshot)
    op_a = operation_id("APPEND_INVENTORY", "match", {"x": 1})
    op_b = operation_id("APPEND_INVENTORY", "match", {"x": 1})

    assert priority == "source_fingerprint"
    assert indexes == [0, 1]
    assert op_a == op_b
    assert op_a.startswith("SYNC-")
    assert "/Users/" not in op_a
    assert SECRET not in match_key


def test_stable_source_fingerprint_is_safe_and_deterministic() -> None:
    value = row(source_fingerprint="", source_key=f"controller|{SECRET}")

    first = stable_source_fingerprint(value)
    second = stable_source_fingerprint(value)

    assert first == second
    assert first.startswith("sha256:")
    assert SECRET not in first
    assert fingerprint_payload({"b": 1, "a": 2}) == fingerprint_payload({"a": 2, "b": 1})

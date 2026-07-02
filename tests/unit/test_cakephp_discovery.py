from __future__ import annotations

from pathlib import Path

import pytest

from webapp_debug_skill.cakephp_discovery import (
    CakePHPDiscoveryError,
    cakephp_major_from_constraint,
    detect_cakephp_version,
    discover_cakephp,
    parse_controller_file,
    parse_routes_file,
    parse_template_file,
)

FIXTURES = Path("tests/fixtures/cakephp_apps")


def app(name: str) -> Path:
    return FIXTURES / name


def test_version_detection_from_composer_and_structure(tmp_path: Path) -> None:
    assert detect_cakephp_version(app("cakephp4_basic")).mode == "4"
    assert cakephp_major_from_constraint("^5.0 || ^4.4") == "5"
    assert cakephp_major_from_constraint("~3.10") == "3"
    assert detect_cakephp_version(app("cakephp2_generic")).mode == "2"
    assert detect_cakephp_version(tmp_path).mode == "generic"

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "composer.json").write_text("{not-json", encoding="utf-8")
    detected = detect_cakephp_version(broken)
    assert detected.mode == "generic"
    assert detected.warning == "COMPOSER_JSON_INVALID"


def test_route_discovery_simple_prefix_plugin_and_dynamic_gap() -> None:
    basic_routes, basic_gaps = parse_routes_file(
        "config/routes.php",
        (app("cakephp4_basic") / "config/routes.php").read_text(encoding="utf-8"),
    )
    assert {route.path for route in basic_routes} >= {"/users", "/users/add"}
    assert basic_routes[0].controller == "Users"
    assert basic_routes[0].action == "index"
    assert basic_routes[0].line > 0
    assert any(gap.reason_code == "DISCOVERY_DYNAMIC_ROUTE" for gap in basic_gaps)

    scoped_routes, _ = parse_routes_file(
        "config/routes.php",
        """<?php
$routes->scope('/admin', function ($builder) {
    $builder->connect('/users', ['controller' => 'Users', 'action' => 'index']);
});
""",
    )
    assert scoped_routes[0].path == "/admin/users"

    admin_routes, _ = parse_routes_file(
        "config/routes.php",
        (app("cakephp4_admin") / "config/routes.php").read_text(encoding="utf-8"),
    )
    assert all(route.prefix == "Admin" for route in admin_routes)

    plugin_result = discover_cakephp(app("cakephp4_plugin"), include_plugins=True)
    assert any(route.plugin == "Reports" for route in plugin_result.routes)

    dynamic_result = discover_cakephp(app("cakephp_dynamic_routes"))
    assert any(gap.reason_code == "DISCOVERY_DYNAMIC_ROUTE" for gap in dynamic_result.gaps)


def test_controller_discovery_filters_methods_and_extracts_hints() -> None:
    relative = "src/Controller/UsersController.php"
    actions, gaps = parse_controller_file(
        relative,
        (app("cakephp4_basic") / relative).read_text(encoding="utf-8"),
    )
    by_name = {action.action: action for action in actions}

    assert gaps == []
    assert set(by_name) == {"index", "add"}
    assert by_name["add"].http_methods == ("GET", "POST")
    assert {"redirect", "flash_message"}.issubset(by_name["add"].hints)
    assert "pagination" in by_name["index"].hints
    assert by_name["index"].source_path == relative
    assert by_name["index"].line > 0

    admin_actions, _ = parse_controller_file(
        "src/Controller/Admin/UsersController.php",
        (app("cakephp4_admin") / "src/Controller/Admin/UsersController.php").read_text(
            encoding="utf-8"
        ),
    )
    assert all(action.prefix == "Admin" for action in admin_actions)


def test_template_discovery_hints() -> None:
    add = parse_template_file(
        "templates/Users/add.php",
        (app("cakephp4_basic") / "templates/Users/add.php").read_text(encoding="utf-8"),
    )
    admin = parse_template_file(
        "templates/Admin/Users/index.php",
        (app("cakephp4_admin") / "templates/Admin/Users/index.php").read_text(encoding="utf-8"),
    )
    legacy = parse_template_file(
        "app/View/Users/index.ctp",
        (app("cakephp2_generic") / "app/View/Users/index.ctp").read_text(encoding="utf-8"),
    )

    assert add.controller == "Users"
    assert add.action == "add"
    assert "form" in add.hints
    assert "post_link" in admin.hints
    assert "download" in admin.hints
    assert "link" in legacy.hints

    upload = parse_template_file(
        "templates/Users/upload.php",
        "<?php echo $this->Form->create($user, ['type' => 'file']);",
    )
    assert "file_upload" in upload.hints


def test_discovery_payload_inventory_and_coverage_compatibility() -> None:
    result = discover_cakephp(app("cakephp4_admin"))
    payload = result.to_payload()
    rows = payload["Inventory"]

    assert payload["source"]["cakephp_version"] == "4"
    assert payload["summary"]["inventory_count"] == len(rows)
    assert result.source_paths
    assert any(row["actor"] == "admin" for row in rows)
    assert any(row["risk"] == "HIGH" for row in rows)
    assert all(row["status"] in {"DISCOVERED", "DISCOVERY_GAP"} for row in rows)
    assert all(row["status"] not in {"MAPPED", "RETIRED"} for row in rows)
    assert all(row["source_key"] for row in rows)
    assert all(not row["source_path"].startswith("/") for row in rows)
    assert all(row["inventory_id"].startswith("INV-TEMP-") for row in rows)

    second_payload = discover_cakephp(app("cakephp4_admin")).to_payload()
    assert [row["inventory_id"] for row in rows] == [
        row["inventory_id"] for row in second_payload["Inventory"]
    ]


def test_non_cakephp_is_blocked_safely() -> None:
    with pytest.raises(CakePHPDiscoveryError) as exc_info:
        discover_cakephp(app("non_cakephp"))

    assert exc_info.value.code == "DISCOVERY_NO_CAKEPHP_APP"

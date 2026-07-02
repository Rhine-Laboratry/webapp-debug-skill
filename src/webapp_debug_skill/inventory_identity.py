"""Deterministic Inventory identity and matching helpers."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from webapp_debug_skill.inventory_model import safe_text


MATCH_PRIORITIES = (
    "source_fingerprint",
    "source_key",
    "source_anchor",
    "route_actor_feature",
    "inventory_id",
)


@dataclass(frozen=True)
class InventoryIdentity:
    """Normalized row identity used for sync planning."""

    row_index: int
    inventory_id: str
    source_fingerprint: str
    source_key: str
    source_anchor: str
    route_actor_feature: str


def canonical_json(value: Any) -> str:
    """Return deterministic JSON used for fingerprints."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(value: Any) -> str:
    """Return sha256 hex for a canonical JSON-compatible value."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def operation_id(operation: str, match_key: str, payload: Mapping[str, Any] | None = None) -> str:
    """Return a deterministic sync operation ID."""

    digest = sha256_hex(
        {
            "operation": operation,
            "match_key": match_key,
            "payload": dict(payload or {}),
        }
    )[:16].upper()
    return f"SYNC-{digest}"


def normalize_text(value: Any) -> str:
    """Normalize a value for conservative equality matching."""

    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = ",".join(str(item) for item in value)
    text = safe_text(str(value))
    return re.sub(r"\s+", " ", text).strip().lower()


def row_value(row: Mapping[str, Any], *keys: str) -> str:
    """Return the first non-empty scalar value for possible aliases."""

    for key in keys:
        value = row.get(key)
        if isinstance(value, list):
            value = ",".join(str(item) for item in value)
        if value is not None and str(value).strip() != "":
            return safe_text(str(value))
    return ""


def row_status(row: Mapping[str, Any]) -> str:
    """Return status/discovery_status alias."""

    return row_value(row, "status", "discovery_status")


def row_feature_name(row: Mapping[str, Any]) -> str:
    """Return feature display name alias."""

    return row_value(row, "feature_name", "name")


def row_route(row: Mapping[str, Any]) -> str:
    """Return route/trigger alias."""

    return row_value(row, "route_or_url", "route_or_trigger")


def row_actor(row: Mapping[str, Any]) -> str:
    """Return actor alias from actor or actor_roles."""

    actor = row.get("actor")
    if actor is not None and str(actor).strip() != "":
        return safe_text(str(actor))
    roles = row.get("actor_roles")
    if isinstance(roles, list) and roles:
        return safe_text(str(roles[0]))
    if roles is not None and str(roles).strip() != "":
        return safe_text(str(roles).split(",", 1)[0])
    return ""


def stable_source_fingerprint(row: Mapping[str, Any]) -> str:
    """Return an existing or derived stable source fingerprint."""

    explicit = row_value(row, "source_fingerprint")
    if explicit:
        return explicit
    source_key = row_value(row, "source_key")
    source_reference = row_value(row, "source_code_reference")
    source_path = row_value(row, "source_path")
    source_symbol = row_value(row, "source_symbol")
    route = row_route(row)
    feature_name = row_feature_name(row)
    if not any((source_key, source_reference, source_path, source_symbol, route, feature_name)):
        return ""
    digest = sha256_hex(
        {
            "source_key": source_key,
            "source_code_reference": source_reference,
            "source_path": source_path,
            "source_symbol": source_symbol,
            "route": route,
            "feature_name": feature_name,
        }
    )
    return f"sha256:{digest}"


def inventory_identity(row: Mapping[str, Any], row_index: int) -> InventoryIdentity:
    """Build normalized identity for a row."""

    source = row_value(row, "discovery_source")
    reference = row_value(row, "source_code_reference")
    feature = row_feature_name(row)
    route = row_route(row)
    actor = row_actor(row)
    return InventoryIdentity(
        row_index=row_index,
        inventory_id=normalize_text(row_value(row, "inventory_id")),
        source_fingerprint=normalize_text(stable_source_fingerprint(row)),
        source_key=normalize_text(row_value(row, "source_key")),
        source_anchor="|".join(normalize_text(item) for item in (source, reference, feature)),
        route_actor_feature="|".join(normalize_text(item) for item in (route, actor, feature)),
    )


def duplicate_values(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, list[int]]:
    """Return duplicate normalized values for one identity field."""

    values: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        if field == "source_fingerprint":
            value = normalize_text(stable_source_fingerprint(row))
        else:
            value = normalize_text(row_value(row, field))
        if value:
            values.setdefault(value, []).append(index)
    return {value: indexes for value, indexes in values.items() if len(indexes) > 1}


def match_existing_rows(
    discovery: Mapping[str, Any],
    snapshot_rows: Sequence[Mapping[str, Any]],
) -> tuple[str | None, list[int], str]:
    """Return match priority, matching indexes, and safe match key."""

    identity = inventory_identity(discovery, -1)
    snapshot_identities = [
        inventory_identity(row, index) for index, row in enumerate(snapshot_rows)
    ]
    for priority in MATCH_PRIORITIES:
        value = getattr(identity, priority)
        if not value:
            continue
        indexes = [
            item.row_index for item in snapshot_identities if getattr(item, priority) == value
        ]
        if indexes:
            return priority, indexes, f"{priority}:{value}"
    return None, [], f"new:{identity.source_fingerprint or identity.inventory_id}"


def fingerprint_payload(value: Any) -> str:
    """Return a stable sha256 fingerprint for a JSON-compatible payload."""

    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def count_duplicate_summary(rows: Sequence[Mapping[str, Any]], field: str) -> int:
    """Return duplicate group count for tests and summaries."""

    return len(duplicate_values(rows, field))


def count_values(values: Sequence[str]) -> Counter[str]:
    """Return normalized value counts."""

    return Counter(value for value in (normalize_text(item) for item in values) if value)

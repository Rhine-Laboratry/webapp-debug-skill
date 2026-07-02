"""Inventory snapshot model for static discovery output."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from webapp_debug_skill.redaction import redact_inline_text, secret_findings

SNAPSHOT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class InventoryCandidate:
    """A normalized Inventory candidate."""

    source_key: str
    feature_area: str
    name: str
    item_type: str
    actor: str
    route_or_url: str
    source_path: str
    source_symbol: str
    source_line: int
    discovery_source: str
    confidence: str = "MEDIUM"
    risk: str = "MEDIUM"
    http_methods: tuple[str, ...] = ("GET",)
    notes: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()

    @property
    def inventory_id(self) -> str:
        """Return deterministic temporary Inventory ID."""

        digest = hashlib.sha256(self.source_key.encode("utf-8")).hexdigest()[:16].upper()
        return f"INV-TEMP-{digest}"

    def to_row(self, *, generated_at: str, commit: str = "UNKNOWN") -> dict[str, Any]:
        """Return a JSON row compatible with coverage and future Sheets sync."""

        safe_notes = safe_join([*self.notes, *self.hints])
        status = "DISCOVERED"
        fingerprint = hashlib.sha256(
            f"{self.source_key}|{self.route_or_url}|{self.source_symbol}".encode("utf-8")
        ).hexdigest()
        return {
            "inventory_id": self.inventory_id,
            "feature_area": safe_text(self.feature_area),
            "feature_name": safe_text(self.name),
            "item_type": self.item_type,
            "name": safe_text(self.name),
            "actor": self.actor,
            "actor_roles": [self.actor],
            "route_or_url": safe_text(self.route_or_url),
            "route_or_trigger": safe_text(self.route_or_url),
            "http_methods": list(self.http_methods),
            "source_code_reference": f"{self.source_path}:{self.source_line}",
            "source_path": self.source_path,
            "source_symbol": safe_text(self.source_symbol),
            "source_lines": str(self.source_line),
            "source_fingerprint": f"sha256:{fingerprint}",
            "test_scope": "E2E_PLAYWRIGHT",
            "recommended_test_type": "playwright",
            "discovery_status": status,
            "status": status,
            "exclusion_reason": "",
            "reachability": "CODE_ONLY",
            "risk": self.risk,
            "mapped_scenario_ids": [],
            "discovered_at": generated_at,
            "last_seen_commit": commit,
            "last_seen_at": generated_at,
            "discovery_source": self.discovery_source,
            "confidence": self.confidence,
            "notes": safe_notes,
        }


@dataclass(frozen=True)
class DiscoveryGap:
    """A static discovery gap."""

    reason_code: str
    source_path: str
    source_line: int
    summary: str
    confidence: str = "LOW"

    @property
    def inventory_id(self) -> str:
        """Return deterministic gap ID."""

        key = f"{self.reason_code}|{self.source_path}|{self.source_line}|{self.summary}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16].upper()
        return f"INV-TEMP-{digest}"

    def to_inventory_row(self, *, generated_at: str, commit: str = "UNKNOWN") -> dict[str, Any]:
        """Represent a gap as an open Inventory row."""

        fingerprint = hashlib.sha256(
            f"{self.reason_code}|{self.source_path}|{self.source_line}".encode("utf-8")
        ).hexdigest()
        return {
            "inventory_id": self.inventory_id,
            "feature_area": "Discovery Gap",
            "feature_name": self.reason_code,
            "item_type": "UI_PAGE",
            "name": self.reason_code,
            "actor": "unknown",
            "actor_roles": ["unknown"],
            "route_or_url": f"unresolved:{self.reason_code}",
            "route_or_trigger": f"unresolved:{self.reason_code}",
            "http_methods": [],
            "source_code_reference": f"{self.source_path}:{self.source_line}",
            "source_path": self.source_path,
            "source_symbol": self.reason_code,
            "source_lines": str(self.source_line),
            "source_fingerprint": f"sha256:{fingerprint}",
            "test_scope": "NOT_TESTABLE_WITH_CURRENT_ACCESS",
            "recommended_test_type": "manual-review",
            "discovery_status": "DISCOVERY_GAP",
            "status": "DISCOVERY_GAP",
            "exclusion_reason": "",
            "reachability": "CODE_ONLY",
            "risk": "MEDIUM",
            "mapped_scenario_ids": [],
            "discovered_at": generated_at,
            "last_seen_commit": commit,
            "last_seen_at": generated_at,
            "discovery_source": "cakephp_static_gap",
            "confidence": self.confidence,
            "notes": safe_text(self.summary),
        }

    def to_gap_dict(self) -> dict[str, Any]:
        """Return a safe gap record."""

        return {
            "inventory_id": self.inventory_id,
            "reason_code": self.reason_code,
            "source_code_reference": f"{self.source_path}:{self.source_line}",
            "summary": safe_text(self.summary),
            "confidence": self.confidence,
        }


@dataclass
class InventorySnapshotBuilder:
    """Build deterministic Inventory snapshot payloads."""

    generated_at: str
    source: Mapping[str, Any]
    commit: str = "UNKNOWN"
    candidates: list[InventoryCandidate] = field(default_factory=list)
    gaps: list[DiscoveryGap] = field(default_factory=list)

    def add_candidate(self, candidate: InventoryCandidate) -> None:
        """Add a candidate."""

        self.candidates.append(candidate)

    def add_gap(self, gap: DiscoveryGap) -> None:
        """Add a gap."""

        self.gaps.append(gap)

    def payload(self, *, summary: Mapping[str, Any]) -> dict[str, Any]:
        """Build JSON payload."""

        rows_by_id: dict[str, dict[str, Any]] = {}
        for candidate in sorted(self.candidates, key=lambda item: item.source_key):
            rows_by_id.setdefault(
                candidate.inventory_id,
                candidate.to_row(generated_at=self.generated_at, commit=self.commit),
            )
        for gap in sorted(self.gaps, key=lambda item: item.inventory_id):
            rows_by_id.setdefault(
                gap.inventory_id,
                gap.to_inventory_row(generated_at=self.generated_at, commit=self.commit),
            )
        inventory = list(rows_by_id.values())
        safe_summary = dict(summary)
        safe_summary["inventory_count"] = len(inventory)
        safe_summary["discovery_gaps"] = len(self.gaps)
        return {
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "source": dict(self.source),
            "summary": safe_summary,
            "Inventory": inventory,
            "Discovery Gaps": [gap.to_gap_dict() for gap in self.gaps],
        }


def rfc3339_utc(value: datetime) -> str:
    """Format UTC datetime."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_text(value: str) -> str:
    """Return a safe single-line text value."""

    if secret_findings(value):
        return "<REDACTED:SECRET>"
    redacted = redact_inline_text(value, {}, {})
    if secret_findings(redacted):
        return "<REDACTED:SECRET>"
    return " ".join(redacted.split())


def safe_join(values: Sequence[str]) -> str:
    """Join safe note fragments."""

    cleaned = [safe_text(value) for value in values if value and not secret_findings(value)]
    return "; ".join(dict.fromkeys(cleaned))


def dumps_snapshot(payload: Mapping[str, Any]) -> bytes:
    """Serialize snapshot JSON deterministically."""

    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )

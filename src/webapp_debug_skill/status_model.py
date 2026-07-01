"""Inventory status normalization for bounded discovery coverage."""

from __future__ import annotations

from enum import Enum
from typing import Any

from webapp_debug_skill.redaction import secret_findings


class InventoryStatus(str, Enum):
    """Inventory statuses used by the coverage gate."""

    NEW = "NEW"
    DISCOVERED = "DISCOVERED"
    MAPPED = "MAPPED"
    EXCLUDED_WITH_REASON = "EXCLUDED_WITH_REASON"
    UNREACHABLE = "UNREACHABLE"
    DISCOVERY_GAP = "DISCOVERY_GAP"
    BLOCKED = "BLOCKED"
    RETIRED = "RETIRED"
    MERGED = "MERGED"


CLOSED_STATUSES = frozenset(
    {
        InventoryStatus.MAPPED,
        InventoryStatus.EXCLUDED_WITH_REASON,
    }
)
OPEN_GAP_STATUSES = frozenset(
    {
        InventoryStatus.NEW,
        InventoryStatus.DISCOVERED,
        InventoryStatus.UNREACHABLE,
        InventoryStatus.DISCOVERY_GAP,
        InventoryStatus.BLOCKED,
    }
)
EXCLUDED_STATUSES = frozenset(
    {
        InventoryStatus.RETIRED,
        InventoryStatus.MERGED,
    }
)


class StatusModelError(RuntimeError):
    """Safe status normalization error."""

    def __init__(
        self,
        code: str,
        path: str = "status",
        reason: str = "INVALID",
    ) -> None:
        safe_code = "COVERAGE_UNKNOWN_STATUS" if secret_findings(code) else code
        super().__init__(safe_code)
        self.code = safe_code
        self.path = "status" if secret_findings(path) else path
        self.reason = "INVALID" if secret_findings(reason) else reason


def normalize_inventory_status(value: Any, *, path: str = "status") -> InventoryStatus:
    """Normalize a status value case-insensitively."""

    if not isinstance(value, str):
        raise StatusModelError("COVERAGE_UNKNOWN_STATUS", path, "STRING_REQUIRED")
    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    if normalized == "":
        raise StatusModelError("COVERAGE_UNKNOWN_STATUS", path, "EMPTY")
    try:
        return InventoryStatus(normalized)
    except ValueError:
        raise StatusModelError("COVERAGE_UNKNOWN_STATUS", path, "UNKNOWN") from None


def is_closed_status(status: InventoryStatus) -> bool:
    """Return whether a status counts as closed."""

    return status in CLOSED_STATUSES


def is_open_gap_status(status: InventoryStatus) -> bool:
    """Return whether a status counts as an open discovery gap."""

    return status in OPEN_GAP_STATUSES


def is_excluded_status(status: InventoryStatus) -> bool:
    """Return whether a status is excluded from the denominator."""

    return status in EXCLUDED_STATUSES

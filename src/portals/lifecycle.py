from __future__ import annotations

from typing import Any, Iterable


def reconciliation_guard(
    *,
    acquisition: dict[str, Any] | None,
    discovered_urls: Iterable[str] | None,
) -> dict[str, Any] | None:
    """Return a no-write lifecycle result unless discovery proved completeness."""
    acquisition_metrics = acquisition or {}
    if not bool(acquisition_metrics.get("reconciliation_safe")):
        return {
            "status": "skipped_incomplete_acquisition",
            "missing_marked": 0,
            "deactivated": 0,
            "reason": "discovery completeness was not proven",
        }
    if not list(discovered_urls or []):
        return {
            "status": "skipped_no_discovered_urls",
            "missing_marked": 0,
            "deactivated": 0,
        }
    return None

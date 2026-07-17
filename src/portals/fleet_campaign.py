from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .certification import PortalInventoryEntry


FLEET_CAMPAIGN_CONTRACT_VERSION = "1.0"

_NETWORK_ERROR_TYPES = {
    "acquisitionhttperror",
    "connectionerror",
    "network_error",
    "source_timeout",
    "timeouterror",
    "urlerror",
}

_NETWORK_MESSAGE_MARKERS = (
    "connection closed",
    "connection refused",
    "could not resolve",
    "dns",
    "err_connection",
    "name resolution",
    "network",
    "timed out",
    "timeout",
)

_ACCESS_ERROR_TYPES = {
    "access_blocked",
    "portalcertificationblocked",
}

_INVALID_SOURCE_ERROR_TYPES = {
    "portalurlsafetyerror",
    "unsafe_url",
}

_DETAIL_ERROR_TYPES = {
    "detail_extraction_shortfall",
    "zero_valid_jobs",
}

_CLASSIFICATION_ACTIONS: dict[str, tuple[str, bool, bool]] = {
    "certified": ("none", False, False),
    "not_executed": ("run_certification", True, False),
    "protected_access_control": ("defer_protected_source", False, False),
    "invalid_or_unsafe_source": ("review_inventory_url", False, False),
    "transient_network_failure": ("retry_with_backoff", True, False),
    "javascript_application": ("discover_public_api_or_rendered_state", True, False),
    "route_resolved_zero_discovery": ("repair_platform_discovery", True, False),
    "zero_discovery": ("inspect_listing_transport", True, False),
    "detail_extraction_failure": (
        "repair_deterministic_detail_then_gpu_fallback",
        True,
        True,
    ),
    "partial_detail_extraction": ("repair_detail_shortfall", True, True),
    "unknown_failure": ("collect_surface_diagnostic", True, False),
}

_CLUSTER_ORDER = {
    "not_executed": 0,
    "transient_network_failure": 1,
    "route_resolved_zero_discovery": 2,
    "javascript_application": 3,
    "zero_discovery": 4,
    "detail_extraction_failure": 5,
    "partial_detail_extraction": 6,
    "unknown_failure": 7,
    "invalid_or_unsafe_source": 8,
    "protected_access_control": 9,
    "certified": 10,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        payload = value.to_dict()
        return dict(payload) if isinstance(payload, dict) else {}
    return {}


def _bounded_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _classify_record(record: Mapping[str, Any] | None) -> str:
    if not record:
        return "not_executed"

    status = str(record.get("status") or "").strip().lower()
    certification_status = str(record.get("certification_status") or "").strip().lower()
    error_type = str(record.get("error_type") or "").strip().lower()
    error_message = str(record.get("error_message") or "").strip().lower()
    surface_kind = str(record.get("surface_kind") or "").strip().lower()
    discovered = _bounded_int(record.get("discovered_urls"))
    attempted = _bounded_int(record.get("attempted_urls"))
    extracted = _bounded_int(record.get("extracted_jobs"))
    resolved_route = str(record.get("resolved_route_url") or "").strip()

    # A success flag is never sufficient by itself. Certification is useful
    # only when at least one grounded job record was actually extracted.
    if status == "success" and extracted > 0:
        return "certified"
    if (
        status == "blocked"
        or certification_status == "access_blocked"
        or error_type in _ACCESS_ERROR_TYPES
        or surface_kind == "confirmed_access_control"
    ):
        return "protected_access_control"
    if error_type in _INVALID_SOURCE_ERROR_TYPES:
        return "invalid_or_unsafe_source"
    if error_type in _NETWORK_ERROR_TYPES or any(
        marker in error_message for marker in _NETWORK_MESSAGE_MARKERS
    ):
        return "transient_network_failure"
    if error_type == "javascript_shell" or surface_kind == "javascript_shell":
        return "javascript_application"
    if extracted > 0:
        return "partial_detail_extraction"
    if discovered > 0 or attempted > 0 or error_type in _DETAIL_ERROR_TYPES:
        return "detail_extraction_failure"
    if resolved_route and discovered == 0:
        return "route_resolved_zero_discovery"
    if error_type == "zero_discovery" or (not error_type and discovered == 0):
        return "zero_discovery"
    return "unknown_failure"


def _disposition(
    entry: PortalInventoryEntry,
    record_value: Any,
) -> dict[str, Any]:
    record = _record_dict(record_value)
    classification = _classify_record(record)
    next_action, retryable, gpu_eligible = _CLASSIFICATION_ACTIONS[classification]
    return {
        "source_id": entry.source_id,
        "source_row": entry.source_row,
        "display_name": entry.display_name,
        "provided_url": entry.listing_url,
        "accounted": bool(record),
        "status": str(record.get("status") or "missing"),
        "certification_status": str(record.get("certification_status") or "not_executed"),
        "classification": classification,
        "next_action": next_action,
        "retryable": retryable,
        "gpu_eligible": gpu_eligible,
        "detected_platform": str(record.get("detected_platform") or "unknown"),
        "surface_kind": str(record.get("surface_kind") or "unknown"),
        "resolved_route_url": str(record.get("resolved_route_url") or "") or None,
        "discovered_urls": _bounded_int(record.get("discovered_urls")),
        "attempted_urls": _bounded_int(record.get("attempted_urls")),
        "extracted_jobs": _bounded_int(record.get("extracted_jobs")),
        "error_type": str(record.get("error_type") or "") or None,
        "error_message": str(record.get("error_message") or "")[:1000] or None,
        "attempt_number": _bounded_int(record.get("attempt_number")),
        "record_contract_version": str(record.get("contract_version") or "unknown"),
        "record_run_id": str(record.get("run_id") or "") or None,
    }


def build_fleet_campaign_report(
    *,
    inventory: Sequence[PortalInventoryEntry],
    latest_records: Mapping[str, Any],
    campaign_id: str,
    target_successes: int = 80,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if target_successes < 1:
        raise ValueError("target_successes must be at least 1")
    if not inventory:
        raise ValueError("fleet campaign inventory cannot be empty")

    inventory_ids = {entry.source_id for entry in inventory}
    dispositions = [
        _disposition(entry, latest_records.get(entry.source_id))
        for entry in inventory
    ]
    accounted = [item for item in dispositions if item["accounted"]]
    certified = [item for item in dispositions if item["classification"] == "certified"]
    blocked = [
        item
        for item in dispositions
        if item["classification"] == "protected_access_control"
    ]
    retryable = [item for item in dispositions if item["retryable"]]
    gpu_eligible = [item for item in dispositions if item["gpu_eligible"]]
    missing = [item for item in dispositions if not item["accounted"]]

    classification_counts = Counter(item["classification"] for item in dispositions)
    action_counts = Counter(item["next_action"] for item in dispositions)
    status_counts = Counter(item["status"] for item in dispositions)
    contract_versions = Counter(
        item["record_contract_version"]
        for item in accounted
    )
    inventory_count = len(dispositions)
    nonblocked_count = max(0, inventory_count - len(blocked))
    complete = len(accounted) == inventory_count
    target_met = complete and len(certified) >= int(target_successes)

    return {
        "contract_version": FLEET_CAMPAIGN_CONTRACT_VERSION,
        "campaign_id": str(campaign_id),
        "generated_at": generated_at or _utc_now(),
        "inventory_count": inventory_count,
        "inventory_source_ids": [entry.source_id for entry in inventory],
        "inventory_duplicates": inventory_count - len(inventory_ids),
        "accounted_count": len(accounted),
        "missing_count": len(missing),
        "complete": complete,
        "certified_source_count": len(certified),
        "blocked_source_count": len(blocked),
        "actionable_source_count": len(retryable),
        "gpu_eligible_source_count": len(gpu_eligible),
        "extracted_job_count": sum(item["extracted_jobs"] for item in dispositions),
        "target_successes": int(target_successes),
        "target_met": target_met,
        "coverage_ratio": round(len(certified) / inventory_count, 4),
        "nonblocked_coverage_ratio": round(
            len(certified) / nonblocked_count if nonblocked_count else 0.0,
            4,
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "classification_counts": dict(sorted(classification_counts.items())),
        "action_counts": dict(sorted(action_counts.items())),
        "record_contract_versions": dict(sorted(contract_versions.items())),
        "certified_source_ids": [item["source_id"] for item in certified],
        "blocked_source_ids": [item["source_id"] for item in blocked],
        "retry_source_ids": [item["source_id"] for item in retryable],
        "gpu_eligible_source_ids": [item["source_id"] for item in gpu_eligible],
        "missing_source_ids": [item["source_id"] for item in missing],
        "bounded_certification": True,
        "production_ingestion_performed": False,
        "safe_for_lifecycle_reconciliation": False,
        "records": dispositions,
    }


def build_fleet_repair_manifest(report: Mapping[str, Any]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for value in report.get("records") or []:
        if not isinstance(value, dict):
            continue
        classification = str(value.get("classification") or "unknown_failure")
        if classification != "certified":
            grouped[classification].append(value)

    clusters: list[dict[str, Any]] = []
    for classification, records in sorted(
        grouped.items(),
        key=lambda item: (_CLUSTER_ORDER.get(item[0], 99), item[0]),
    ):
        next_action, retryable, gpu_eligible = _CLASSIFICATION_ACTIONS.get(
            classification,
            _CLASSIFICATION_ACTIONS["unknown_failure"],
        )
        clusters.append(
            {
                "classification": classification,
                "next_action": next_action,
                "count": len(records),
                "retryable": retryable,
                "gpu_eligible": gpu_eligible,
                "source_ids": [str(item.get("source_id") or "") for item in records],
            }
        )

    return {
        "contract_version": FLEET_CAMPAIGN_CONTRACT_VERSION,
        "campaign_id": report.get("campaign_id"),
        "generated_at": report.get("generated_at"),
        "inventory_count": report.get("inventory_count"),
        "accounted_count": report.get("accounted_count"),
        "complete": bool(report.get("complete")),
        "target_successes": report.get("target_successes"),
        "target_met": bool(report.get("target_met")),
        "retry_source_ids": list(report.get("retry_source_ids") or []),
        "blocked_source_ids": list(report.get("blocked_source_ids") or []),
        "gpu_eligible_source_ids": list(report.get("gpu_eligible_source_ids") or []),
        "missing_source_ids": list(report.get("missing_source_ids") or []),
        "clusters": clusters,
    }


def select_campaign_source_ids(
    report: Mapping[str, Any],
    *,
    mode: str,
) -> list[str]:
    normalized_mode = str(mode or "").strip().lower().replace("_", "-")
    if normalized_mode == "full":
        return [str(value) for value in report.get("inventory_source_ids") or []]
    if normalized_mode == "resume":
        return [str(value) for value in report.get("missing_source_ids") or []]
    if normalized_mode == "retry-actionable":
        return [str(value) for value in report.get("retry_source_ids") or []]
    if normalized_mode == "retry-gpu-eligible":
        return [str(value) for value in report.get("gpu_eligible_source_ids") or []]
    if normalized_mode == "analyze-only":
        return []
    raise ValueError(
        "campaign mode must be full, resume, retry-actionable, "
        "retry-gpu-eligible, or analyze-only"
    )


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )


def write_fleet_campaign_artifacts(
    *,
    output_dir: Path,
    report: Mapping[str, Any],
) -> dict[str, str]:
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    report_path = destination / "fleet_campaign_report.json"
    manifest_path = destination / "fleet_repair_manifest.json"
    csv_path = destination / "fleet_campaign_report.csv"
    retry_path = destination / "fleet_retry_source_ids.txt"
    blocked_path = destination / "fleet_blocked_source_ids.txt"
    gpu_path = destination / "fleet_gpu_eligible_source_ids.txt"
    missing_path = destination / "fleet_missing_source_ids.txt"

    manifest = build_fleet_repair_manifest(report)
    _atomic_json(report_path, dict(report))
    _atomic_json(manifest_path, manifest)

    rows = [item for item in report.get("records") or [] if isinstance(item, dict)]
    fields = [
        "source_id",
        "source_row",
        "display_name",
        "provided_url",
        "accounted",
        "status",
        "certification_status",
        "classification",
        "next_action",
        "retryable",
        "gpu_eligible",
        "detected_platform",
        "surface_kind",
        "resolved_route_url",
        "discovered_urls",
        "attempted_urls",
        "extracted_jobs",
        "error_type",
        "error_message",
    ]
    csv_temporary = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with csv_temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(csv_temporary, csv_path)

    def ids(name: str) -> str:
        values = [str(value) for value in report.get(name) or [] if str(value).strip()]
        return "\n".join(values) + ("\n" if values else "")

    _atomic_text(retry_path, ids("retry_source_ids"))
    _atomic_text(blocked_path, ids("blocked_source_ids"))
    _atomic_text(gpu_path, ids("gpu_eligible_source_ids"))
    _atomic_text(missing_path, ids("missing_source_ids"))
    return {
        "report": str(report_path),
        "manifest": str(manifest_path),
        "csv": str(csv_path),
        "retry_source_ids": str(retry_path),
        "blocked_source_ids": str(blocked_path),
        "gpu_eligible_source_ids": str(gpu_path),
        "missing_source_ids": str(missing_path),
    }

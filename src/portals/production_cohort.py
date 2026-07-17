from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .certification import PortalInventoryEntry, read_portal_inventory


PRODUCTION_COHORT_CONTRACT_VERSION = "1.0"
PHASE = "5.5C"


class ProductionCohortError(ValueError):
    """Raised when certification evidence cannot safely freeze a cohort."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Required JSON file does not exist: {source}")
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ProductionCohortError(f"Expected a JSON object in {source}")
    return payload


def _string_ids(values: Any, *, field: str) -> list[str]:
    if not isinstance(values, list):
        raise ProductionCohortError(f"{field} must be a JSON array")
    ids = [str(value or "").strip() for value in values]
    if any(not value for value in ids):
        raise ProductionCohortError(f"{field} contains an empty source id")
    if len(ids) != len(set(ids)):
        raise ProductionCohortError(f"{field} contains duplicate source ids")
    return ids


def _records_by_source(payload: Mapping[str, Any], *, field: str) -> dict[str, dict[str, Any]]:
    records = payload.get("records")
    if not isinstance(records, list):
        raise ProductionCohortError(f"{field}.records must be a JSON array")
    result: dict[str, dict[str, Any]] = {}
    for value in records:
        if not isinstance(value, dict):
            continue
        source_id = str(value.get("source_id") or "").strip()
        if not source_id:
            raise ProductionCohortError(f"{field}.records contains a record without source_id")
        if source_id in result:
            raise ProductionCohortError(f"{field}.records contains duplicate source_id {source_id}")
        result[source_id] = dict(value)
    return result


def _validate_inventory(
    inventory: Sequence[PortalInventoryEntry],
    *,
    expected_inventory: int,
) -> tuple[list[str], dict[str, PortalInventoryEntry]]:
    if len(inventory) != expected_inventory:
        raise ProductionCohortError(
            f"Expected {expected_inventory} inventory sources but parsed {len(inventory)}"
        )
    ids = [entry.source_id for entry in inventory]
    if len(ids) != len(set(ids)):
        raise ProductionCohortError("Portal inventory contains duplicate source ids")
    return ids, {entry.source_id: entry for entry in inventory}


def _validate_report(
    report: Mapping[str, Any],
    *,
    inventory_ids: Sequence[str],
    expected_certified: int,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    inventory_set = set(inventory_ids)
    report_inventory_ids = _string_ids(
        report.get("inventory_source_ids"),
        field="fleet_report.inventory_source_ids",
    )
    if set(report_inventory_ids) != inventory_set:
        missing = sorted(inventory_set - set(report_inventory_ids))
        extra = sorted(set(report_inventory_ids) - inventory_set)
        raise ProductionCohortError(
            "Fleet report inventory does not match the current workbook "
            f"(missing={missing[:5]}, extra={extra[:5]})"
        )
    if int(report.get("inventory_count") or 0) != len(inventory_ids):
        raise ProductionCohortError("Fleet report inventory_count is inconsistent")
    if int(report.get("accounted_count") or 0) != len(inventory_ids):
        raise ProductionCohortError("Fleet report does not account for every inventory source")
    if int(report.get("missing_count") or 0) != 0 or not bool(report.get("complete")):
        raise ProductionCohortError("Fleet report is incomplete; missing sources cannot be frozen")
    if int(report.get("inventory_duplicates") or 0) != 0:
        raise ProductionCohortError("Fleet report records inventory duplicates")
    if not bool(report.get("bounded_certification")):
        raise ProductionCohortError("Fleet report is not marked as bounded certification evidence")
    if bool(report.get("production_ingestion_performed")):
        raise ProductionCohortError("Closeout evidence unexpectedly reports production ingestion")
    if bool(report.get("safe_for_lifecycle_reconciliation")):
        raise ProductionCohortError("Bounded certification must not enable lifecycle reconciliation")

    certified_ids = _string_ids(
        report.get("certified_source_ids"),
        field="fleet_report.certified_source_ids",
    )
    if len(certified_ids) != expected_certified:
        raise ProductionCohortError(
            f"Expected exactly {expected_certified} certified sources but report has {len(certified_ids)}"
        )
    if int(report.get("certified_source_count") or -1) != len(certified_ids):
        raise ProductionCohortError("Fleet report certified_source_count is inconsistent")
    if not set(certified_ids).issubset(inventory_set):
        raise ProductionCohortError("Fleet report certifies a source outside the inventory")

    records = _records_by_source(report, field="fleet_report")
    if set(records) != inventory_set:
        raise ProductionCohortError("Fleet report records do not cover the full inventory")
    for source_id in certified_ids:
        record = records[source_id]
        if str(record.get("classification") or "") != "certified":
            raise ProductionCohortError(f"Certified source {source_id} is not classified certified")
        if str(record.get("status") or "").lower() != "success":
            raise ProductionCohortError(f"Certified source {source_id} does not have success status")
        if int(record.get("extracted_jobs") or 0) < 1:
            raise ProductionCohortError(f"Certified source {source_id} extracted zero jobs")
    return certified_ids, records


def _validate_summary(
    summary: Mapping[str, Any],
    *,
    inventory_ids: Sequence[str],
    certified_ids: Sequence[str],
    inventory_sha256: str,
) -> None:
    if int(summary.get("inventory_count") or 0) != len(inventory_ids):
        raise ProductionCohortError("Certification summary inventory_count is inconsistent")
    if int(summary.get("latest_result_count") or 0) != len(inventory_ids):
        raise ProductionCohortError("Certification summary does not contain 102 latest results")
    recorded_hash = str(summary.get("input_sha256") or "").strip().lower()
    if recorded_hash and recorded_hash != inventory_sha256.lower():
        raise ProductionCohortError("Certification summary was produced from a different inventory file")

    records = _records_by_source(summary, field="certification_summary")
    if set(records) != set(inventory_ids):
        raise ProductionCohortError("Certification summary records do not cover the full inventory")
    summary_successes = {
        source_id
        for source_id, record in records.items()
        if str(record.get("status") or "").lower() == "success"
        and int(record.get("extracted_jobs") or 0) > 0
    }
    if summary_successes != set(certified_ids):
        missing = sorted(set(certified_ids) - summary_successes)
        extra = sorted(summary_successes - set(certified_ids))
        raise ProductionCohortError(
            "Fleet report and certification summary disagree on successful sources "
            f"(missing={missing[:5]}, extra={extra[:5]})"
        )


def build_frozen_production_cohort(
    *,
    input_path: Path,
    fleet_report_path: Path,
    certification_summary_path: Path,
    expected_inventory: int = 102,
    expected_certified: int = 19,
    frozen_at: str | None = None,
) -> dict[str, Any]:
    input_path = Path(input_path).resolve()
    fleet_report_path = Path(fleet_report_path).resolve()
    certification_summary_path = Path(certification_summary_path).resolve()

    inventory = read_portal_inventory(input_path)
    inventory_ids, inventory_by_id = _validate_inventory(
        inventory,
        expected_inventory=expected_inventory,
    )
    inventory_sha256 = _file_sha256(input_path)
    report = _load_json(fleet_report_path)
    summary = _load_json(certification_summary_path)
    certified_ids, report_records = _validate_report(
        report,
        inventory_ids=inventory_ids,
        expected_certified=expected_certified,
    )
    _validate_summary(
        summary,
        inventory_ids=inventory_ids,
        certified_ids=certified_ids,
        inventory_sha256=inventory_sha256,
    )

    certified_set = set(certified_ids)
    ordered_certified_ids = [source_id for source_id in inventory_ids if source_id in certified_set]
    deferred_ids = [source_id for source_id in inventory_ids if source_id not in certified_set]
    sources: list[dict[str, Any]] = []
    for source_id in ordered_certified_ids:
        entry = inventory_by_id[source_id]
        record = report_records[source_id]
        sources.append(
            {
                "source_id": source_id,
                "source_row": entry.source_row,
                "display_name": entry.display_name,
                "listing_url": entry.listing_url,
                "detected_platform": str(record.get("detected_platform") or "unknown"),
                "resolved_route_url": record.get("resolved_route_url"),
                "bounded_extracted_jobs": int(record.get("extracted_jobs") or 0),
                "evidence_run_id": record.get("record_run_id"),
            }
        )

    payload: dict[str, Any] = {
        "contract_version": PRODUCTION_COHORT_CONTRACT_VERSION,
        "phase": PHASE,
        "cohort_status": "frozen_certified_cohort",
        "frozen_at": frozen_at or _utc_now(),
        "inventory": {
            "path": input_path.name,
            "sha256": inventory_sha256,
            "source_count": len(inventory_ids),
        },
        "evidence": {
            "fleet_report_path": fleet_report_path.name,
            "fleet_report_sha256": _file_sha256(fleet_report_path),
            "fleet_campaign_id": report.get("campaign_id"),
            "certification_summary_path": certification_summary_path.name,
            "certification_summary_sha256": _file_sha256(certification_summary_path),
            "certification_run_id": summary.get("run_id"),
        },
        "cohort_source_count": len(ordered_certified_ids),
        "deferred_source_count": len(deferred_ids),
        "cohort_source_ids": ordered_certified_ids,
        "deferred_source_ids": deferred_ids,
        "sources": sources,
        "safety": {
            "bounded_certification_only": True,
            "full_inventory_extraction_validated": False,
            "production_ingestion_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "failed_or_partial_runs_may_deactivate_jobs": False,
            "phase_6_validation_required": True,
        },
    }
    payload["cohort_sha256"] = _canonical_sha256(payload)
    return payload


def validate_frozen_production_cohort(
    *,
    cohort_path: Path,
    input_path: Path,
    fleet_report_path: Path,
    certification_summary_path: Path,
    expected_inventory: int = 102,
    expected_certified: int = 19,
) -> dict[str, Any]:
    stored = _load_json(Path(cohort_path))
    stored_hash = str(stored.get("cohort_sha256") or "").strip()
    without_hash = dict(stored)
    without_hash.pop("cohort_sha256", None)
    if not stored_hash or stored_hash != _canonical_sha256(without_hash):
        raise ProductionCohortError("Frozen cohort checksum is missing or invalid")

    rebuilt = build_frozen_production_cohort(
        input_path=input_path,
        fleet_report_path=fleet_report_path,
        certification_summary_path=certification_summary_path,
        expected_inventory=expected_inventory,
        expected_certified=expected_certified,
        frozen_at=str(stored.get("frozen_at") or ""),
    )
    if stored != rebuilt:
        raise ProductionCohortError(
            "Frozen cohort no longer matches the inventory or certification evidence"
        )
    return stored


def _atomic_text(path: Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, target)


def write_frozen_production_cohort(
    *,
    output_dir: Path,
    cohort: Mapping[str, Any],
) -> dict[str, str]:
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    cohort_path = destination / "phase5_5c_certified_cohort.json"
    source_ids_path = destination / "phase5_5c_cohort_source_ids.txt"
    deferred_ids_path = destination / "phase5_5c_deferred_source_ids.txt"

    _atomic_text(
        cohort_path,
        json.dumps(dict(cohort), indent=2, ensure_ascii=False) + "\n",
    )

    def ids(field: str) -> str:
        values = [str(value) for value in cohort.get(field) or []]
        return "\n".join(values) + ("\n" if values else "")

    _atomic_text(source_ids_path, ids("cohort_source_ids"))
    _atomic_text(deferred_ids_path, ids("deferred_source_ids"))
    return {
        "cohort": str(cohort_path),
        "cohort_source_ids": str(source_ids_path),
        "deferred_source_ids": str(deferred_ids_path),
    }


def read_source_id_file(path: Path) -> list[str]:
    source = Path(path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Source id file does not exist: {source}")
    ids = [
        line.strip()
        for line in source.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not ids:
        raise ProductionCohortError(f"Source id file is empty: {source}")
    if len(ids) != len(set(ids)):
        raise ProductionCohortError(f"Source id file contains duplicate ids: {source}")
    return ids

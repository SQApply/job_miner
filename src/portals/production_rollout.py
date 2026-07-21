from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..schemas import JobPosting
from ..warehouse.url_utils import canonical_job_url
from .certification import PortalInventoryEntry, read_portal_inventory
from .repair_campaign import RepairCohort, load_repair_cohort
from .url_intelligence import assess_certification_job


PHASE_7D1_COHORT_CONTRACT_VERSION = "1.1"
PHASE_7D1_REPORT_CONTRACT_VERSION = "1.0"
PHASE_7D1 = "7D1"
PHASE_7D1_COHORT_STATUS = "frozen_truthful_production_cohort"


class ProductionRolloutError(ValueError):
    """Raised when saved certification evidence is unsafe for production."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Required {label} does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ProductionRolloutError(f"{label} contains invalid JSON: {source}") from exc
    if not isinstance(payload, dict):
        raise ProductionRolloutError(f"{label} must contain a JSON object")
    return payload


def _records_by_source(payload: Mapping[str, Any], *, label: str) -> dict[str, dict[str, Any]]:
    values = payload.get("records")
    if not isinstance(values, list):
        raise ProductionRolloutError(f"{label}.records must be a JSON array")
    records: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict):
            raise ProductionRolloutError(f"{label}.records contains a non-object record")
        source_id = str(value.get("source_id") or "").strip()
        if not source_id:
            raise ProductionRolloutError(f"{label}.records contains a missing source_id")
        if source_id in records:
            raise ProductionRolloutError(f"{label}.records contains duplicate {source_id}")
        records[source_id] = dict(value)
    return records


def _validate_inventory(
    inventory: Sequence[PortalInventoryEntry],
    *,
    expected_inventory: int,
) -> tuple[list[str], dict[str, PortalInventoryEntry]]:
    if len(inventory) != expected_inventory:
        raise ProductionRolloutError(
            f"Expected {expected_inventory} inventory sources but parsed {len(inventory)}"
        )
    ids = [entry.source_id for entry in inventory]
    if len(ids) != len(set(ids)):
        raise ProductionRolloutError("Portal inventory contains duplicate source ids")
    return ids, {entry.source_id: entry for entry in inventory}


def _validate_summary(
    payload: Mapping[str, Any],
    *,
    label: str,
    expected_inventory: int,
    expected_record_ids: set[str],
    inventory_sha256: str,
) -> dict[str, dict[str, Any]]:
    if int(payload.get("inventory_count") or 0) != expected_inventory:
        raise ProductionRolloutError(f"{label}.inventory_count is inconsistent")
    recorded_hash = str(payload.get("input_sha256") or "").strip().lower()
    if not recorded_hash or recorded_hash != inventory_sha256.lower():
        raise ProductionRolloutError(f"{label} was produced from a different inventory file")
    records = _records_by_source(payload, label=label)
    try:
        latest_result_count = int(payload.get("latest_result_count"))
    except (TypeError, ValueError):
        latest_result_count = -1
    if latest_result_count != len(records):
        raise ProductionRolloutError(f"{label}.latest_result_count is inconsistent")
    if set(records) != expected_record_ids:
        missing = sorted(expected_record_ids - set(records))
        extra = sorted(set(records) - expected_record_ids)
        raise ProductionRolloutError(
            f"{label} source coverage is inconsistent "
            f"(missing={missing[:5]}, extra={extra[:5]})"
        )
    options = payload.get("options")
    if not isinstance(options, dict) or int(options.get("max_jobs") or 0) != 10:
        raise ProductionRolloutError(f"{label} is not bounded to ten certification jobs")
    actual_counts = Counter(str(record.get("status") or "unknown") for record in records.values())
    raw_counts = payload.get("status_counts")
    if not isinstance(raw_counts, dict):
        raise ProductionRolloutError(f"{label}.status_counts is missing")
    recorded_counts = {str(key): int(value) for key, value in raw_counts.items()}
    if recorded_counts != dict(actual_counts):
        raise ProductionRolloutError(f"{label}.status_counts is inconsistent")
    return records


def _validate_record_identity(
    records: Mapping[str, Mapping[str, Any]],
    inventory_by_id: Mapping[str, PortalInventoryEntry],
    *,
    label: str,
) -> None:
    for source_id, record in records.items():
        expected_url = inventory_by_id[source_id].listing_url
        provided_url = str(record.get("provided_url") or "").strip()
        if provided_url != expected_url:
            raise ProductionRolloutError(
                f"{label} record {source_id} does not match its inventory URL"
            )


def assess_production_record(
    record: Mapping[str, Any],
    *,
    evidence_origin: str,
) -> dict[str, Any]:
    """Independently revalidate every bounded sample before cohort admission."""

    source_id = str(record.get("source_id") or "").strip()
    status = str(record.get("status") or "unknown").strip().lower()
    try:
        extracted_jobs = int(record.get("extracted_jobs") or 0)
    except (TypeError, ValueError):
        extracted_jobs = 0
    raw_samples = record.get("sample_jobs")
    samples = raw_samples if isinstance(raw_samples, list) else []
    reasons: list[str] = []
    sample_rejections: list[dict[str, Any]] = []
    canonical_urls: list[str] = []
    valid_samples = 0

    if status != "success":
        reasons.append(f"status_{status}_not_production_eligible")
    if extracted_jobs < 1:
        reasons.append("zero_extracted_jobs")
    if not isinstance(raw_samples, list):
        reasons.append("sample_jobs_not_array")
    if len(samples) != extracted_jobs:
        reasons.append("sample_count_does_not_match_extracted_jobs")
    if len(samples) > 10:
        reasons.append("sample_count_exceeds_certification_bound")

    source_url = str(
        record.get("effective_listing_url") or record.get("provided_url") or ""
    ).strip()
    for index, payload in enumerate(samples):
        rejection: str | None = None
        if not isinstance(payload, dict):
            rejection = "invalid_sample_payload"
        else:
            try:
                job = JobPosting.model_validate(payload)
            except Exception as exc:
                rejection = f"invalid_job_schema:{type(exc).__name__}"
            else:
                try:
                    normalized_url = canonical_job_url(job.job_url)
                except ValueError:
                    normalized_url = ""
                if not normalized_url:
                    rejection = "missing_or_invalid_job_url"
                else:
                    canonical_urls.append(normalized_url)
                    valid, validation_reason = assess_certification_job(job, source_url)
                    if not valid:
                        rejection = validation_reason
        if rejection is None:
            valid_samples += 1
        else:
            sample_rejections.append({"sample_index": index, "reason": rejection})

    if len(canonical_urls) != len(set(canonical_urls)):
        reasons.append("duplicate_canonical_sample_job_urls")
    if sample_rejections:
        reasons.append("one_or_more_sample_jobs_failed_quality")
    production_ready = (
        status == "success"
        and extracted_jobs >= 1
        and len(samples) == extracted_jobs
        and len(samples) <= 10
        and valid_samples == len(samples)
        and len(canonical_urls) == len(set(canonical_urls))
        and not reasons
    )
    return {
        "source_id": source_id,
        "evidence_origin": evidence_origin,
        "status": status,
        "certification_status": str(record.get("certification_status") or "unknown"),
        "extracted_jobs": extracted_jobs,
        "sample_job_count": len(samples),
        "valid_sample_job_count": valid_samples,
        "production_ready": production_ready,
        "classification": "production_ready" if production_ready else "deferred",
        "rejection_reasons": reasons,
        "sample_rejections": sample_rejections,
    }


def _ready_ids(
    records: Mapping[str, Mapping[str, Any]],
    *,
    evidence_origin: str,
) -> tuple[set[str], dict[str, dict[str, Any]]]:
    assessments = {
        source_id: assess_production_record(record, evidence_origin=evidence_origin)
        for source_id, record in records.items()
    }
    return {
        source_id
        for source_id, assessment in assessments.items()
        if assessment["production_ready"]
    }, assessments


def build_phase7d1_production_rollout(
    *,
    input_path: Path,
    baseline_summary_path: Path,
    repair_manifest_path: Path,
    repair_summary_path: Path,
    expected_inventory: int = 102,
    expected_baseline_ready: int = 24,
    expected_repair_records: int = 15,
    expected_final_ready: int = 26,
    frozen_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Freeze a production cohort from existing evidence without any live crawl."""

    if expected_final_ready < expected_baseline_ready:
        raise ProductionRolloutError(
            "expected_final_ready cannot be smaller than expected_baseline_ready"
        )
    input_path = Path(input_path).resolve()
    baseline_summary_path = Path(baseline_summary_path).resolve()
    repair_manifest_path = Path(repair_manifest_path).resolve()
    repair_summary_path = Path(repair_summary_path).resolve()
    inventory = read_portal_inventory(input_path)
    inventory_ids, inventory_by_id = _validate_inventory(
        inventory,
        expected_inventory=expected_inventory,
    )
    inventory_set = set(inventory_ids)
    inventory_sha256 = _file_sha256(input_path)
    baseline_payload = _load_json(baseline_summary_path, label="baseline summary")
    repair_payload = _load_json(repair_summary_path, label="repair summary")
    cohort: RepairCohort = load_repair_cohort(repair_manifest_path)

    if cohort.baseline_production_ready != expected_baseline_ready:
        raise ProductionRolloutError(
            "Repair manifest baseline_production_ready does not match the expected baseline"
        )
    if len(cohort.source_ids) != expected_repair_records:
        raise ProductionRolloutError(
            f"Expected {expected_repair_records} repair records but manifest has "
            f"{len(cohort.source_ids)}"
        )
    if not set(cohort.source_ids).issubset(inventory_set):
        raise ProductionRolloutError("Repair manifest contains a source outside the inventory")

    baseline_records = _validate_summary(
        baseline_payload,
        label="baseline_summary",
        expected_inventory=expected_inventory,
        expected_record_ids=inventory_set,
        inventory_sha256=inventory_sha256,
    )
    repair_records = _validate_summary(
        repair_payload,
        label="repair_summary",
        expected_inventory=expected_inventory,
        expected_record_ids=set(cohort.source_ids),
        inventory_sha256=inventory_sha256,
    )
    if str(baseline_payload.get("contract_version") or "") != str(
        repair_payload.get("contract_version") or ""
    ):
        raise ProductionRolloutError("Baseline and repair summaries use different contracts")
    _validate_record_identity(baseline_records, inventory_by_id, label="baseline_summary")
    _validate_record_identity(repair_records, inventory_by_id, label="repair_summary")

    baseline_ready, _ = _ready_ids(
        baseline_records,
        evidence_origin="baseline",
    )
    if len(baseline_ready) != expected_baseline_ready:
        raise ProductionRolloutError(
            f"Expected {expected_baseline_ready} truthful baseline sources but revalidation "
            f"found {len(baseline_ready)}"
        )

    final_records = dict(baseline_records)
    final_origins = {source_id: "baseline" for source_id in inventory_ids}
    for source_id, record in repair_records.items():
        final_records[source_id] = record
        final_origins[source_id] = "repair"
    final_assessments = {
        source_id: assess_production_record(
            final_records[source_id],
            evidence_origin=final_origins[source_id],
        )
        for source_id in inventory_ids
    }
    final_ready = {
        source_id
        for source_id, assessment in final_assessments.items()
        if assessment["production_ready"]
    }
    added = final_ready - baseline_ready
    removed = baseline_ready - final_ready
    if removed:
        raise ProductionRolloutError(
            "Repair evidence unexpectedly removed baseline-ready sources: "
            + ", ".join(sorted(removed))
        )
    if len(final_ready) != expected_final_ready:
        raise ProductionRolloutError(
            f"Expected {expected_final_ready} truthful final sources but revalidation found "
            f"{len(final_ready)}"
        )
    if len(added) != expected_final_ready - expected_baseline_ready:
        raise ProductionRolloutError("Final cohort addition count is inconsistent")

    ordered_ready = [source_id for source_id in inventory_ids if source_id in final_ready]
    ordered_deferred = [source_id for source_id in inventory_ids if source_id not in final_ready]
    generated_at = frozen_at or _utc_now()
    quality_report: dict[str, Any] = {
        "contract_version": PHASE_7D1_REPORT_CONTRACT_VERSION,
        "phase": PHASE_7D1,
        "report_status": "truthful_evidence_revalidation_complete",
        "generated_at": generated_at,
        "quality_policy": {
            "success_status_required": True,
            "all_bounded_samples_must_validate": True,
            "sample_count_must_match_extracted_count": True,
            "canonical_sample_urls_must_be_unique": True,
            "partial_or_failed_sources_allowed": False,
            "live_network_requests_performed": False,
        },
        "counts": {
            "inventory": len(inventory_ids),
            "baseline_production_ready": len(baseline_ready),
            "repair_records": len(repair_records),
            "newly_production_ready": len(added),
            "final_production_ready": len(ordered_ready),
            "deferred": len(ordered_deferred),
        },
        "baseline_production_ready_source_ids": [
            source_id for source_id in inventory_ids if source_id in baseline_ready
        ],
        "newly_production_ready_source_ids": [
            source_id for source_id in inventory_ids if source_id in added
        ],
        "final_production_ready_source_ids": ordered_ready,
        "deferred_source_ids": ordered_deferred,
        "repair_source_ids": list(cohort.source_ids),
        "records": [final_assessments[source_id] for source_id in inventory_ids],
    }
    quality_report["report_sha256"] = _canonical_sha256(quality_report)

    sources: list[dict[str, Any]] = []
    for source_id in ordered_ready:
        entry = inventory_by_id[source_id]
        record = final_records[source_id]
        sources.append(
            {
                "source_id": source_id,
                "source_row": entry.source_row,
                "display_name": entry.display_name,
                "listing_url": entry.listing_url,
                "detected_platform": str(record.get("detected_platform") or "unknown"),
                "resolved_route_url": (
                    str(record.get("resolved_route_url") or "").strip() or None
                ),
                "bounded_extracted_jobs": int(record.get("extracted_jobs") or 0),
                "evidence_run_id": record.get("run_id"),
            }
        )

    cohort_payload: dict[str, Any] = {
        "contract_version": PHASE_7D1_COHORT_CONTRACT_VERSION,
        "phase": PHASE_7D1,
        "cohort_status": PHASE_7D1_COHORT_STATUS,
        "frozen_at": generated_at,
        "inventory": {
            "path": input_path.name,
            "sha256": inventory_sha256,
            "source_count": len(inventory_ids),
        },
        "evidence": {
            "baseline_summary_path": baseline_summary_path.name,
            "baseline_summary_sha256": _file_sha256(baseline_summary_path),
            "baseline_run_id": baseline_payload.get("run_id"),
            "repair_manifest_path": repair_manifest_path.name,
            "repair_manifest_sha256": _file_sha256(repair_manifest_path),
            "repair_summary_path": repair_summary_path.name,
            "repair_summary_sha256": _file_sha256(repair_summary_path),
            "repair_run_id": repair_payload.get("run_id"),
            "quality_report_sha256": quality_report["report_sha256"],
        },
        "cohort_source_count": len(ordered_ready),
        "deferred_source_count": len(ordered_deferred),
        "cohort_source_ids": ordered_ready,
        "deferred_source_ids": ordered_deferred,
        "sources": sources,
        "safety": {
            "bounded_certification_only": True,
            "full_inventory_extraction_validated": False,
            "production_ingestion_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "failed_or_partial_runs_may_deactivate_jobs": False,
            "phase_6_validation_required": True,
            "sample_job_evidence_revalidated": True,
            "partial_sources_excluded": True,
            "failed_sources_excluded": True,
            "lifecycle_reconciliation_requires_two_clean_runs": True,
        },
    }
    cohort_payload["cohort_sha256"] = _canonical_sha256(cohort_payload)
    return cohort_payload, quality_report


def _atomic_text(path: Path, content: str) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, target)


def write_phase7d1_production_rollout(
    *,
    output_dir: Path,
    cohort: Mapping[str, Any],
    quality_report: Mapping[str, Any],
) -> dict[str, str]:
    destination = Path(output_dir).resolve()
    artifacts = {
        "cohort": destination / "phase7d_truthful_production_cohort.json",
        "quality_report": destination / "phase7d_truthful_quality_report.json",
        "cohort_source_ids": destination / "phase7d_production_source_ids.txt",
        "deferred_source_ids": destination / "phase7d_deferred_source_ids.txt",
    }
    _atomic_text(
        artifacts["cohort"],
        json.dumps(dict(cohort), indent=2, ensure_ascii=False) + "\n",
    )
    _atomic_text(
        artifacts["quality_report"],
        json.dumps(dict(quality_report), indent=2, ensure_ascii=False) + "\n",
    )
    for path_key, value_key in (
        ("cohort_source_ids", "cohort_source_ids"),
        ("deferred_source_ids", "deferred_source_ids"),
    ):
        values = [str(value) for value in cohort.get(value_key) or []]
        _atomic_text(artifacts[path_key], "\n".join(values) + ("\n" if values else ""))
    return {key: str(path) for key, path in artifacts.items()}


def validate_phase7d1_production_rollout(
    *,
    cohort_path: Path,
    quality_report_path: Path,
    input_path: Path,
    baseline_summary_path: Path,
    repair_manifest_path: Path,
    repair_summary_path: Path,
    expected_inventory: int = 102,
    expected_baseline_ready: int = 24,
    expected_repair_records: int = 15,
    expected_final_ready: int = 26,
) -> tuple[dict[str, Any], dict[str, Any]]:
    stored_cohort = _load_json(cohort_path, label="Phase 7D1 cohort")
    stored_report = _load_json(quality_report_path, label="Phase 7D1 quality report")
    cohort_hash = str(stored_cohort.get("cohort_sha256") or "").strip().lower()
    report_hash = str(stored_report.get("report_sha256") or "").strip().lower()
    cohort_without_hash = dict(stored_cohort)
    report_without_hash = dict(stored_report)
    cohort_without_hash.pop("cohort_sha256", None)
    report_without_hash.pop("report_sha256", None)
    if not cohort_hash or cohort_hash != _canonical_sha256(cohort_without_hash):
        raise ProductionRolloutError("Phase 7D1 cohort checksum is missing or invalid")
    if not report_hash or report_hash != _canonical_sha256(report_without_hash):
        raise ProductionRolloutError("Phase 7D1 quality report checksum is missing or invalid")
    stored_evidence = stored_cohort.get("evidence")
    if not isinstance(stored_evidence, dict):
        raise ProductionRolloutError("Phase 7D1 cohort evidence metadata is missing")
    if stored_evidence.get("quality_report_sha256") != report_hash:
        raise ProductionRolloutError("Phase 7D1 cohort references a different quality report")

    rebuilt_cohort, rebuilt_report = build_phase7d1_production_rollout(
        input_path=input_path,
        baseline_summary_path=baseline_summary_path,
        repair_manifest_path=repair_manifest_path,
        repair_summary_path=repair_summary_path,
        expected_inventory=expected_inventory,
        expected_baseline_ready=expected_baseline_ready,
        expected_repair_records=expected_repair_records,
        expected_final_ready=expected_final_ready,
        frozen_at=str(stored_cohort.get("frozen_at") or ""),
    )
    if stored_cohort != rebuilt_cohort or stored_report != rebuilt_report:
        raise ProductionRolloutError(
            "Frozen Phase 7D1 artifacts no longer match their certification evidence"
        )
    return stored_cohort, stored_report

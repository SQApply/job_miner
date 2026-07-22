from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import Field, model_validator

from .contracts import ContractModel
from .production_ingestion import Phase6AIngestionPlan
from .production_pilot import load_phase7d2a_pilot_inputs


PHASE_7D2B_CONTRACT_VERSION = "1.0"
PHASE_7D2B = "7D2B"
PHASE_7D2B_WRITE_CONFIRMATION = "ENABLE_PHASE_7D2B_TWO_SOURCE_WRITES"


class ProductionCanaryError(RuntimeError):
    """Raised when the bounded production-write canary is not safe to run."""


class Phase7D2BWriteAuthorization(ContractModel):
    contract_version: Literal["1.0"] = PHASE_7D2B_CONTRACT_VERSION
    phase: Literal["7D2B"] = PHASE_7D2B
    plan_id: str = Field(min_length=1)
    cohort_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    quality_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pilot_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pilot_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pilot_run_id: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    expected_source_count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_authorization(self) -> "Phase7D2BWriteAuthorization":
        if len(self.source_ids) != self.expected_source_count:
            raise ValueError("Write authorization source count is inconsistent")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("Write authorization source ids must be unique")
        return self


class Phase7D2BSourceSnapshot(ContractModel):
    source_ids: list[str]
    current_jobs: int = Field(ge=0)
    active_jobs: int = Field(ge=0)
    inactive_jobs: int = Field(ge=0)
    source_job_counts: dict[str, int]
    duplicate_identity_hashes: int = Field(ge=0)
    duplicate_external_job_keys: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> "Phase7D2BSourceSnapshot":
        if any(value < 0 for value in self.source_job_counts.values()):
            raise ValueError("Per-source snapshot counts cannot be negative")
        if self.active_jobs + self.inactive_jobs != self.current_jobs:
            raise ValueError("Active and inactive snapshot counts do not reconcile")
        if sum(self.source_job_counts.values()) != self.current_jobs:
            raise ValueError("Per-source snapshot counts do not reconcile")
        if set(self.source_job_counts) != set(self.source_ids):
            raise ValueError("Snapshot source partition is inconsistent")
        return self


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
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Required {label} does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ProductionCanaryError(f"{label} contains invalid JSON: {source}") from exc
    if not isinstance(payload, dict):
        raise ProductionCanaryError(f"{label} must contain a JSON object")
    return payload


def _verify_checksum(payload: Mapping[str, Any], *, field: str, label: str) -> str:
    stored = str(payload.get(field) or "").strip().lower()
    unsigned = dict(payload)
    unsigned.pop(field, None)
    if not stored or stored != _canonical_sha256(unsigned):
        raise ProductionCanaryError(f"{label} checksum is missing or invalid")
    return stored


def _ids(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ProductionCanaryError(f"{field} must be a JSON array")
    values = [str(item or "").strip() for item in value]
    if any(not item for item in values) or len(values) != len(set(values)):
        raise ProductionCanaryError(f"{field} contains invalid or duplicate source ids")
    return values


def _integer(value: Any, *, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _result_map(value: Any, *, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise ProductionCanaryError(f"{label} must be a JSON array")
    results: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ProductionCanaryError(f"{label} contains a non-object result")
        source_id = str(item.get("source_id") or "").strip()
        if not source_id or source_id in results:
            raise ProductionCanaryError(f"{label} contains an invalid source identity")
        results[source_id] = item
    return results


def require_phase7d2b_write_confirmation(value: str) -> None:
    if str(value or "") != PHASE_7D2B_WRITE_CONFIRMATION:
        raise ProductionCanaryError(
            "Phase 7D2B writes require --confirm-production-writes "
            + PHASE_7D2B_WRITE_CONFIRMATION
        )


def load_phase7d2b_write_authorization(
    *,
    cohort_path: Path,
    quality_report_path: Path,
    plan_path: Path,
    pilot_report_path: Path,
    pilot_manifest_path: Path,
    expected_inventory: int = 102,
    expected_cohort_size: int = 26,
    expected_source_count: int = 2,
) -> tuple[Phase6AIngestionPlan, Phase7D2BWriteAuthorization]:
    """Authorize writes only from a clean, linked Phase 7D2A pilot."""

    plan, selection = load_phase7d2a_pilot_inputs(
        cohort_path=cohort_path,
        quality_report_path=quality_report_path,
        plan_path=plan_path,
        expected_inventory=expected_inventory,
        expected_cohort_size=expected_cohort_size,
        expected_new_sources=expected_source_count,
    )
    report_path = Path(pilot_report_path).resolve()
    manifest_path = Path(pilot_manifest_path).resolve()
    report = _load_json(report_path, label="Phase 7D2A pilot report")
    manifest = _load_json(manifest_path, label="Phase 7D2A run manifest")
    report_sha256 = _verify_checksum(
        report,
        field="report_sha256",
        label="Phase 7D2A pilot report",
    )

    if (
        str(report.get("contract_version") or ""),
        str(report.get("phase") or ""),
        str(report.get("status") or ""),
        report.get("ready_for_phase7d2b"),
    ) != ("1.0", "7D2A", "passed", True):
        raise ProductionCanaryError("Phase 7D2A did not authorize Phase 7D2B")
    if report.get("blockers") != []:
        raise ProductionCanaryError("Phase 7D2A report contains rollout blockers")
    if str(report.get("plan_id") or "") != selection.plan_id:
        raise ProductionCanaryError("Pilot report references a different production plan")
    if str(report.get("cohort_sha256") or "") != selection.cohort_sha256:
        raise ProductionCanaryError("Pilot report references a different frozen cohort")
    if str(report.get("quality_report_sha256") or "") != selection.quality_report_sha256:
        raise ProductionCanaryError("Pilot report references a different quality report")
    report_source_ids = _ids(report.get("pilot_source_ids"), field="pilot_source_ids")
    if report_source_ids != selection.source_ids:
        raise ProductionCanaryError("Pilot report source selection is inconsistent")

    required_safety = {
        "database_write_guard_enabled": True,
        "index_creation_performed": False,
        "normalized_job_writes_enabled": False,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
        "full_cohort_execution_performed": False,
        "anti_bot_bypass_enabled": False,
    }
    safety = report.get("safety")
    if not isinstance(safety, dict):
        raise ProductionCanaryError("Phase 7D2A safety evidence is missing")
    for key, expected in required_safety.items():
        if safety.get(key) is not expected:
            raise ProductionCanaryError(f"Unsafe Phase 7D2A safety control: {key}")

    if (
        str(manifest.get("contract_version") or ""),
        str(manifest.get("phase") or ""),
        str(manifest.get("execution_mode") or ""),
    ) != ("1.0", "6E", "dry_run"):
        raise ProductionCanaryError("Phase 7D2A manifest is not a dry-run manifest")
    pilot_run_id = str(report.get("run_id") or "").strip()
    if not pilot_run_id or str(manifest.get("run_id") or "") != pilot_run_id:
        raise ProductionCanaryError("Pilot report and manifest run ids differ")
    if str(manifest.get("plan_id") or "") != selection.plan_id:
        raise ProductionCanaryError("Pilot manifest references a different production plan")
    if str(manifest.get("cohort_sha256") or "") != selection.cohort_sha256:
        raise ProductionCanaryError("Pilot manifest references a different frozen cohort")
    manifest_source_ids = _ids(
        manifest.get("selected_source_ids"),
        field="manifest.selected_source_ids",
    )
    if manifest_source_ids != selection.source_ids:
        raise ProductionCanaryError("Pilot manifest source selection is inconsistent")

    for key, expected in {
        "requested_source_count": expected_source_count,
        "completed_source_count": expected_source_count,
        "successful_source_count": expected_source_count,
        "failed_source_count": 0,
        "blocked_source_count": 0,
        "cancelled_source_count": 0,
        "quarantined_job_count": 0,
    }.items():
        if _integer(manifest.get(key)) != expected:
            raise ProductionCanaryError(f"Pilot manifest count {key} is inconsistent")
    accepted = _integer(manifest.get("accepted_job_count"))
    if accepted < expected_source_count:
        raise ProductionCanaryError("Pilot manifest accepted too few jobs")
    if _integer(manifest.get("inserted_job_count")) != accepted:
        raise ProductionCanaryError("Pilot did not preview every accepted job as an insert")
    for key in ("updated_job_count", "unchanged_job_count", "reactivated_job_count"):
        if _integer(manifest.get(key)) != 0:
            raise ProductionCanaryError(f"Unexpected Phase 7D2A preview outcome: {key}")

    preview = report.get("preview_counts")
    if not isinstance(preview, dict):
        raise ProductionCanaryError("Pilot preview counts are missing")
    preview_mapping = {
        "extracted": "extracted_job_count",
        "accepted": "accepted_job_count",
        "quarantined": "quarantined_job_count",
        "would_insert": "inserted_job_count",
        "would_update": "updated_job_count",
        "would_reactivate": "reactivated_job_count",
        "unchanged": "unchanged_job_count",
    }
    for report_key, manifest_key in preview_mapping.items():
        if _integer(preview.get(report_key)) != _integer(manifest.get(manifest_key)):
            raise ProductionCanaryError(
                f"Pilot report and manifest count differ: {report_key}"
            )

    manifest_controls = manifest.get("controls")
    if not isinstance(manifest_controls, dict):
        raise ProductionCanaryError("Pilot manifest controls are missing")
    for key, expected in {
        "normalized_job_writes_enabled": False,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
        "max_source_concurrency": 1,
        "max_attempts": 1,
        "max_jobs_per_source": 10,
        "bounded_pilot_execution": True,
    }.items():
        if manifest_controls.get(key) != expected:
            raise ProductionCanaryError(f"Unsafe or inconsistent pilot control: {key}")

    manifest_results = _result_map(
        manifest.get("source_results"),
        label="pilot_manifest.source_results",
    )
    summarized_results = _result_map(
        report.get("source_results"),
        label="pilot_report.source_results",
    )
    if set(manifest_results) != set(selection.source_ids) or set(summarized_results) != set(
        selection.source_ids
    ):
        raise ProductionCanaryError("Pilot source-result partition is inconsistent")
    for source_id in selection.source_ids:
        detailed = manifest_results[source_id]
        summarized = summarized_results[source_id]
        attempts = detailed.get("attempts")
        if (
            detailed.get("status") != "success"
            or summarized.get("status") != "success"
            or not isinstance(attempts, list)
            or len(attempts) != 1
            or _integer(summarized.get("attempts")) != 1
            or _integer(detailed.get("accepted_count")) < 1
            or _integer(detailed.get("quarantined_count")) != 0
            or _integer(detailed.get("rejected_count")) != 0
            or detailed.get("error_type") is not None
            or summarized.get("error_type") is not None
        ):
            raise ProductionCanaryError(f"Pilot source was not clean: {source_id}")
        for key in (
            "discovered_count",
            "extracted_count",
            "accepted_count",
            "quarantined_count",
            "rejected_count",
        ):
            if _integer(summarized.get(key)) != _integer(detailed.get(key)):
                raise ProductionCanaryError(
                    f"Pilot source summary differs for {source_id}: {key}"
                )
    if sum(_integer(result.get("accepted_count"), default=0) for result in manifest_results.values()) != accepted:
        raise ProductionCanaryError("Pilot accepted-job source totals do not reconcile")
    if sum(_integer(result.get("extracted_count"), default=0) for result in manifest_results.values()) != _integer(
        manifest.get("extracted_job_count")
    ):
        raise ProductionCanaryError("Pilot extracted-job source totals do not reconcile")

    return plan, Phase7D2BWriteAuthorization(
        plan_id=selection.plan_id,
        cohort_sha256=selection.cohort_sha256,
        quality_report_sha256=selection.quality_report_sha256,
        pilot_report_sha256=report_sha256,
        pilot_manifest_sha256=_file_sha256(manifest_path),
        pilot_run_id=pilot_run_id,
        source_ids=selection.source_ids,
        expected_source_count=expected_source_count,
    )


def _duplicate_groups(values: list[Any]) -> int:
    counts = Counter(value for value in values if value not in (None, ""))
    return sum(1 for count in counts.values() if count > 1)


def capture_phase7d2b_source_snapshot(
    database: Any,
    *,
    source_ids: list[str],
) -> Phase7D2BSourceSnapshot:
    documents = list(database["jobs_current"].find({"source_id": {"$in": source_ids}}))
    source_counts = {source_id: 0 for source_id in source_ids}
    for document in documents:
        source_id = str(document.get("source_id") or "")
        if source_id in source_counts:
            source_counts[source_id] += 1
    return Phase7D2BSourceSnapshot(
        source_ids=source_ids,
        current_jobs=len(documents),
        active_jobs=sum(1 for document in documents if document.get("is_active") is True),
        inactive_jobs=sum(1 for document in documents if document.get("is_active") is not True),
        source_job_counts=source_counts,
        duplicate_identity_hashes=_duplicate_groups(
            [document.get("identity_hash") for document in documents]
        ),
        duplicate_external_job_keys=_duplicate_groups(
            [
                (document.get("source_id"), document.get("external_job_id"))
                for document in documents
                if document.get("external_job_id")
            ]
        ),
    )


def require_empty_phase7d2b_source_scope(snapshot: Phase7D2BSourceSnapshot) -> None:
    if snapshot.current_jobs != 0:
        raise ProductionCanaryError(
            "PHASE_7D2B_SOURCE_SCOPE_NOT_EMPTY: the first-write canary cannot be rerun; "
            "use the Phase 7D2C idempotency path"
        )


def build_phase7d2b_write_report(
    *,
    authorization: Phase7D2BWriteAuthorization,
    manifest: Mapping[str, Any],
    database: Any,
    before: Phase7D2BSourceSnapshot,
    index_definitions_ensured: Mapping[str, int],
) -> dict[str, Any]:
    blockers: list[str] = []

    def block(reason: str) -> None:
        if reason not in blockers:
            blockers.append(reason)

    source_ids = authorization.source_ids
    after = capture_phase7d2b_source_snapshot(database, source_ids=source_ids)
    selected = [str(value or "") for value in manifest.get("selected_source_ids") or []]
    results = manifest.get("source_results")
    if not isinstance(results, list):
        results = []
        block("source_results_missing")

    if before.current_jobs != 0:
        block("source_scope_was_not_empty_before_write")
    if str(manifest.get("execution_mode") or "") != "write":
        block("execution_mode_not_write")
    if str(manifest.get("plan_id") or "") != authorization.plan_id:
        block("plan_id_mismatch")
    if str(manifest.get("cohort_sha256") or "") != authorization.cohort_sha256:
        block("cohort_sha256_mismatch")
    if selected != source_ids:
        block("write_source_selection_mismatch")
    for key, expected in {
        "requested_source_count": authorization.expected_source_count,
        "completed_source_count": authorization.expected_source_count,
        "successful_source_count": authorization.expected_source_count,
        "failed_source_count": 0,
        "blocked_source_count": 0,
        "cancelled_source_count": 0,
        "quarantined_job_count": 0,
    }.items():
        if _integer(manifest.get(key)) != expected:
            block(f"inconsistent_manifest_count:{key}")

    accepted = _integer(manifest.get("accepted_job_count"))
    inserted = _integer(manifest.get("inserted_job_count"))
    if accepted < authorization.expected_source_count:
        block("write_accepted_too_few_jobs")
    if inserted != accepted:
        block("first_write_did_not_insert_every_accepted_job")
    for key in ("updated_job_count", "unchanged_job_count", "reactivated_job_count"):
        if _integer(manifest.get(key)) != 0:
            block(f"unexpected_first_write_outcome:{key}")

    controls = manifest.get("controls")
    if not isinstance(controls, dict):
        controls = {}
        block("manifest_controls_missing")
    for key, expected in {
        "normalized_job_writes_enabled": True,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
        "max_source_concurrency": 1,
        "max_attempts": 1,
        "max_jobs_per_source": 10,
        "bounded_pilot_execution": True,
    }.items():
        if controls.get(key) != expected:
            block(f"unsafe_or_inconsistent_manifest_control:{key}")

    by_id: dict[str, Mapping[str, Any]] = {}
    for value in results:
        if not isinstance(value, Mapping):
            block("invalid_source_result")
            continue
        source_id = str(value.get("source_id") or "")
        if not source_id or source_id in by_id:
            block("duplicate_or_missing_source_result_id")
            continue
        by_id[source_id] = value
    if set(by_id) != set(source_ids):
        block("source_result_partition_mismatch")

    source_summaries: list[dict[str, Any]] = []
    source_total_counts = Counter()
    for source_id in source_ids:
        result = by_id.get(source_id, {})
        attempts = result.get("attempts") if isinstance(result.get("attempts"), list) else []
        source_accepted = _integer(result.get("accepted_count"), default=0)
        source_inserted = _integer(result.get("inserted_count"), default=0)
        source_quarantined = _integer(result.get("quarantined_count"), default=0)
        source_rejected = _integer(result.get("rejected_count"), default=0)
        if result.get("status") != "success":
            block(f"source_not_successful:{source_id}")
        if len(attempts) != 1:
            block(f"source_attempt_count_not_one:{source_id}")
        if source_accepted < 1 or source_inserted != source_accepted:
            block(f"source_first_write_not_clean:{source_id}")
        if source_quarantined or source_rejected:
            block(f"source_has_quality_rejections:{source_id}")
        for key in ("updated_count", "unchanged_count", "reactivated_count"):
            if _integer(result.get(key), default=0) != 0:
                block(f"source_has_unexpected_outcome:{source_id}:{key}")
        source_total_counts.update(
            {
                "accepted": source_accepted,
                "inserted": source_inserted,
                "quarantined": source_quarantined,
                "extracted": _integer(result.get("extracted_count"), default=0),
            }
        )
        source_summaries.append(
            {
                "source_id": source_id,
                "status": str(result.get("status") or "missing"),
                "source_run_id": result.get("source_run_id"),
                "attempts": len(attempts),
                "discovered": _integer(result.get("discovered_count"), default=0),
                "extracted": _integer(result.get("extracted_count"), default=0),
                "accepted": source_accepted,
                "inserted": source_inserted,
                "quarantined": source_quarantined,
                "rejected": source_rejected,
            }
        )
    for name, aggregate in (
        ("accepted", accepted),
        ("inserted", inserted),
        ("quarantined", _integer(manifest.get("quarantined_job_count"), default=0)),
        ("extracted", _integer(manifest.get("extracted_job_count"), default=0)),
    ):
        if source_total_counts[name] != aggregate:
            block(f"source_aggregate_count_mismatch:{name}")

    required_index_collections = {
        "production_ingestion_fleet_runs",
        "production_ingestion_source_runs",
        "production_raw_job_evidence",
        "production_job_quarantine",
        "jobs_current",
        "jobs_history",
    }
    if set(index_definitions_ensured) != required_index_collections or any(
        _integer(value, default=0) < 1 for value in index_definitions_ensured.values()
    ):
        block("required_index_definitions_not_ensured")

    run_id = str(manifest.get("run_id") or "")
    fleet_records = list(
        database["production_ingestion_fleet_runs"].find({"fleet_run_id": run_id})
    )
    source_records = list(
        database["production_ingestion_source_runs"].find({"fleet_run_id": run_id})
    )
    raw_evidence_count = int(
        database["production_raw_job_evidence"].count_documents({"fleet_run_id": run_id})
    )
    quarantine_count = int(
        database["production_job_quarantine"].count_documents({"fleet_run_id": run_id})
    )
    touched_jobs = list(
        database["jobs_current"].find(
            {"source_id": {"$in": source_ids}, "last_fleet_run_id": run_id}
        )
    )
    history_mismatches = 0
    for document in touched_jobs:
        job_id = str(document.get("job_id") or "")
        version = _integer(document.get("version"), default=0)
        histories = int(database["jobs_history"].count_documents({"job_id": job_id}))
        if not job_id or version < 1 or histories != version:
            history_mismatches += 1
    unsafe_deactivated = sum(
        1
        for document in touched_jobs
        if document.get("is_active") is not True or document.get("deactivated_at") is not None
    )

    if len(fleet_records) != 1 or fleet_records[0].get("status") != "completed":
        block("fleet_audit_record_not_completed")
    if len(source_records) != authorization.expected_source_count:
        block("source_audit_record_count_mismatch")
    if any(record.get("status") != "success" for record in source_records):
        block("source_audit_record_not_successful")
    if {str(record.get("source_id") or "") for record in source_records} != set(source_ids):
        block("source_audit_partition_mismatch")
    manifest_source_run_ids = {
        str(result.get("source_run_id") or "") for result in by_id.values()
    }
    audit_source_run_ids = {
        str(record.get("source_run_id") or "") for record in source_records
    }
    if "" in manifest_source_run_ids or manifest_source_run_ids != audit_source_run_ids:
        block("source_run_identity_mismatch")
    if raw_evidence_count != accepted:
        block("raw_evidence_count_mismatch")
    if quarantine_count != 0:
        block("write_created_quarantine_records")
    if len(touched_jobs) != accepted:
        block("touched_job_count_mismatch")
    if history_mismatches:
        block("current_history_version_mismatch")
    if unsafe_deactivated:
        block("write_created_inactive_or_deactivated_jobs")

    if after.current_jobs != before.current_jobs + inserted:
        block("jobs_current_delta_mismatch")
    if after.active_jobs != after.current_jobs or after.inactive_jobs != 0:
        block("source_scope_contains_inactive_jobs")
    if any(after.source_job_counts.get(source_id, 0) < 1 for source_id in source_ids):
        block("one_or_more_sources_have_no_persisted_jobs")
    if after.duplicate_identity_hashes:
        block("duplicate_identity_hashes_detected")
    if after.duplicate_external_job_keys:
        block("duplicate_external_job_keys_detected")

    report: dict[str, Any] = {
        "contract_version": PHASE_7D2B_CONTRACT_VERSION,
        "phase": PHASE_7D2B,
        "status": "passed" if not blockers else "failed",
        "ready_for_phase7d2c": not blockers,
        "run_id": manifest.get("run_id"),
        "generated_at": manifest.get("generated_at"),
        "plan_id": authorization.plan_id,
        "cohort_sha256": authorization.cohort_sha256,
        "quality_report_sha256": authorization.quality_report_sha256,
        "pilot_report_sha256": authorization.pilot_report_sha256,
        "pilot_manifest_sha256": authorization.pilot_manifest_sha256,
        "pilot_run_id": authorization.pilot_run_id,
        "source_ids": source_ids,
        "source_results": source_summaries,
        "before": before.model_dump(mode="json"),
        "after": after.model_dump(mode="json"),
        "database_checks": {
            "fleet_run_records": len(fleet_records),
            "source_run_records": len(source_records),
            "raw_evidence_records": raw_evidence_count,
            "quarantine_records": quarantine_count,
            "touched_current_jobs": len(touched_jobs),
            "history_mismatches": history_mismatches,
            "unsafe_deactivated_jobs": unsafe_deactivated,
        },
        "write_counts": {
            "extracted": _integer(manifest.get("extracted_job_count"), default=0),
            "accepted": accepted,
            "inserted": inserted,
            "updated": _integer(manifest.get("updated_job_count"), default=0),
            "unchanged": _integer(manifest.get("unchanged_job_count"), default=0),
            "reactivated": _integer(manifest.get("reactivated_job_count"), default=0),
            "quarantined": _integer(manifest.get("quarantined_job_count"), default=0),
        },
        "index_definitions_ensured": dict(index_definitions_ensured),
        "controls": {
            "explicit_write_confirmation_required": True,
            "normalized_job_writes_enabled": True,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "full_cohort_execution_performed": False,
            "anti_bot_bypass_enabled": False,
            "automatic_rollback_enabled": False,
        },
        "blockers": blockers,
    }
    report["report_sha256"] = _canonical_sha256(report)
    return report


def write_phase7d2b_report(path: Path, report: Mapping[str, Any]) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(report), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, target)
    return target

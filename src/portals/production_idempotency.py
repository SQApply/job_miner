from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import Field, model_validator

from .contracts import ContractModel
from .production_canary import (
    Phase7D2BSourceSnapshot,
    capture_phase7d2b_source_snapshot,
    load_phase7d2b_write_authorization,
)
from .production_ingestion import Phase6AIngestionPlan


PHASE_7D2C_CONTRACT_VERSION = "1.0"
PHASE_7D2C = "7D2C"
PHASE_7D2C_WRITE_CONFIRMATION = "ENABLE_PHASE_7D2C_IDEMPOTENCY_RERUN"


class ProductionIdempotencyError(RuntimeError):
    """Raised when the Phase 7D2C rerun is unsafe or non-idempotent."""


class Phase7D2CIdempotencyAuthorization(ContractModel):
    contract_version: Literal["1.0"] = PHASE_7D2C_CONTRACT_VERSION
    phase: Literal["7D2C"] = PHASE_7D2C
    plan_id: str = Field(min_length=1)
    cohort_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    phase7d2b_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    first_write_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    first_write_run_id: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    expected_source_count: int = Field(ge=1)
    expected_job_count: int = Field(ge=1)
    expected_source_job_counts: dict[str, int]

    @model_validator(mode="after")
    def validate_authorization(self) -> "Phase7D2CIdempotencyAuthorization":
        if len(self.source_ids) != self.expected_source_count:
            raise ValueError("Idempotency source count is inconsistent")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("Idempotency source ids must be unique")
        if set(self.expected_source_job_counts) != set(self.source_ids):
            raise ValueError("Expected source-job partition is inconsistent")
        if any(value < 1 for value in self.expected_source_job_counts.values()):
            raise ValueError("Every idempotency source must contain at least one job")
        if sum(self.expected_source_job_counts.values()) != self.expected_job_count:
            raise ValueError("Expected source-job counts do not reconcile")
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
        raise ProductionIdempotencyError(f"{label} contains invalid JSON: {source}") from exc
    if not isinstance(payload, dict):
        raise ProductionIdempotencyError(f"{label} must contain a JSON object")
    return payload


def _verify_checksum(payload: Mapping[str, Any], *, field: str, label: str) -> str:
    stored = str(payload.get(field) or "").strip().lower()
    unsigned = dict(payload)
    unsigned.pop(field, None)
    if not stored or stored != _canonical_sha256(unsigned):
        raise ProductionIdempotencyError(f"{label} checksum is missing or invalid")
    return stored


def _integer(value: Any, *, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ids(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ProductionIdempotencyError(f"{field} must be a JSON array")
    values = [str(item or "").strip() for item in value]
    if any(not item for item in values) or len(values) != len(set(values)):
        raise ProductionIdempotencyError(f"{field} contains invalid source ids")
    return values


def _result_map(value: Any, *, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise ProductionIdempotencyError(f"{label} must be a JSON array")
    results: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ProductionIdempotencyError(f"{label} contains a non-object result")
        source_id = str(item.get("source_id") or "").strip()
        if not source_id or source_id in results:
            raise ProductionIdempotencyError(f"{label} contains an invalid source identity")
        results[source_id] = item
    return results


def require_phase7d2c_write_confirmation(value: str) -> None:
    if str(value or "") != PHASE_7D2C_WRITE_CONFIRMATION:
        raise ProductionIdempotencyError(
            "Phase 7D2C writes require --confirm-idempotency-rerun "
            + PHASE_7D2C_WRITE_CONFIRMATION
        )


def load_phase7d2c_authorization(
    *,
    cohort_path: Path,
    quality_report_path: Path,
    plan_path: Path,
    pilot_report_path: Path,
    pilot_manifest_path: Path,
    first_write_report_path: Path,
    first_write_manifest_path: Path,
    expected_inventory: int = 102,
    expected_cohort_size: int = 26,
    expected_source_count: int = 2,
) -> tuple[Phase6AIngestionPlan, Phase7D2CIdempotencyAuthorization]:
    plan, canary = load_phase7d2b_write_authorization(
        cohort_path=cohort_path,
        quality_report_path=quality_report_path,
        plan_path=plan_path,
        pilot_report_path=pilot_report_path,
        pilot_manifest_path=pilot_manifest_path,
        expected_inventory=expected_inventory,
        expected_cohort_size=expected_cohort_size,
        expected_source_count=expected_source_count,
    )
    report_path = Path(first_write_report_path).resolve()
    manifest_path = Path(first_write_manifest_path).resolve()
    report = _load_json(report_path, label="Phase 7D2B write report")
    manifest = _load_json(manifest_path, label="Phase 7D2B first-write manifest")
    report_sha256 = _verify_checksum(
        report,
        field="report_sha256",
        label="Phase 7D2B write report",
    )

    if (
        str(report.get("contract_version") or ""),
        str(report.get("phase") or ""),
        str(report.get("status") or ""),
        report.get("ready_for_phase7d2c"),
    ) != ("1.0", "7D2B", "passed", True):
        raise ProductionIdempotencyError("Phase 7D2B did not authorize Phase 7D2C")
    if report.get("blockers") != []:
        raise ProductionIdempotencyError("Phase 7D2B report contains blockers")
    for key, expected in {
        "plan_id": canary.plan_id,
        "cohort_sha256": canary.cohort_sha256,
        "quality_report_sha256": canary.quality_report_sha256,
        "pilot_report_sha256": canary.pilot_report_sha256,
        "pilot_manifest_sha256": canary.pilot_manifest_sha256,
        "pilot_run_id": canary.pilot_run_id,
    }.items():
        if str(report.get(key) or "") != expected:
            raise ProductionIdempotencyError(f"Phase 7D2B evidence mismatch: {key}")
    report_source_ids = _ids(report.get("source_ids"), field="phase7d2b.source_ids")
    if report_source_ids != canary.source_ids:
        raise ProductionIdempotencyError("Phase 7D2B source selection is inconsistent")

    controls = report.get("controls")
    if not isinstance(controls, dict):
        raise ProductionIdempotencyError("Phase 7D2B controls are missing")
    for key, expected in {
        "explicit_write_confirmation_required": True,
        "normalized_job_writes_enabled": True,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
        "full_cohort_execution_performed": False,
        "anti_bot_bypass_enabled": False,
        "automatic_rollback_enabled": False,
    }.items():
        if controls.get(key) is not expected:
            raise ProductionIdempotencyError(f"Unsafe Phase 7D2B control: {key}")

    before = report.get("before")
    after = report.get("after")
    write_counts = report.get("write_counts")
    database_checks = report.get("database_checks")
    indexes = report.get("index_definitions_ensured")
    if not all(isinstance(value, dict) for value in (before, after, write_counts, database_checks, indexes)):
        raise ProductionIdempotencyError("Phase 7D2B accounting evidence is incomplete")
    if _integer(before.get("current_jobs")) != 0:
        raise ProductionIdempotencyError("Phase 7D2B was not a first-write run")
    expected_jobs = _integer(after.get("current_jobs"))
    source_counts = {
        str(key): _integer(value)
        for key, value in dict(after.get("source_job_counts") or {}).items()
    }
    if (
        expected_jobs < expected_source_count
        or _integer(after.get("active_jobs")) != expected_jobs
        or _integer(after.get("inactive_jobs")) != 0
        or _integer(after.get("duplicate_identity_hashes")) != 0
        or _integer(after.get("duplicate_external_job_keys")) != 0
        or set(source_counts) != set(canary.source_ids)
        or any(value < 1 for value in source_counts.values())
        or sum(source_counts.values()) != expected_jobs
    ):
        raise ProductionIdempotencyError("Phase 7D2B after-state is unsafe or inconsistent")
    if (
        _integer(write_counts.get("accepted")) != expected_jobs
        or _integer(write_counts.get("inserted")) != expected_jobs
        or any(
            _integer(write_counts.get(key)) != 0
            for key in ("updated", "unchanged", "reactivated", "quarantined")
        )
    ):
        raise ProductionIdempotencyError("Phase 7D2B write outcomes are inconsistent")
    for key, expected in {
        "fleet_run_records": 1,
        "source_run_records": expected_source_count,
        "raw_evidence_records": expected_jobs,
        "quarantine_records": 0,
        "touched_current_jobs": expected_jobs,
        "history_mismatches": 0,
        "unsafe_deactivated_jobs": 0,
    }.items():
        if _integer(database_checks.get(key)) != expected:
            raise ProductionIdempotencyError(f"Phase 7D2B database check failed: {key}")
    required_index_collections = {
        "production_ingestion_fleet_runs",
        "production_ingestion_source_runs",
        "production_raw_job_evidence",
        "production_job_quarantine",
        "jobs_current",
        "jobs_history",
    }
    if set(indexes) != required_index_collections or any(_integer(value, default=0) < 1 for value in indexes.values()):
        raise ProductionIdempotencyError("Phase 7D2B did not ensure required indexes")

    if (
        str(manifest.get("contract_version") or ""),
        str(manifest.get("phase") or ""),
        str(manifest.get("execution_mode") or ""),
    ) != ("1.0", "6E", "write"):
        raise ProductionIdempotencyError("Phase 7D2B manifest is not a write manifest")
    first_run_id = str(report.get("run_id") or "").strip()
    if not first_run_id or str(manifest.get("run_id") or "") != first_run_id:
        raise ProductionIdempotencyError("Phase 7D2B report and manifest run ids differ")
    if str(manifest.get("plan_id") or "") != canary.plan_id or str(
        manifest.get("cohort_sha256") or ""
    ) != canary.cohort_sha256:
        raise ProductionIdempotencyError("Phase 7D2B manifest plan/cohort mismatch")
    if _ids(manifest.get("selected_source_ids"), field="first_write.selected_source_ids") != canary.source_ids:
        raise ProductionIdempotencyError("Phase 7D2B manifest source selection differs")
    for key, expected in {
        "requested_source_count": expected_source_count,
        "completed_source_count": expected_source_count,
        "successful_source_count": expected_source_count,
        "failed_source_count": 0,
        "blocked_source_count": 0,
        "cancelled_source_count": 0,
        "accepted_job_count": expected_jobs,
        "inserted_job_count": expected_jobs,
        "updated_job_count": 0,
        "unchanged_job_count": 0,
        "reactivated_job_count": 0,
        "quarantined_job_count": 0,
    }.items():
        if _integer(manifest.get(key)) != expected:
            raise ProductionIdempotencyError(f"Phase 7D2B manifest count differs: {key}")
    manifest_controls = manifest.get("controls")
    if not isinstance(manifest_controls, dict):
        raise ProductionIdempotencyError("Phase 7D2B manifest controls are missing")
    for key, expected in {
        "normalized_job_writes_enabled": True,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
        "max_source_concurrency": 1,
        "max_attempts": 1,
        "max_jobs_per_source": 10,
        "bounded_pilot_execution": True,
    }.items():
        if manifest_controls.get(key) != expected:
            raise ProductionIdempotencyError(f"Unsafe first-write manifest control: {key}")

    report_results = _result_map(report.get("source_results"), label="phase7d2b.source_results")
    manifest_results = _result_map(manifest.get("source_results"), label="first_write.source_results")
    if set(report_results) != set(canary.source_ids) or set(manifest_results) != set(canary.source_ids):
        raise ProductionIdempotencyError("Phase 7D2B source-result partition differs")
    for source_id in canary.source_ids:
        summary = report_results[source_id]
        detail = manifest_results[source_id]
        attempts = detail.get("attempts")
        expected_source_jobs = source_counts[source_id]
        if (
            summary.get("status") != "success"
            or detail.get("status") != "success"
            or not isinstance(attempts, list)
            or len(attempts) != 1
            or _integer(summary.get("attempts")) != 1
            or _integer(summary.get("accepted")) != expected_source_jobs
            or _integer(summary.get("inserted")) != expected_source_jobs
            or _integer(detail.get("accepted_count")) != expected_source_jobs
            or _integer(detail.get("inserted_count")) != expected_source_jobs
            or any(
                _integer(detail.get(key)) != 0
                for key in ("updated_count", "unchanged_count", "reactivated_count", "quarantined_count", "rejected_count")
            )
        ):
            raise ProductionIdempotencyError(f"Phase 7D2B source outcome differs: {source_id}")

    return plan, Phase7D2CIdempotencyAuthorization(
        plan_id=canary.plan_id,
        cohort_sha256=canary.cohort_sha256,
        phase7d2b_report_sha256=report_sha256,
        first_write_manifest_sha256=_file_sha256(manifest_path),
        first_write_run_id=first_run_id,
        source_ids=canary.source_ids,
        expected_source_count=expected_source_count,
        expected_job_count=expected_jobs,
        expected_source_job_counts=source_counts,
    )


def require_phase7d2c_database_preflight(
    database: Any,
    *,
    authorization: Phase7D2CIdempotencyAuthorization,
) -> Phase7D2BSourceSnapshot:
    snapshot = capture_phase7d2b_source_snapshot(database, source_ids=authorization.source_ids)
    if (
        snapshot.current_jobs != authorization.expected_job_count
        or snapshot.active_jobs != authorization.expected_job_count
        or snapshot.inactive_jobs != 0
        or snapshot.source_job_counts != authorization.expected_source_job_counts
        or snapshot.duplicate_identity_hashes != 0
        or snapshot.duplicate_external_job_keys != 0
    ):
        raise ProductionIdempotencyError("PHASE_7D2C_DATABASE_STATE_MISMATCH")
    first_fleet = list(
        database["production_ingestion_fleet_runs"].find(
            {"fleet_run_id": authorization.first_write_run_id}
        )
    )
    if len(first_fleet) != 1 or first_fleet[0].get("status") != "completed":
        raise ProductionIdempotencyError("Phase 7D2B fleet audit record is missing")
    existing_reruns = [
        row
        for row in database["production_ingestion_fleet_runs"].find(
            {"plan_id": authorization.plan_id}
        )
        if str(row.get("fleet_run_id") or "").startswith("phase7d2c_")
    ]
    if existing_reruns:
        raise ProductionIdempotencyError(
            "PHASE_7D2C_ALREADY_EXECUTED: do not repeat the idempotency rerun"
        )
    source_runs = list(
        database["production_ingestion_source_runs"].find(
            {"fleet_run_id": authorization.first_write_run_id}
        )
    )
    if (
        len(source_runs) != authorization.expected_source_count
        or {str(row.get("source_id") or "") for row in source_runs} != set(authorization.source_ids)
        or any(row.get("status") != "success" for row in source_runs)
    ):
        raise ProductionIdempotencyError("Phase 7D2B source audit records are inconsistent")
    if int(
        database["production_raw_job_evidence"].count_documents(
            {"fleet_run_id": authorization.first_write_run_id}
        )
    ) != authorization.expected_job_count:
        raise ProductionIdempotencyError("Phase 7D2B raw evidence count is inconsistent")
    if int(
        database["production_job_quarantine"].count_documents(
            {"fleet_run_id": authorization.first_write_run_id}
        )
    ) != 0:
        raise ProductionIdempotencyError("Phase 7D2B contains quarantine records")
    touched = list(
        database["jobs_current"].find(
            {
                "source_id": {"$in": authorization.source_ids},
                "last_fleet_run_id": authorization.first_write_run_id,
            }
        )
    )
    if len(touched) != authorization.expected_job_count:
        raise ProductionIdempotencyError("Phase 7D2B current-job linkage is inconsistent")
    for document in touched:
        job_id = str(document.get("job_id") or "")
        version = _integer(document.get("version"), default=0)
        histories = int(database["jobs_history"].count_documents({"job_id": job_id}))
        if not job_id or version != 1 or histories != 1:
            raise ProductionIdempotencyError("Phase 7D2B history state is not first-write clean")
    return snapshot


def build_phase7d2c_report(
    *,
    authorization: Phase7D2CIdempotencyAuthorization,
    rerun_manifest: Mapping[str, Any],
    rerun_manifest_path: Path,
    closeout_report: Mapping[str, Any],
    closeout_report_path: Path,
    database: Any,
    before: Phase7D2BSourceSnapshot,
) -> dict[str, Any]:
    blockers: list[str] = []

    def block(reason: str) -> None:
        if reason not in blockers:
            blockers.append(reason)

    after = capture_phase7d2b_source_snapshot(database, source_ids=authorization.source_ids)
    if before.current_jobs != authorization.expected_job_count:
        block("rerun_before_job_count_mismatch")
    if before.source_job_counts != authorization.expected_source_job_counts:
        block("rerun_before_source_partition_mismatch")
    if str(rerun_manifest.get("execution_mode") or "") != "write":
        block("rerun_execution_mode_not_write")
    if str(rerun_manifest.get("plan_id") or "") != authorization.plan_id:
        block("rerun_plan_id_mismatch")
    if str(rerun_manifest.get("cohort_sha256") or "") != authorization.cohort_sha256:
        block("rerun_cohort_sha256_mismatch")
    selected = [str(value or "") for value in rerun_manifest.get("selected_source_ids") or []]
    if selected != authorization.source_ids:
        block("rerun_source_selection_mismatch")
    for key, expected in {
        "requested_source_count": authorization.expected_source_count,
        "completed_source_count": authorization.expected_source_count,
        "successful_source_count": authorization.expected_source_count,
        "failed_source_count": 0,
        "blocked_source_count": 0,
        "cancelled_source_count": 0,
        "accepted_job_count": authorization.expected_job_count,
        "inserted_job_count": 0,
        "updated_job_count": 0,
        "unchanged_job_count": authorization.expected_job_count,
        "reactivated_job_count": 0,
        "quarantined_job_count": 0,
    }.items():
        if _integer(rerun_manifest.get(key)) != expected:
            block(f"rerun_manifest_count_mismatch:{key}")

    controls = rerun_manifest.get("controls")
    if not isinstance(controls, dict):
        controls = {}
        block("rerun_controls_missing")
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
            block(f"unsafe_or_inconsistent_rerun_control:{key}")

    try:
        results = _result_map(rerun_manifest.get("source_results"), label="rerun.source_results")
    except ProductionIdempotencyError as exc:
        results = {}
        block(str(exc))
    if set(results) != set(authorization.source_ids):
        block("rerun_source_result_partition_mismatch")
    summaries: list[dict[str, Any]] = []
    for source_id in authorization.source_ids:
        result = results.get(source_id, {})
        attempts = result.get("attempts") if isinstance(result.get("attempts"), list) else []
        expected_jobs = authorization.expected_source_job_counts[source_id]
        if result.get("status") != "success":
            block(f"rerun_source_not_successful:{source_id}")
        if len(attempts) != 1:
            block(f"rerun_source_attempt_count_not_one:{source_id}")
        for key, expected in {
            "accepted_count": expected_jobs,
            "inserted_count": 0,
            "updated_count": 0,
            "unchanged_count": expected_jobs,
            "reactivated_count": 0,
            "quarantined_count": 0,
            "rejected_count": 0,
        }.items():
            if _integer(result.get(key)) != expected:
                block(f"rerun_source_count_mismatch:{source_id}:{key}")
        summaries.append(
            {
                "source_id": source_id,
                "status": str(result.get("status") or "missing"),
                "source_run_id": result.get("source_run_id"),
                "attempts": len(attempts),
                "accepted": _integer(result.get("accepted_count"), default=0),
                "inserted": _integer(result.get("inserted_count"), default=0),
                "updated": _integer(result.get("updated_count"), default=0),
                "unchanged": _integer(result.get("unchanged_count"), default=0),
                "quarantined": _integer(result.get("quarantined_count"), default=0),
            }
        )

    if (
        after.current_jobs != authorization.expected_job_count
        or after.active_jobs != authorization.expected_job_count
        or after.inactive_jobs != 0
        or after.source_job_counts != authorization.expected_source_job_counts
    ):
        block("rerun_changed_the_source_job_partition")
    if after.duplicate_identity_hashes:
        block("rerun_created_duplicate_identity_hashes")
    if after.duplicate_external_job_keys:
        block("rerun_created_duplicate_external_job_keys")

    closeout = dict(closeout_report)
    if closeout.get("status") != "passed" or closeout.get("ready_for_phase7") is not True:
        block("phase6f_closeout_did_not_pass")
    if closeout.get("issues") != []:
        block("phase6f_closeout_contains_issues")
    if str(closeout.get("plan_id") or "") != authorization.plan_id:
        block("phase6f_closeout_plan_mismatch")
    if str(closeout.get("cohort_sha256") or "") != authorization.cohort_sha256:
        block("phase6f_closeout_cohort_mismatch")
    if _integer(closeout.get("expected_source_count")) != authorization.expected_source_count:
        block("phase6f_closeout_source_count_mismatch")
    if [str(value or "") for value in closeout.get("selected_source_ids") or []] != authorization.source_ids:
        block("phase6f_closeout_source_selection_mismatch")
    if str(closeout.get("first_write_run_id") or "") != authorization.first_write_run_id:
        block("phase6f_closeout_first_run_mismatch")
    if str(closeout.get("rerun_run_id") or "") != str(rerun_manifest.get("run_id") or ""):
        block("phase6f_closeout_rerun_mismatch")
    if str(closeout.get("first_write_manifest_sha256") or "") != authorization.first_write_manifest_sha256:
        block("phase6f_closeout_first_manifest_hash_mismatch")
    rerun_manifest_sha256 = _file_sha256(Path(rerun_manifest_path).resolve())
    if str(closeout.get("rerun_manifest_sha256") or "") != rerun_manifest_sha256:
        block("phase6f_closeout_rerun_manifest_hash_mismatch")
    closeout_controls = closeout.get("controls")
    if not isinstance(closeout_controls, dict) or closeout_controls.get(
        "lifecycle_reconciliation_enabled"
    ) is not False or closeout_controls.get("deactivation_enabled") is not False:
        block("phase6f_closeout_controls_unsafe")

    checks = closeout.get("database_checks")
    if not isinstance(checks, dict):
        checks = {}
        block("phase6f_database_checks_missing")
    for key, expected in {
        "fleet_run_records": 2,
        "source_run_records_first": authorization.expected_source_count,
        "source_run_records_rerun": authorization.expected_source_count,
        "current_jobs_observed_on_rerun": authorization.expected_job_count,
        "active_jobs_observed_on_rerun": authorization.expected_job_count,
        "duplicate_identity_hashes": 0,
        "duplicate_external_job_keys": 0,
        "history_mismatches": 0,
        "unsafe_deactivated_jobs": 0,
        "quarantine_records_first": 0,
        "quarantine_records_rerun": 0,
    }.items():
        if _integer(checks.get(key)) != expected:
            block(f"phase6f_database_check_mismatch:{key}")

    report: dict[str, Any] = {
        "contract_version": PHASE_7D2C_CONTRACT_VERSION,
        "phase": PHASE_7D2C,
        "status": "passed" if not blockers else "failed",
        "ready_for_phase7d3": not blockers,
        "run_id": rerun_manifest.get("run_id"),
        "generated_at": rerun_manifest.get("generated_at"),
        "plan_id": authorization.plan_id,
        "cohort_sha256": authorization.cohort_sha256,
        "phase7d2b_report_sha256": authorization.phase7d2b_report_sha256,
        "first_write_manifest_sha256": authorization.first_write_manifest_sha256,
        "first_write_run_id": authorization.first_write_run_id,
        "rerun_manifest_sha256": rerun_manifest_sha256,
        "phase6f_closeout_sha256": _file_sha256(Path(closeout_report_path).resolve()),
        "source_ids": authorization.source_ids,
        "source_results": summaries,
        "before": before.model_dump(mode="json"),
        "after": after.model_dump(mode="json"),
        "idempotency_counts": {
            "expected_jobs": authorization.expected_job_count,
            "accepted": _integer(rerun_manifest.get("accepted_job_count"), default=0),
            "inserted": _integer(rerun_manifest.get("inserted_job_count"), default=0),
            "updated": _integer(rerun_manifest.get("updated_job_count"), default=0),
            "unchanged": _integer(rerun_manifest.get("unchanged_job_count"), default=0),
            "reactivated": _integer(rerun_manifest.get("reactivated_job_count"), default=0),
            "quarantined": _integer(rerun_manifest.get("quarantined_job_count"), default=0),
        },
        "phase6f_database_checks": checks,
        "controls": {
            "explicit_rerun_confirmation_required": True,
            "normalized_job_writes_enabled": True,
            "index_creation_performed": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "full_cohort_execution_performed": False,
            "automatic_rollback_enabled": False,
        },
        "blockers": blockers,
    }
    report["report_sha256"] = _canonical_sha256(report)
    return report


def write_phase7d2c_report(path: Path, report: Mapping[str, Any]) -> Path:
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

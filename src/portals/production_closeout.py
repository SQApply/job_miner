from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import Field, field_validator, model_validator
from pymongo.database import Database

from .contracts import ContractModel
from .production_ingestion import Phase6AIngestionPlan
from .production_persistence import read_phase6a_ingestion_plan
from .production_runner import Phase6ERunManifest


PHASE_6F_CONTRACT_VERSION = "1.0"
PHASE_6F = "6F"


class ProductionCloseoutError(RuntimeError):
    """Raised when Phase 6 closeout evidence cannot be read or validated."""


class Phase6FDatabaseChecks(ContractModel):
    fleet_run_records: int = Field(ge=0)
    source_run_records_first: int = Field(ge=0)
    source_run_records_rerun: int = Field(ge=0)
    current_jobs_observed_on_rerun: int = Field(ge=0)
    active_jobs_observed_on_rerun: int = Field(ge=0)
    duplicate_identity_hashes: int = Field(ge=0)
    duplicate_external_job_keys: int = Field(ge=0)
    history_mismatches: int = Field(ge=0)
    unsafe_deactivated_jobs: int = Field(ge=0)
    quarantine_records_first: int = Field(ge=0)
    quarantine_records_rerun: int = Field(ge=0)


class Phase6FCloseoutReport(ContractModel):
    contract_version: Literal["1.0"] = PHASE_6F_CONTRACT_VERSION
    phase: Literal["6F"] = PHASE_6F
    generated_at: datetime
    status: Literal["passed", "failed"]
    ready_for_phase7: bool
    plan_id: str
    cohort_sha256: str
    expected_source_count: int = Field(ge=1)
    selected_source_ids: list[str]
    first_write_run_id: str
    rerun_run_id: str
    first_write_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rerun_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    first_write_summary: dict[str, int]
    rerun_summary: dict[str, int]
    database_checks: Phase6FDatabaseChecks
    controls: dict[str, bool]
    issues: list[str] = Field(default_factory=list)

    @field_validator("generated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_status(self) -> "Phase6FCloseoutReport":
        expected_status = "passed" if not self.issues else "failed"
        if self.status != expected_status:
            raise ValueError("status does not match issues")
        if self.ready_for_phase7 != (self.status == "passed"):
            raise ValueError("ready_for_phase7 must match passed status")
        if self.controls.get("lifecycle_reconciliation_enabled") is not False:
            raise ValueError("Phase 6F cannot enable lifecycle reconciliation")
        if self.controls.get("deactivation_enabled") is not False:
            raise ValueError("Phase 6F cannot enable deactivation")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_phase6e_manifest(path: Path) -> Phase6ERunManifest:
    source = Path(path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Phase 6E manifest does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ProductionCloseoutError(f"Phase 6E manifest contains invalid JSON: {source}") from exc
    try:
        return Phase6ERunManifest.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise ProductionCloseoutError(f"Invalid Phase 6E manifest {source}: {exc}") from exc


def _outcome_total(manifest: Phase6ERunManifest) -> int:
    return (
        manifest.inserted_job_count
        + manifest.updated_job_count
        + manifest.unchanged_job_count
        + manifest.reactivated_job_count
    )


def _summary(manifest: Phase6ERunManifest) -> dict[str, int]:
    return {
        "requested_sources": manifest.requested_source_count,
        "completed_sources": manifest.completed_source_count,
        "successful_sources": manifest.successful_source_count,
        "failed_sources": manifest.failed_source_count,
        "blocked_sources": manifest.blocked_source_count,
        "cancelled_sources": manifest.cancelled_source_count,
        "extracted_jobs": manifest.extracted_job_count,
        "accepted_jobs": manifest.accepted_job_count,
        "quarantined_jobs": manifest.quarantined_job_count,
        "inserted_jobs": manifest.inserted_job_count,
        "updated_jobs": manifest.updated_job_count,
        "unchanged_jobs": manifest.unchanged_job_count,
        "reactivated_jobs": manifest.reactivated_job_count,
    }


def _manifest_controls_safe(manifest: Phase6ERunManifest) -> bool:
    return (
        manifest.execution_mode == "write"
        and manifest.controls.get("normalized_job_writes_enabled") is True
        and manifest.controls.get("lifecycle_reconciliation_enabled") is False
        and manifest.controls.get("deactivation_enabled") is False
    )


def _append_manifest_issues(
    issues: list[str],
    *,
    label: str,
    manifest: Phase6ERunManifest,
    expected_source_count: int,
) -> None:
    if manifest.execution_mode != "write":
        issues.append(f"{label}: execution_mode must be write")
    if manifest.requested_source_count != expected_source_count:
        issues.append(
            f"{label}: requested_source_count={manifest.requested_source_count}, expected={expected_source_count}"
        )
    if manifest.completed_source_count != expected_source_count:
        issues.append(
            f"{label}: completed_source_count={manifest.completed_source_count}, expected={expected_source_count}"
        )
    if manifest.successful_source_count != expected_source_count:
        issues.append(
            f"{label}: successful_source_count={manifest.successful_source_count}, expected={expected_source_count}"
        )
    for name, value in (
        ("failed_source_count", manifest.failed_source_count),
        ("blocked_source_count", manifest.blocked_source_count),
        ("cancelled_source_count", manifest.cancelled_source_count),
    ):
        if value != 0:
            issues.append(f"{label}: {name} must be 0, got {value}")
    if manifest.accepted_job_count < 1:
        issues.append(f"{label}: accepted_job_count must be at least 1")
    if _outcome_total(manifest) != manifest.accepted_job_count:
        issues.append(
            f"{label}: accepted jobs are not fully accounted for by upsert outcomes"
        )
    if not _manifest_controls_safe(manifest):
        issues.append(f"{label}: unsafe or incomplete Phase 6 controls")
    source_ids = [result.source_id for result in manifest.source_results]
    if len(source_ids) != len(set(source_ids)):
        issues.append(f"{label}: duplicate source results")
    for result in manifest.source_results:
        if result.status != "success":
            issues.append(f"{label}: source {result.source_id} status={result.status}")
        if result.accepted_count < 1:
            issues.append(f"{label}: source {result.source_id} accepted no jobs")
        source_outcomes = (
            result.inserted_count
            + result.updated_count
            + result.unchanged_count
            + result.reactivated_count
        )
        if source_outcomes != result.accepted_count:
            issues.append(
                f"{label}: source {result.source_id} accepted/upsert counters do not reconcile"
            )


def _duplicate_count(values: Sequence[Any]) -> int:
    counts = Counter(value for value in values if value not in (None, ""))
    return sum(1 for count in counts.values() if count > 1)


def _database_checks(
    db: Database,
    *,
    first: Phase6ERunManifest,
    rerun: Phase6ERunManifest,
) -> Phase6FDatabaseChecks:
    selected = list(rerun.selected_source_ids)
    fleet_records = list(
        db["production_ingestion_fleet_runs"].find(
            {"fleet_run_id": {"$in": [first.run_id, rerun.run_id]}}
        )
    )
    first_sources = list(
        db["production_ingestion_source_runs"].find({"fleet_run_id": first.run_id})
    )
    rerun_sources = list(
        db["production_ingestion_source_runs"].find({"fleet_run_id": rerun.run_id})
    )
    current_jobs = list(
        db["jobs_current"].find(
            {
                "source_id": {"$in": selected},
                "last_fleet_run_id": rerun.run_id,
            }
        )
    )
    identity_duplicates = _duplicate_count(
        [document.get("identity_hash") for document in current_jobs]
    )
    external_duplicates = _duplicate_count(
        [
            (document.get("source_id"), document.get("external_job_id"))
            for document in current_jobs
            if document.get("external_job_id")
        ]
    )
    history_mismatches = 0
    for document in current_jobs:
        job_id = str(document.get("job_id") or "")
        expected_versions = int(document.get("version") or 0)
        actual_versions = int(db["jobs_history"].count_documents({"job_id": job_id}))
        if not job_id or expected_versions < 1 or actual_versions != expected_versions:
            history_mismatches += 1
    unsafe_deactivated = sum(
        1
        for document in current_jobs
        if document.get("is_active") is not True or document.get("deactivated_at") is not None
    )
    return Phase6FDatabaseChecks(
        fleet_run_records=len(fleet_records),
        source_run_records_first=len(first_sources),
        source_run_records_rerun=len(rerun_sources),
        current_jobs_observed_on_rerun=len(current_jobs),
        active_jobs_observed_on_rerun=sum(
            1 for document in current_jobs if document.get("is_active") is True
        ),
        duplicate_identity_hashes=identity_duplicates,
        duplicate_external_job_keys=external_duplicates,
        history_mismatches=history_mismatches,
        unsafe_deactivated_jobs=unsafe_deactivated,
        quarantine_records_first=int(
            db["production_job_quarantine"].count_documents({"fleet_run_id": first.run_id})
        ),
        quarantine_records_rerun=int(
            db["production_job_quarantine"].count_documents({"fleet_run_id": rerun.run_id})
        ),
    )


def build_phase6f_closeout_report(
    *,
    plan: Phase6AIngestionPlan,
    first_write_manifest: Phase6ERunManifest,
    rerun_manifest: Phase6ERunManifest,
    first_write_manifest_path: Path,
    rerun_manifest_path: Path,
    db: Database,
    expected_source_count: int,
) -> Phase6FCloseoutReport:
    if expected_source_count < 1:
        raise ProductionCloseoutError("expected_source_count must be at least 1")

    first = first_write_manifest
    rerun = rerun_manifest
    issues: list[str] = []

    _append_manifest_issues(
        issues, label="first_write", manifest=first, expected_source_count=expected_source_count
    )
    _append_manifest_issues(
        issues, label="rerun", manifest=rerun, expected_source_count=expected_source_count
    )

    if first.plan_id != plan.plan_id or rerun.plan_id != plan.plan_id:
        issues.append("Phase 6E manifests do not belong to the supplied Phase 6A plan")
    if first.cohort_sha256 != plan.cohort_sha256 or rerun.cohort_sha256 != plan.cohort_sha256:
        issues.append("Phase 6E cohort checksum does not match the Phase 6A plan")
    if first.selected_source_ids != rerun.selected_source_ids:
        issues.append("First write and rerun selected different source cohorts")
    selected = list(rerun.selected_source_ids)
    if len(selected) != expected_source_count:
        issues.append(
            f"Selected cohort contains {len(selected)} sources, expected {expected_source_count}"
        )
    if any(source_id not in plan.selected_source_ids for source_id in selected):
        issues.append("Phase 6E manifests contain a source outside the frozen cohort")
    if expected_source_count == plan.selected_source_count and selected != plan.selected_source_ids:
        issues.append("Full-cohort closeout must preserve the frozen Phase 6A source order")

    if rerun.inserted_job_count != 0:
        issues.append(
            f"Rerun inserted {rerun.inserted_job_count} jobs; idempotent rerun requires 0"
        )
    if rerun.reactivated_job_count != 0:
        issues.append(
            f"Rerun reactivated {rerun.reactivated_job_count} jobs while deactivation is disabled"
        )

    checks = _database_checks(db, first=first, rerun=rerun)
    if checks.fleet_run_records != 2:
        issues.append(
            f"MongoDB contains {checks.fleet_run_records}/2 required fleet run records"
        )
    if checks.source_run_records_first != expected_source_count:
        issues.append(
            "MongoDB first-write source-run count does not match the expected cohort"
        )
    if checks.source_run_records_rerun != expected_source_count:
        issues.append("MongoDB rerun source-run count does not match the expected cohort")
    if checks.current_jobs_observed_on_rerun != rerun.accepted_job_count:
        issues.append(
            "MongoDB current jobs observed on rerun do not match rerun accepted-job count"
        )
    if checks.active_jobs_observed_on_rerun != checks.current_jobs_observed_on_rerun:
        issues.append("One or more rerun jobs are not active")
    if checks.duplicate_identity_hashes:
        issues.append(
            f"MongoDB contains {checks.duplicate_identity_hashes} duplicate identity hashes"
        )
    if checks.duplicate_external_job_keys:
        issues.append(
            f"MongoDB contains {checks.duplicate_external_job_keys} duplicate external job keys"
        )
    if checks.history_mismatches:
        issues.append(
            f"MongoDB contains {checks.history_mismatches} current/history version mismatches"
        )
    if checks.unsafe_deactivated_jobs:
        issues.append(
            f"MongoDB contains {checks.unsafe_deactivated_jobs} unexpectedly deactivated jobs"
        )

    controls = {
        "normalized_job_writes_enabled": True,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
    }
    status: Literal["passed", "failed"] = "passed" if not issues else "failed"
    return Phase6FCloseoutReport(
        generated_at=datetime.now(timezone.utc),
        status=status,
        ready_for_phase7=status == "passed",
        plan_id=plan.plan_id,
        cohort_sha256=plan.cohort_sha256,
        expected_source_count=expected_source_count,
        selected_source_ids=selected,
        first_write_run_id=first.run_id,
        rerun_run_id=rerun.run_id,
        first_write_manifest_sha256=_sha256_file(first_write_manifest_path),
        rerun_manifest_sha256=_sha256_file(rerun_manifest_path),
        first_write_summary=_summary(first),
        rerun_summary=_summary(rerun),
        database_checks=checks,
        controls=controls,
        issues=issues,
    )


def load_and_build_phase6f_report(
    *,
    plan_path: Path,
    first_write_manifest_path: Path,
    rerun_manifest_path: Path,
    db: Database,
    expected_source_count: int,
) -> Phase6FCloseoutReport:
    plan = read_phase6a_ingestion_plan(plan_path)
    first = read_phase6e_manifest(first_write_manifest_path)
    rerun = read_phase6e_manifest(rerun_manifest_path)
    return build_phase6f_closeout_report(
        plan=plan,
        first_write_manifest=first,
        rerun_manifest=rerun,
        first_write_manifest_path=first_write_manifest_path,
        rerun_manifest_path=rerun_manifest_path,
        db=db,
        expected_source_count=expected_source_count,
    )


def write_phase6f_report(path: Path, report: Phase6FCloseoutReport) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(target)
    return target

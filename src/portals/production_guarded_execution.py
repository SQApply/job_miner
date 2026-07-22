from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator
from .contracts import ContractModel
from .production_guarded_rollout import read_phase7d3a_rollout_plan
from .production_ingestion import Phase6AIngestionPlan

if TYPE_CHECKING:
    from pymongo.database import Database
    from .production_runner import Phase6ERunManifest


PHASE_7D3B_CONTRACT_VERSION = "1.0"
PHASE_7D3B = "7D3B"
PHASE_7D3B_WRITE_CONFIRMATION = "ENABLE_PHASE_7D3B_COMPLETE_BATCH_WRITE"


class ProductionGuardedExecutionError(RuntimeError):
    """Raised when a guarded complete-catalog batch cannot run safely."""


class Phase7D3BBatchAuthorization(ContractModel):
    contract_version: Literal["1.0"] = PHASE_7D3B_CONTRACT_VERSION
    phase: Literal["7D3B"] = PHASE_7D3B
    rollout_id: str = Field(min_length=1)
    rollout_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    production_plan_id: str = Field(min_length=1)
    cohort_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    batch_id: str = Field(min_length=1)
    batch_ordinal: int = Field(ge=1, le=7)
    batch_count: int = Field(ge=1)
    purpose: Literal["initial_backfill"] = "initial_backfill"
    source_count: int = Field(ge=1, le=4)
    source_ids: list[str] = Field(min_length=1)
    catalog_mode: Literal["complete_catalog"] = "complete_catalog"
    max_jobs_per_source: None = None
    page_safety_cap: int = Field(ge=1, le=500)
    source_timeout_seconds: int = Field(ge=30, le=86400)
    max_attempts: int = Field(ge=1, le=3)
    detail_retry_attempts: int = Field(ge=0, le=2)
    requests_per_minute: int = Field(ge=1, le=600)
    acquisition_timeout_seconds: float = Field(gt=0, le=300)
    allow_llm_fallback: bool

    @model_validator(mode="after")
    def validate_authorization(self) -> "Phase7D3BBatchAuthorization":
        if self.batch_count != 7:
            raise ValueError("Phase 7D3B requires the seven-batch Phase 7D3A plan")
        expected_count = 2 if self.batch_ordinal == self.batch_count else 4
        if self.source_count != expected_count or self.source_count != len(self.source_ids):
            raise ValueError("Batch source count differs from the signed rollout partition")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("Batch source ids must be unique")
        if self.page_safety_cap != 500:
            raise ValueError("Complete-catalog page safety cap must be 500")
        if self.source_timeout_seconds != 1800:
            raise ValueError("Complete-catalog source timeout must be 1800 seconds")
        if self.max_attempts != 2 or self.detail_retry_attempts != 1:
            raise ValueError("Complete-catalog retry policy differs from Phase 7D3A")
        if self.requests_per_minute != 30 or not self.allow_llm_fallback:
            raise ValueError("Complete-catalog extraction controls differ from Phase 7D3A")
        if self.acquisition_timeout_seconds != 25:
            raise ValueError("Complete-catalog acquisition timeout differs from Phase 7D3A")
        return self


class Phase7D3BSourceSnapshot(ContractModel):
    source_ids: list[str] = Field(min_length=1)
    current_by_source: dict[str, int]
    active_by_source: dict[str, int]
    current_total: int = Field(ge=0)
    active_total: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "Phase7D3BSourceSnapshot":
        expected = set(self.source_ids)
        if set(self.current_by_source) != expected or set(self.active_by_source) != expected:
            raise ValueError("Snapshot source maps do not cover the authorized batch")
        if self.current_total != sum(self.current_by_source.values()):
            raise ValueError("Snapshot current total is inconsistent")
        if self.active_total != sum(self.active_by_source.values()):
            raise ValueError("Snapshot active total is inconsistent")
        return self


class Phase7D3BBatchReportBody(ContractModel):
    contract_version: Literal["1.0"] = PHASE_7D3B_CONTRACT_VERSION
    phase: Literal["7D3B"] = PHASE_7D3B
    status: Literal["passed", "failed"]
    ready_for_next_batch_or_closeout: bool
    generated_at: datetime
    rollout_id: str = Field(min_length=1)
    rollout_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    production_plan_id: str = Field(min_length=1)
    cohort_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    batch_id: str = Field(min_length=1)
    batch_ordinal: int = Field(ge=1, le=7)
    batch_count: int = Field(ge=1)
    run_id: str = Field(min_length=1)
    source_count: int = Field(ge=1, le=4)
    source_ids: list[str] = Field(min_length=1)
    successful_source_count: int = Field(ge=0)
    complete_catalog_source_count: int = Field(ge=0)
    counters: dict[str, int]
    before: Phase7D3BSourceSnapshot
    after: Phase7D3BSourceSnapshot
    source_results: list[dict[str, Any]]
    blockers: list[str]
    controls: dict[str, Any]

    @field_validator("generated_at")
    @classmethod
    def require_aware_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_report(self) -> "Phase7D3BBatchReportBody":
        if self.source_count != len(self.source_ids):
            raise ValueError("Report source count does not match source ids")
        if self.before.source_ids != self.source_ids or self.after.source_ids != self.source_ids:
            raise ValueError("Report snapshots do not match the authorized source order")
        if len(self.source_results) != self.source_count:
            raise ValueError("Report must retain one terminal result per source")
        passed = self.status == "passed"
        if passed != (not self.blockers):
            raise ValueError("Report status and blockers disagree")
        if self.ready_for_next_batch_or_closeout is not passed:
            raise ValueError("Only a passed batch can authorize progression")
        if passed and (
            self.successful_source_count != self.source_count
            or self.complete_catalog_source_count != self.source_count
        ):
            raise ValueError("Passed report does not contain complete successful sources")
        required_controls = {
            "production_writes_enabled": True,
            "catalog_mode": "complete_catalog",
            "max_jobs_per_source": None,
            "page_safety_cap": 500,
            "max_source_concurrency": 1,
            "gpu_llm_concurrency": 1,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "automatic_rollback_enabled": False,
            "stop_after_failed_batch": True,
        }
        for key, expected in required_controls.items():
            if self.controls.get(key) != expected:
                raise ValueError(f"Unsafe or inconsistent guarded-batch control: {key}")
        return self


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Required {label} does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ProductionGuardedExecutionError(f"{label} contains invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ProductionGuardedExecutionError(f"{label} must contain a JSON object")
    return payload


def load_phase7d3b_authorization(
    *,
    rollout_plan_path: Path,
    production_plan_path: Path,
    batch_ordinal: int,
) -> tuple[Phase6AIngestionPlan, Phase7D3BBatchAuthorization]:
    rollout = read_phase7d3a_rollout_plan(rollout_plan_path)
    try:
        production_plan = Phase6AIngestionPlan.model_validate(
            _load_json(production_plan_path, label="Phase 7D1 production plan")
        )
    except (TypeError, ValueError) as exc:
        raise ProductionGuardedExecutionError(
            f"Invalid Phase 7D1 production plan: {exc}"
        ) from exc

    if production_plan.plan_id != str(rollout.get("plan_id") or ""):
        raise ProductionGuardedExecutionError("Rollout references a different production plan")
    if production_plan.cohort_sha256 != str(rollout.get("cohort_sha256") or ""):
        raise ProductionGuardedExecutionError("Rollout references a different frozen cohort")
    if production_plan.selected_source_ids != rollout.get("full_source_ids"):
        raise ProductionGuardedExecutionError("Rollout source order differs from production plan")

    batches = rollout.get("initial_backfill_batches")
    if not isinstance(batches, list) or len(batches) != 7:
        raise ProductionGuardedExecutionError("Rollout does not contain seven backfill batches")
    if batch_ordinal < 1 or batch_ordinal > len(batches):
        raise ProductionGuardedExecutionError("batch_ordinal must be between 1 and 7")
    batch = batches[batch_ordinal - 1]
    if not isinstance(batch, dict) or int(batch.get("ordinal") or 0) != batch_ordinal:
        raise ProductionGuardedExecutionError("Rollout batch ordinal is inconsistent")

    completion = rollout.get("catalog_completion_policy") or {}
    limits = rollout.get("execution_limits") or {}
    controls = rollout.get("controls") or {}
    if controls.get("pilot_seeded_sources_included_in_initial_backfill") is not True:
        raise ProductionGuardedExecutionError("Rollout does not include pilot-seeded sources")
    authorization = Phase7D3BBatchAuthorization(
        rollout_id=str(rollout.get("rollout_id") or ""),
        rollout_plan_sha256=str(rollout.get("plan_sha256") or ""),
        production_plan_id=production_plan.plan_id,
        cohort_sha256=production_plan.cohort_sha256,
        batch_id=str(batch.get("batch_id") or ""),
        batch_ordinal=batch_ordinal,
        batch_count=len(batches),
        purpose=str(batch.get("purpose") or ""),
        source_count=int(batch.get("source_count") or 0),
        source_ids=[str(value or "") for value in batch.get("source_ids") or []],
        catalog_mode=str(completion.get("mode") or ""),
        max_jobs_per_source=completion.get("max_jobs_per_source"),
        page_safety_cap=int(completion.get("page_safety_cap") or 0),
        source_timeout_seconds=int(limits.get("source_timeout_seconds") or 0),
        max_attempts=int(limits.get("max_attempts") or 0),
        detail_retry_attempts=int(limits.get("detail_retry_attempts") or 0),
        requests_per_minute=int(limits.get("requests_per_minute") or 0),
        acquisition_timeout_seconds=float(
            limits.get("acquisition_timeout_seconds") or 0
        ),
        allow_llm_fallback=limits.get("allow_llm_fallback") is True,
    )
    return production_plan, authorization


def require_phase7d3b_write_confirmation(value: str) -> None:
    if str(value or "") != PHASE_7D3B_WRITE_CONFIRMATION:
        raise ProductionGuardedExecutionError(
            "Phase 7D3B writes require --confirm-production-writes "
            + PHASE_7D3B_WRITE_CONFIRMATION
        )


def batch_checkpoint_path(output_dir: Path, batch_ordinal: int) -> Path:
    return Path(output_dir).resolve() / f"phase7d3b_batch_{batch_ordinal:02d}_checkpoint.json"


def require_phase7d3b_checkpoint_state(
    *,
    output_dir: Path,
    authorization: Phase7D3BBatchAuthorization,
    resume_incomplete: bool,
) -> None:
    batch_ordinal = authorization.batch_ordinal

    def require_same_rollout(report: Mapping[str, Any], ordinal: int) -> None:
        if (
            report.get("rollout_id") != authorization.rollout_id
            or report.get("rollout_plan_sha256")
            != authorization.rollout_plan_sha256
            or report.get("production_plan_id")
            != authorization.production_plan_id
            or report.get("cohort_sha256") != authorization.cohort_sha256
        ):
            raise ProductionGuardedExecutionError(
                f"Batch {ordinal:02d} checkpoint belongs to a different rollout"
            )

    for ordinal in range(1, batch_ordinal):
        previous = batch_checkpoint_path(output_dir, ordinal)
        if not previous.exists():
            raise ProductionGuardedExecutionError(
                f"Batch {ordinal:02d} checkpoint is missing; batches must run in order"
            )
        report = read_phase7d3b_report(previous)
        require_same_rollout(report, ordinal)
        if report.get("status") != "passed":
            raise ProductionGuardedExecutionError(
                f"Batch {ordinal:02d} is not passed; later batches are blocked"
            )

    current = batch_checkpoint_path(output_dir, batch_ordinal)
    if not current.exists():
        return
    report = read_phase7d3b_report(current)
    require_same_rollout(report, batch_ordinal)
    if report.get("status") == "passed":
        raise ProductionGuardedExecutionError(
            f"Batch {batch_ordinal:02d} already passed and cannot be written again"
        )
    if not resume_incomplete:
        raise ProductionGuardedExecutionError(
            f"Batch {batch_ordinal:02d} has an incomplete checkpoint; "
            "rerun with --resume-incomplete after reviewing its report"
        )


def capture_phase7d3b_source_snapshot(
    database: "Database",
    *,
    source_ids: list[str],
) -> Phase7D3BSourceSnapshot:
    current: dict[str, int] = {}
    active: dict[str, int] = {}
    collection = database["jobs_current"]
    for source_id in source_ids:
        current[source_id] = int(collection.count_documents({"source_id": source_id}))
        active[source_id] = int(
            collection.count_documents({"source_id": source_id, "is_active": True})
        )
    return Phase7D3BSourceSnapshot(
        source_ids=list(source_ids),
        current_by_source=current,
        active_by_source=active,
        current_total=sum(current.values()),
        active_total=sum(active.values()),
    )


def build_phase7d3b_batch_report(
    *,
    authorization: Phase7D3BBatchAuthorization,
    manifest: "Phase6ERunManifest",
    before: Phase7D3BSourceSnapshot,
    after: Phase7D3BSourceSnapshot,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []

    def block(value: str) -> None:
        if value not in blockers:
            blockers.append(value)

    if manifest.plan_id != authorization.production_plan_id:
        block("manifest_plan_id_mismatch")
    if manifest.cohort_sha256 != authorization.cohort_sha256:
        block("manifest_cohort_mismatch")
    if manifest.execution_mode != "write":
        block("manifest_not_in_write_mode")
    if manifest.selected_source_ids != authorization.source_ids:
        block("manifest_source_scope_mismatch")
    if manifest.requested_source_count != authorization.source_count:
        block("manifest_source_count_mismatch")
    required_manifest_controls = {
        "normalized_job_writes_enabled": True,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
        "max_source_concurrency": 1,
        "max_jobs_per_source": None,
        "max_pages_per_source": 500,
        "catalog_mode": "complete_catalog",
        "catalog_completion_required": True,
        "bounded_pilot_execution": False,
    }
    for key, expected in required_manifest_controls.items():
        if manifest.controls.get(key) != expected:
            block(f"unsafe_manifest_control:{key}")

    result_by_id = {result.source_id: result for result in manifest.source_results}
    if len(result_by_id) != authorization.source_count:
        block("manifest_terminal_result_count_mismatch")
    for source_id in authorization.source_ids:
        result = result_by_id.get(source_id)
        if result is None:
            block(f"missing_source_result:{source_id}")
            continue
        if result.status != "success":
            block(f"source_not_successful:{source_id}:{result.status}")
        if result.catalog_mode != "complete_catalog" or not result.catalog_complete:
            block(f"source_catalog_incomplete:{source_id}")
        if not result.discovery_complete:
            block(f"source_discovery_incomplete:{source_id}")
        if result.discovered_count != result.attempted_count:
            block(f"source_detail_scope_incomplete:{source_id}")
        if result.quarantined_count or result.rejected_count:
            block(f"source_quality_shortfall:{source_id}")
        if result.accepted_count < 1:
            block(f"source_zero_accepted_jobs:{source_id}")
        outcomes = (
            result.inserted_count
            + result.updated_count
            + result.unchanged_count
            + result.reactivated_count
        )
        if outcomes != result.accepted_count:
            block(f"source_write_accounting_mismatch:{source_id}")

    current_delta = after.current_total - before.current_total
    active_delta = after.active_total - before.active_total
    if current_delta != manifest.inserted_job_count:
        block("database_current_delta_mismatch")
    if active_delta != manifest.inserted_job_count + manifest.reactivated_job_count:
        block("database_active_delta_mismatch")
    if manifest.completed_source_count != authorization.source_count:
        block("manifest_incomplete_source_execution")
    if manifest.successful_source_count != authorization.source_count:
        block("manifest_successful_source_count_mismatch")

    counters = {
        "discovered": sum(result.discovered_count for result in manifest.source_results),
        "extracted": manifest.extracted_job_count,
        "accepted": manifest.accepted_job_count,
        "quarantined": manifest.quarantined_job_count,
        "inserted": manifest.inserted_job_count,
        "updated": manifest.updated_job_count,
        "unchanged": manifest.unchanged_job_count,
        "reactivated": manifest.reactivated_job_count,
        "current_jobs_before": before.current_total,
        "current_jobs_after": after.current_total,
        "active_jobs_before": before.active_total,
        "active_jobs_after": after.active_total,
    }
    passed = not blockers
    body = Phase7D3BBatchReportBody(
        status="passed" if passed else "failed",
        ready_for_next_batch_or_closeout=passed,
        generated_at=generated_at or datetime.now(timezone.utc),
        rollout_id=authorization.rollout_id,
        rollout_plan_sha256=authorization.rollout_plan_sha256,
        production_plan_id=authorization.production_plan_id,
        cohort_sha256=authorization.cohort_sha256,
        batch_id=authorization.batch_id,
        batch_ordinal=authorization.batch_ordinal,
        batch_count=authorization.batch_count,
        run_id=manifest.run_id,
        source_count=authorization.source_count,
        source_ids=authorization.source_ids,
        successful_source_count=manifest.successful_source_count,
        complete_catalog_source_count=sum(
            result.catalog_mode == "complete_catalog" and result.catalog_complete
            for result in manifest.source_results
        ),
        counters=counters,
        before=before,
        after=after,
        source_results=[result.model_dump(mode="json") for result in manifest.source_results],
        blockers=blockers,
        controls={
            "production_writes_enabled": True,
            "catalog_mode": "complete_catalog",
            "max_jobs_per_source": None,
            "page_safety_cap": authorization.page_safety_cap,
            "max_source_concurrency": 1,
            "gpu_llm_concurrency": 1,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "automatic_rollback_enabled": False,
            "stop_after_failed_batch": True,
        },
    )
    payload = body.model_dump(mode="json")
    payload["report_sha256"] = _canonical_sha256(payload)
    return payload


def read_phase7d3b_report(path: Path) -> dict[str, Any]:
    payload = _load_json(path, label="Phase 7D3B batch report")
    stored = str(payload.get("report_sha256") or "").strip().lower()
    unsigned = dict(payload)
    unsigned.pop("report_sha256", None)
    if not stored or stored != _canonical_sha256(unsigned):
        raise ProductionGuardedExecutionError("Phase 7D3B report checksum is invalid")
    try:
        Phase7D3BBatchReportBody.model_validate(unsigned)
    except (TypeError, ValueError) as exc:
        raise ProductionGuardedExecutionError(
            f"Invalid Phase 7D3B batch report: {exc}"
        ) from exc
    return payload


def write_phase7d3b_report(path: Path, payload: Mapping[str, Any]) -> Path:
    target = Path(path).resolve()
    candidate = dict(payload)
    stored = str(candidate.get("report_sha256") or "").strip().lower()
    unsigned = dict(candidate)
    unsigned.pop("report_sha256", None)
    if not stored or stored != _canonical_sha256(unsigned):
        raise ProductionGuardedExecutionError("Candidate Phase 7D3B checksum is invalid")
    Phase7D3BBatchReportBody.model_validate(unsigned)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(candidate, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, target)
    return target

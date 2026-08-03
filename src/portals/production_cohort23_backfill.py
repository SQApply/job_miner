from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel
from .production_cohort23 import (
    read_phase7d4a_cohort,
    read_phase7d4a_rollout,
)
from .production_ingestion import Phase6AIngestionPlan, Phase6ASource
from .production_runner import Phase6ERunnerConfig

if TYPE_CHECKING:
    from pymongo.database import Database
    from .production_runner import Phase6ERunManifest


PHASE_7D4B_CONTRACT_VERSION = "1.0"
PHASE_7D4B = "7D4B"
PHASE_7D4B_WRITE_CONFIRMATION = "ENABLE_PHASE_7D4B_PROMOTED_BACKFILL_WRITE"
_PROGRESSING_STATUSES = {"passed", "passed_with_partial"}


class ProductionCohort23BackfillError(RuntimeError):
    """Raised when the signed seven-source backfill cannot run safely."""


class Phase7D4BBatchAuthorization(ContractModel):
    contract_version: Literal["1.0"] = PHASE_7D4B_CONTRACT_VERSION
    phase: Literal["7D4B"] = PHASE_7D4B
    rollout_id: str = Field(min_length=1)
    rollout_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    production_plan_id: str = Field(min_length=1)
    cohort_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    batch_id: str = Field(min_length=1)
    batch_ordinal: int = Field(ge=1, le=2)
    batch_count: Literal[2] = 2
    source_count: int = Field(ge=3, le=4)
    source_ids: list[str] = Field(min_length=3, max_length=4)
    source_tiers: dict[str, Literal["complete_catalog", "partial_safe"]]
    catalog_mode: Literal["complete_catalog"] = "complete_catalog"
    max_jobs_per_source: None = None
    page_safety_cap: Literal[500] = 500
    source_timeout_seconds: Literal[1800] = 1800
    max_attempts: Literal[2] = 2
    detail_retry_attempts: Literal[1] = 1
    requests_per_minute: Literal[30] = 30
    acquisition_timeout_seconds: Literal[25.0] = 25.0
    allow_llm_fallback: Literal[True] = True
    source_concurrency: Literal[1] = 1
    detail_concurrency: Literal[1] = 1
    gpu_llm_concurrency: Literal[1] = 1

    @model_validator(mode="after")
    def validate_authorization(self) -> "Phase7D4BBatchAuthorization":
        expected_count = 4 if self.batch_ordinal == 1 else 3
        if self.source_count != expected_count:
            raise ValueError("Phase 7D4B batch size differs from the signed rollout")
        if self.source_count != len(self.source_ids):
            raise ValueError("Phase 7D4B source count differs from source ids")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("Phase 7D4B source ids must be unique")
        if set(self.source_tiers) != set(self.source_ids):
            raise ValueError("Phase 7D4B source tiers do not cover the batch")
        return self


class Phase7D4BSourceSnapshot(ContractModel):
    source_ids: list[str] = Field(min_length=1)
    current_by_source: dict[str, int]
    active_by_source: dict[str, int]
    current_total: int = Field(ge=0)
    active_total: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "Phase7D4BSourceSnapshot":
        expected = set(self.source_ids)
        if set(self.current_by_source) != expected:
            raise ValueError("Current-job snapshot does not cover the batch")
        if set(self.active_by_source) != expected:
            raise ValueError("Active-job snapshot does not cover the batch")
        if self.current_total != sum(self.current_by_source.values()):
            raise ValueError("Current-job snapshot total is inconsistent")
        if self.active_total != sum(self.active_by_source.values()):
            raise ValueError("Active-job snapshot total is inconsistent")
        return self


class Phase7D4BBatchReportBody(ContractModel):
    contract_version: Literal["1.0"] = PHASE_7D4B_CONTRACT_VERSION
    phase: Literal["7D4B"] = PHASE_7D4B
    status: Literal["passed", "passed_with_partial", "failed"]
    ready_for_next_batch_or_closeout: bool
    generated_at: datetime
    rollout_id: str = Field(min_length=1)
    rollout_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    production_plan_id: str = Field(min_length=1)
    cohort_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    batch_id: str = Field(min_length=1)
    batch_ordinal: int = Field(ge=1, le=2)
    batch_count: Literal[2] = 2
    run_id: str = Field(min_length=1)
    source_count: int = Field(ge=3, le=4)
    source_ids: list[str] = Field(min_length=3, max_length=4)
    source_tiers: dict[str, Literal["complete_catalog", "partial_safe"]]
    successful_source_count: int = Field(ge=0)
    complete_catalog_source_count: int = Field(ge=0)
    productive_partial_source_count: int = Field(ge=0)
    deferred_source_count: int = Field(ge=0)
    productive_source_ids: list[str]
    complete_catalog_source_ids: list[str]
    productive_partial_source_ids: list[str]
    deferred_source_ids: list[str]
    source_dispositions: dict[str, str]
    counters: dict[str, int]
    before: Phase7D4BSourceSnapshot
    after: Phase7D4BSourceSnapshot
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
    def validate_report(self) -> "Phase7D4BBatchReportBody":
        if self.source_count != len(self.source_ids):
            raise ValueError("Report source count does not match source ids")
        if set(self.source_tiers) != set(self.source_ids):
            raise ValueError("Report source tiers do not cover the batch")
        if self.before.source_ids != self.source_ids:
            raise ValueError("Before snapshot does not match the source order")
        if self.after.source_ids != self.source_ids:
            raise ValueError("After snapshot does not match the source order")
        if len(self.source_results) != self.source_count:
            raise ValueError("Report must retain one result per source")
        if set(self.source_dispositions) != set(self.source_ids):
            raise ValueError("Source dispositions do not cover the batch")
        complete = self.complete_catalog_source_ids
        partial = self.productive_partial_source_ids
        deferred = self.deferred_source_ids
        if any(len(values) != len(set(values)) for values in (complete, partial, deferred)):
            raise ValueError("Source disposition lists contain duplicates")
        if set(complete) & set(partial):
            raise ValueError("Complete and partial source sets overlap")
        if set(complete + partial) & set(deferred):
            raise ValueError("Productive and deferred source sets overlap")
        if set(complete + partial + deferred) != set(self.source_ids):
            raise ValueError("Source dispositions do not account for the batch")
        if self.complete_catalog_source_count != len(complete):
            raise ValueError("Complete source count is inconsistent")
        if self.productive_partial_source_count != len(partial):
            raise ValueError("Partial source count is inconsistent")
        if self.deferred_source_count != len(deferred):
            raise ValueError("Deferred source count is inconsistent")
        if self.productive_source_ids != complete + partial:
            raise ValueError("Productive source order is inconsistent")
        progressing = self.status in _PROGRESSING_STATUSES
        if progressing != (not self.blockers):
            raise ValueError("Report status and safety blockers disagree")
        if self.ready_for_next_batch_or_closeout is not progressing:
            raise ValueError("Only an accounting-safe batch may progress")
        if self.status == "passed" and len(complete) != self.source_count:
            raise ValueError("Passed report must contain only complete sources")
        if self.status == "passed_with_partial" and not (partial or deferred):
            raise ValueError("passed_with_partial requires a partial or deferred source")
        if self.status == "failed" and not self.blockers:
            raise ValueError("Failed report must retain a safety blocker")
        required_controls = {
            "production_writes_enabled": True,
            "promoted_sources_only": True,
            "catalog_mode": "complete_catalog",
            "max_jobs_per_source": None,
            "page_safety_cap": 500,
            "max_source_concurrency": 1,
            "detail_concurrency": 1,
            "gpu_llm_concurrency": 1,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "failed_or_partial_runs_increment_missing_count": False,
            "changed_only_downstream_processing": True,
        }
        for key, expected in required_controls.items():
            if self.controls.get(key) != expected:
                raise ValueError(f"Unsafe Phase 7D4B report control: {key}")
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
        raise ProductionCohort23BackfillError(
            f"{label} contains invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ProductionCohort23BackfillError(f"{label} must contain an object")
    return payload


def _phase7d4b_plan(
    cohort: Mapping[str, Any],
    rollout: Mapping[str, Any],
) -> Phase6AIngestionPlan:
    raw_sources = cohort.get("sources")
    if not isinstance(raw_sources, list):
        raise ProductionCohort23BackfillError("Cohort sources are missing")
    sources: list[Phase6ASource] = []
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise ProductionCohort23BackfillError(
                "Cohort contains a non-object source"
            )
        evidence = raw.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        sources.append(
            Phase6ASource(
                source_id=str(raw.get("source_id") or ""),
                source_row=int(raw.get("source_row") or 0),
                display_name=str(raw.get("display_name") or ""),
                listing_url=str(raw.get("listing_url") or ""),
                detected_platform="unknown",
                bounded_extracted_jobs=int(evidence.get("accepted_jobs") or 0),
                evidence_run_id=str(evidence.get("origin") or "") or None,
            )
        )
    source_ids = [source.source_id for source in sources]
    if source_ids != list(cohort.get("cohort_source_ids") or []):
        raise ProductionCohort23BackfillError(
            "Cohort source records do not match the frozen source order"
        )
    generated_at = datetime.fromisoformat(str(rollout.get("generated_at") or ""))
    plan_id = "phase7d4b_" + _canonical_sha256(
        {
            "rollout_id": rollout.get("rollout_id"),
            "rollout_plan_sha256": rollout.get("rollout_plan_sha256"),
            "cohort_sha256": cohort.get("cohort_sha256"),
            "source_ids": source_ids,
        }
    )[:20]
    return Phase6AIngestionPlan(
        plan_id=plan_id,
        generated_at=generated_at,
        cohort_sha256=str(cohort.get("cohort_sha256") or ""),
        cohort_source_count=len(source_ids),
        selected_source_count=len(source_ids),
        deferred_source_count=len(cohort.get("deferred_source_ids") or []),
        selected_source_ids=source_ids,
        sources=sources,
        controls={
            "execution_mode": "plan_only",
            "max_source_concurrency": 1,
            "source_timeout_seconds": 1800,
            "production_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
        },
    )


def load_phase7d4b_authorization(
    *,
    cohort_path: Path,
    rollout_plan_path: Path,
    batch_ordinal: int,
) -> tuple[Phase6AIngestionPlan, Phase7D4BBatchAuthorization]:
    cohort = read_phase7d4a_cohort(cohort_path)
    rollout = read_phase7d4a_rollout(rollout_plan_path, cohort=cohort)
    plan = _phase7d4b_plan(cohort, rollout)
    initial = rollout.get("initial_backfill")
    if not isinstance(initial, dict):
        raise ProductionCohort23BackfillError(
            "Phase 7D4A initial backfill is missing"
        )
    batches = initial.get("batches")
    if not isinstance(batches, list) or len(batches) != 2:
        raise ProductionCohort23BackfillError(
            "Phase 7D4A must contain exactly two initial backfill batches"
        )
    if batch_ordinal < 1 or batch_ordinal > len(batches):
        raise ProductionCohort23BackfillError(
            "batch_ordinal must be between 1 and 2"
        )
    promoted = list(cohort.get("promoted_backfill_source_ids") or [])
    failed = set(cohort.get("failed_candidate_source_ids") or [])
    if list(initial.get("source_ids") or []) != promoted or len(promoted) != 7:
        raise ProductionCohort23BackfillError(
            "Initial backfill is not the exact seven-source promoted set"
        )
    if set(promoted) & failed:
        raise ProductionCohort23BackfillError(
            "Failed candidates leaked into the promoted backfill"
        )
    batch = batches[batch_ordinal - 1]
    if not isinstance(batch, dict):
        raise ProductionCohort23BackfillError("Backfill batch is invalid")
    if int(batch.get("batch_ordinal") or 0) != batch_ordinal:
        raise ProductionCohort23BackfillError(
            "Backfill batch ordinal is inconsistent"
        )
    limits = rollout.get("execution_limits")
    if not isinstance(limits, dict):
        raise ProductionCohort23BackfillError("Execution limits are missing")
    required_limits: dict[str, int | float] = {
        "source_concurrency": 1,
        "detail_concurrency": 1,
        "gpu_llm_concurrency": 1,
        "requests_per_minute": 30,
        "source_timeout_seconds": 1800,
        "acquisition_timeout_seconds": 25,
        "page_safety_cap": 500,
        "batch_size": 4,
    }
    for key, expected in required_limits.items():
        if limits.get(key) != expected:
            raise ProductionCohort23BackfillError(
                f"Unsafe or inconsistent execution limit: {key}"
            )
    tier_by_source = {
        str(source.get("source_id") or ""): str(source.get("tier") or "")
        for source in cohort.get("sources") or []
        if isinstance(source, dict)
    }
    source_ids = [str(value or "") for value in batch.get("source_ids") or []]
    authorization = Phase7D4BBatchAuthorization(
        rollout_id=str(rollout.get("rollout_id") or ""),
        rollout_plan_sha256=str(rollout.get("rollout_plan_sha256") or ""),
        production_plan_id=plan.plan_id,
        cohort_sha256=plan.cohort_sha256,
        batch_id=str(batch.get("batch_id") or ""),
        batch_ordinal=batch_ordinal,
        source_count=int(batch.get("source_count") or 0),
        source_ids=source_ids,
        source_tiers={source_id: tier_by_source.get(source_id, "") for source_id in source_ids},
    )
    return plan, authorization


def require_phase7d4b_write_confirmation(value: str) -> None:
    if str(value or "") != PHASE_7D4B_WRITE_CONFIRMATION:
        raise ProductionCohort23BackfillError(
            "Phase 7D4B writes require --confirm-production-writes "
            + PHASE_7D4B_WRITE_CONFIRMATION
        )


def build_phase7d4b_runner_config(
    authorization: Phase7D4BBatchAuthorization,
) -> Phase6ERunnerConfig:
    return Phase6ERunnerConfig(
        execution_mode="write",
        catalog_mode=authorization.catalog_mode,
        max_source_concurrency=authorization.source_concurrency,
        max_attempts=authorization.max_attempts,
        retry_backoff_seconds=1,
        max_jobs=authorization.max_jobs_per_source,
        max_pages=authorization.page_safety_cap,
        detail_concurrency=authorization.detail_concurrency,
        detail_retry_attempts=authorization.detail_retry_attempts,
        requests_per_minute=authorization.requests_per_minute,
        source_timeout_seconds=authorization.source_timeout_seconds,
        acquisition_timeout_seconds=authorization.acquisition_timeout_seconds,
        allow_llm_fallback=authorization.allow_llm_fallback,
        normalized_job_writes_enabled=True,
        lifecycle_reconciliation_enabled=False,
        deactivation_enabled=False,
    )


def batch_checkpoint_path(output_dir: Path, batch_ordinal: int) -> Path:
    return (
        Path(output_dir).resolve()
        / f"phase7d4b_batch_{batch_ordinal:02d}_checkpoint.json"
    )


def require_phase7d4b_checkpoint_state(
    *,
    output_dir: Path,
    authorization: Phase7D4BBatchAuthorization,
    resume_incomplete: bool,
) -> None:
    def require_same_rollout(report: Mapping[str, Any], ordinal: int) -> None:
        if (
            report.get("rollout_id") != authorization.rollout_id
            or report.get("rollout_plan_sha256")
            != authorization.rollout_plan_sha256
            or report.get("production_plan_id") != authorization.production_plan_id
            or report.get("cohort_sha256") != authorization.cohort_sha256
        ):
            raise ProductionCohort23BackfillError(
                f"Batch {ordinal:02d} checkpoint belongs to a different rollout"
            )

    for ordinal in range(1, authorization.batch_ordinal):
        previous = batch_checkpoint_path(output_dir, ordinal)
        if not previous.exists():
            raise ProductionCohort23BackfillError(
                f"Batch {ordinal:02d} checkpoint is missing; run batches in order"
            )
        report = read_phase7d4b_report(previous)
        require_same_rollout(report, ordinal)
        if report.get("status") not in _PROGRESSING_STATUSES:
            raise ProductionCohort23BackfillError(
                f"Batch {ordinal:02d} has unresolved safety blockers"
            )

    current = batch_checkpoint_path(output_dir, authorization.batch_ordinal)
    if not current.exists():
        return
    report = read_phase7d4b_report(current)
    require_same_rollout(report, authorization.batch_ordinal)
    if report.get("status") in _PROGRESSING_STATUSES:
        raise ProductionCohort23BackfillError(
            f"Batch {authorization.batch_ordinal:02d} already completed"
        )
    if not resume_incomplete:
        raise ProductionCohort23BackfillError(
            f"Batch {authorization.batch_ordinal:02d} has an incomplete checkpoint; "
            "review it and rerun with --resume-incomplete"
        )


def capture_phase7d4b_source_snapshot(
    database: "Database",
    *,
    source_ids: list[str],
) -> Phase7D4BSourceSnapshot:
    collection = database["jobs_current"]
    current: dict[str, int] = {}
    active: dict[str, int] = {}
    for source_id in source_ids:
        current[source_id] = int(
            collection.count_documents({"source_id": source_id})
        )
        active[source_id] = int(
            collection.count_documents(
                {"source_id": source_id, "is_active": True}
            )
        )
    return Phase7D4BSourceSnapshot(
        source_ids=list(source_ids),
        current_by_source=current,
        active_by_source=active,
        current_total=sum(current.values()),
        active_total=sum(active.values()),
    )


def build_phase7d4b_batch_report(
    *,
    authorization: Phase7D4BBatchAuthorization,
    manifest: "Phase6ERunManifest",
    before: Phase7D4BSourceSnapshot,
    after: Phase7D4BSourceSnapshot,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []
    complete_ids: list[str] = []
    partial_ids: list[str] = []
    deferred_ids: list[str] = []
    dispositions: dict[str, str] = {}

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
        "max_attempts": 2,
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
            dispositions[source_id] = "missing_terminal_result"
            deferred_ids.append(source_id)
            continue
        outcomes = (
            result.inserted_count
            + result.updated_count
            + result.unchanged_count
            + result.reactivated_count
        )
        if outcomes != result.accepted_count:
            block(f"source_write_accounting_mismatch:{source_id}")
        if result.rejected_count:
            block(f"source_persistence_or_validation_exception:{source_id}")
        complete = bool(
            result.status == "success"
            and result.catalog_mode == "complete_catalog"
            and result.catalog_complete
            and result.discovery_complete
            and result.discovered_count == result.attempted_count
            and result.accepted_count >= 1
            and result.quarantined_count == 0
            and result.rejected_count == 0
        )
        if complete:
            complete_ids.append(source_id)
            dispositions[source_id] = "complete_catalog"
        elif result.accepted_count > 0 and outcomes == result.accepted_count:
            partial_ids.append(source_id)
            dispositions[source_id] = (
                str(result.error_type or "").strip()
                or "productive_partial_catalog"
            )
        else:
            deferred_ids.append(source_id)
            dispositions[source_id] = (
                str(result.error_type or "").strip()
                or "zero_valid_jobs"
            )

    if manifest.completed_source_count != authorization.source_count:
        block("manifest_incomplete_source_execution")
    if after.current_total - before.current_total != manifest.inserted_job_count:
        block("database_current_delta_mismatch")
    if (
        after.active_total - before.active_total
        != manifest.inserted_job_count + manifest.reactivated_job_count
    ):
        block("database_active_delta_mismatch")

    counters = {
        "discovered": sum(
            result.discovered_count for result in manifest.source_results
        ),
        "attempted": sum(
            result.attempted_count for result in manifest.source_results
        ),
        "extracted": manifest.extracted_job_count,
        "accepted": manifest.accepted_job_count,
        "quarantined": manifest.quarantined_job_count,
        "rejected": sum(
            result.rejected_count for result in manifest.source_results
        ),
        "inserted": manifest.inserted_job_count,
        "updated": manifest.updated_job_count,
        "unchanged": manifest.unchanged_job_count,
        "reactivated": manifest.reactivated_job_count,
        "current_jobs_before": before.current_total,
        "current_jobs_after": after.current_total,
        "active_jobs_before": before.active_total,
        "active_jobs_after": after.active_total,
    }
    status = (
        "failed"
        if blockers
        else "passed_with_partial"
        if partial_ids or deferred_ids
        else "passed"
    )
    body = Phase7D4BBatchReportBody(
        status=status,
        ready_for_next_batch_or_closeout=not blockers,
        generated_at=generated_at or datetime.now(timezone.utc),
        rollout_id=authorization.rollout_id,
        rollout_plan_sha256=authorization.rollout_plan_sha256,
        production_plan_id=authorization.production_plan_id,
        cohort_sha256=authorization.cohort_sha256,
        batch_id=authorization.batch_id,
        batch_ordinal=authorization.batch_ordinal,
        run_id=manifest.run_id,
        source_count=authorization.source_count,
        source_ids=authorization.source_ids,
        source_tiers=authorization.source_tiers,
        successful_source_count=manifest.successful_source_count,
        complete_catalog_source_count=len(complete_ids),
        productive_partial_source_count=len(partial_ids),
        deferred_source_count=len(deferred_ids),
        productive_source_ids=complete_ids + partial_ids,
        complete_catalog_source_ids=complete_ids,
        productive_partial_source_ids=partial_ids,
        deferred_source_ids=deferred_ids,
        source_dispositions=dispositions,
        counters=counters,
        before=before,
        after=after,
        source_results=[
            result.model_dump(mode="json") for result in manifest.source_results
        ],
        blockers=blockers,
        controls={
            "production_writes_enabled": True,
            "promoted_sources_only": True,
            "catalog_mode": "complete_catalog",
            "max_jobs_per_source": None,
            "page_safety_cap": 500,
            "max_source_concurrency": 1,
            "detail_concurrency": 1,
            "gpu_llm_concurrency": 1,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "failed_or_partial_runs_increment_missing_count": False,
            "changed_only_downstream_processing": True,
        },
    )
    payload = body.model_dump(mode="json")
    payload["report_sha256"] = _canonical_sha256(payload)
    return payload


def read_phase7d4b_report(path: Path) -> dict[str, Any]:
    payload = _load_json(path, label="Phase 7D4B batch report")
    stored = str(payload.get("report_sha256") or "").strip().lower()
    unsigned = dict(payload)
    unsigned.pop("report_sha256", None)
    if not stored or stored != _canonical_sha256(unsigned):
        raise ProductionCohort23BackfillError(
            "Phase 7D4B report checksum is invalid"
        )
    try:
        Phase7D4BBatchReportBody.model_validate(unsigned)
    except (TypeError, ValueError) as exc:
        raise ProductionCohort23BackfillError(
            f"Invalid Phase 7D4B batch report: {exc}"
        ) from exc
    return payload


def write_phase7d4b_report(path: Path, payload: Mapping[str, Any]) -> Path:
    target = Path(path).resolve()
    candidate = dict(payload)
    stored = str(candidate.get("report_sha256") or "").strip().lower()
    unsigned = dict(candidate)
    unsigned.pop("report_sha256", None)
    if not stored or stored != _canonical_sha256(unsigned):
        raise ProductionCohort23BackfillError(
            "Candidate Phase 7D4B report checksum is invalid"
        )
    Phase7D4BBatchReportBody.model_validate(unsigned)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(candidate, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, target)
    return target

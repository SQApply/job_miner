from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel
from .production_ingestion import Phase6AIngestionPlan
from .production_pilot import Phase7D2PilotSelection, load_phase7d2a_pilot_inputs


PHASE_7D3A_CONTRACT_VERSION = "1.1"
PHASE_7D3A = "7D3A"
PHASE_7D3A_STATUS = "guarded_rollout_plan_ready"


class ProductionGuardedRolloutError(RuntimeError):
    """Raised when the Phase 7D3 rollout cannot be planned safely."""


class Phase7D3ABatch(ContractModel):
    batch_id: str = Field(min_length=1, max_length=200)
    ordinal: int = Field(ge=1)
    purpose: Literal["initial_backfill", "steady_state_rescrape"]
    status: Literal["pending"] = "pending"
    source_count: int = Field(ge=1)
    source_ids: list[str] = Field(min_length=1)
    catalog_mode: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def validate_batch(self) -> "Phase7D3ABatch":
        if self.source_count != len(self.source_ids):
            raise ValueError("Batch source_count does not match source_ids")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("Batch source_ids must be unique")
        if any(not source_id.strip() for source_id in self.source_ids):
            raise ValueError("Batch source_ids cannot contain empty values")
        return self


class Phase7D3ARolloutPlanBody(ContractModel):
    contract_version: Literal["1.1"] = PHASE_7D3A_CONTRACT_VERSION
    phase: Literal["7D3A"] = PHASE_7D3A
    status: Literal["guarded_rollout_plan_ready"] = PHASE_7D3A_STATUS
    ready_for_phase7d3b: bool = True
    rollout_id: str = Field(min_length=1, max_length=200)
    generated_at: datetime
    plan_id: str = Field(min_length=1)
    cohort_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    quality_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    semantic_closeout_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    full_source_count: int = Field(ge=1)
    full_source_ids: list[str] = Field(min_length=1)
    seeded_source_count: int = Field(ge=1)
    seeded_source_ids: list[str] = Field(min_length=1)
    seeded_job_count: int = Field(ge=1)
    initial_backfill_source_count: int = Field(ge=1)
    initial_backfill_source_ids: list[str] = Field(min_length=1)
    initial_backfill_batch_size: int = Field(ge=1, le=10)
    initial_backfill_batch_count: int = Field(ge=1)
    initial_backfill_batches: list[Phase7D3ABatch] = Field(min_length=1)
    steady_state_rescrape_source_count: int = Field(ge=1)
    steady_state_rescrape_source_ids: list[str] = Field(min_length=1)
    steady_state_rescrape_batch_size: int = Field(ge=1, le=10)
    steady_state_rescrape_batch_count: int = Field(ge=1)
    steady_state_rescrape_batches: list[Phase7D3ABatch] = Field(min_length=1)
    catalog_completion_policy: dict[str, Any]
    rescrape_policy: dict[str, Any]
    execution_limits: dict[str, Any]
    evidence: dict[str, Any]
    controls: dict[str, Any]

    @field_validator("generated_at")
    @classmethod
    def require_aware_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_partition_and_controls(self) -> "Phase7D3ARolloutPlanBody":
        if not self.ready_for_phase7d3b:
            raise ValueError("A stored Phase 7D3A plan must be ready for Phase 7D3B")
        if (
            self.full_source_count != 26
            or self.seeded_source_count != 2
            or self.seeded_job_count != 20
            or self.initial_backfill_source_count != 26
            or self.initial_backfill_batch_size != 4
            or self.initial_backfill_batch_count != 7
            or self.steady_state_rescrape_source_count != 26
            or self.steady_state_rescrape_batch_size != 4
            or self.steady_state_rescrape_batch_count != 7
        ):
            raise ValueError(
                "Phase 7D3A requires all 26 sources in complete backfill and recurring rescrapes"
            )
        if self.full_source_count != len(self.full_source_ids):
            raise ValueError("full_source_count does not match full_source_ids")
        if self.seeded_source_count != len(self.seeded_source_ids):
            raise ValueError("seeded_source_count does not match seeded_source_ids")
        if self.initial_backfill_source_count != len(self.initial_backfill_source_ids):
            raise ValueError(
                "initial_backfill_source_count does not match initial_backfill_source_ids"
            )
        if self.initial_backfill_batch_count != len(self.initial_backfill_batches):
            raise ValueError(
                "initial_backfill_batch_count does not match initial_backfill_batches"
            )
        if self.steady_state_rescrape_source_count != len(
            self.steady_state_rescrape_source_ids
        ):
            raise ValueError(
                "steady_state_rescrape_source_count does not match steady_state_rescrape_source_ids"
            )
        if self.steady_state_rescrape_batch_count != len(
            self.steady_state_rescrape_batches
        ):
            raise ValueError(
                "steady_state_rescrape_batch_count does not match steady_state_rescrape_batches"
            )
        for values, label in (
            (self.full_source_ids, "full_source_ids"),
            (self.seeded_source_ids, "seeded_source_ids"),
            (self.initial_backfill_source_ids, "initial_backfill_source_ids"),
            (self.steady_state_rescrape_source_ids, "steady_state_rescrape_source_ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} contains duplicates")
        if not set(self.seeded_source_ids).issubset(set(self.full_source_ids)):
            raise ValueError("Seeded sources are not part of the full cohort")
        if self.initial_backfill_source_ids != self.full_source_ids:
            raise ValueError("Initial backfill does not preserve frozen cohort order")
        if not set(self.seeded_source_ids).issubset(
            set(self.initial_backfill_source_ids)
        ):
            raise ValueError("Pilot-seeded sources are missing from complete backfill")
        if self.steady_state_rescrape_source_ids != self.full_source_ids:
            raise ValueError("Steady-state rescrape must contain the complete frozen cohort")
        if not set(self.seeded_source_ids).issubset(
            set(self.steady_state_rescrape_source_ids)
        ):
            raise ValueError("Seeded sources are missing from steady-state rescraping")

        batch_groups = (
            (
                "initial_backfill",
                self.initial_backfill_source_ids,
                self.initial_backfill_batch_size,
                self.initial_backfill_batch_count,
                self.initial_backfill_batches,
            ),
            (
                "steady_state_rescrape",
                self.steady_state_rescrape_source_ids,
                self.steady_state_rescrape_batch_size,
                self.steady_state_rescrape_batch_count,
                self.steady_state_rescrape_batches,
            ),
        )
        all_batch_ids: list[str] = []
        for purpose, expected_ids, batch_size, batch_count, batches in batch_groups:
            flattened = [
                source_id for batch in batches for source_id in batch.source_ids
            ]
            if flattened != expected_ids:
                raise ValueError(f"{purpose} batches do not preserve source order")
            if [batch.ordinal for batch in batches] != list(range(1, batch_count + 1)):
                raise ValueError(f"{purpose} batch ordinals are not contiguous")
            if any(batch.purpose != purpose for batch in batches):
                raise ValueError(f"{purpose} batch purpose is inconsistent")
            if any(batch.source_count > batch_size for batch in batches):
                raise ValueError(f"{purpose} batch exceeds the configured batch size")
            all_batch_ids.extend(batch.batch_id for batch in batches)
        if len(all_batch_ids) != len(set(all_batch_ids)):
            raise ValueError("Batch ids must be unique across rollout scopes")
        expected_complete_batch_sizes = [
            4,
            4,
            4,
            4,
            4,
            4,
            2,
        ]
        if [
            batch.source_count for batch in self.initial_backfill_batches
        ] != expected_complete_batch_sizes:
            raise ValueError("Initial backfill must use six batches of four and one of two")
        if [
            batch.source_count for batch in self.steady_state_rescrape_batches
        ] != expected_complete_batch_sizes:
            raise ValueError("Steady-state rescrape must use six batches of four and one of two")

        required_completion_policy = {
            "mode": "complete_catalog",
            "max_jobs_per_source": None,
            "stop_only_on_verified_exhaustion": True,
            "page_safety_cap": 500,
            "safety_cap_marks_run_incomplete": True,
            "resume_incomplete_sources": True,
            "partial_run_reconciliation_safe": False,
        }
        for key, expected in required_completion_policy.items():
            if self.catalog_completion_policy.get(key) != expected:
                raise ValueError(f"Unsafe or inconsistent catalog completion policy: {key}")

        required_rescrape_policy = {
            "cadence_hours": 72,
            "include_all_production_sources": True,
            "start_after_initial_backfill_closeout": True,
            "require_complete_successful_cycle": True,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "deactivation_requires_two_complete_successful_cycles": True,
            "changed_only_downstream_processing": True,
        }
        for key, expected in required_rescrape_policy.items():
            if self.rescrape_policy.get(key) != expected:
                raise ValueError(f"Unsafe or inconsistent rescrape policy: {key}")

        required_limits = {
            "max_source_concurrency": 1,
            "detail_concurrency": 1,
            "max_attempts": 2,
            "detail_retry_attempts": 1,
            "requests_per_minute": 30,
            "source_timeout_seconds": 1800,
            "acquisition_timeout_seconds": 25,
            "gpu_llm_concurrency": 1,
            "allow_llm_fallback": True,
        }
        for key, expected in required_limits.items():
            if self.execution_limits.get(key) != expected:
                raise ValueError(f"Unsafe or inconsistent execution limit: {key}")

        required_controls = {
            "plan_only": True,
            "network_requests_performed": False,
            "mongodb_reads_performed": False,
            "mongodb_writes_performed": False,
            "normalized_job_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "anti_bot_bypass_enabled": False,
            "automatic_rollback_enabled": False,
            "stop_after_failed_batch": True,
            "require_batch_evidence_before_next_batch": True,
            "pilot_seeded_sources_included_in_initial_backfill": True,
            "all_sources_included_in_steady_state_rescrape": True,
        }
        for key, expected in required_controls.items():
            if self.controls.get(key) is not expected:
                raise ValueError(f"Unsafe or inconsistent rollout control: {key}")
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
        raise ProductionGuardedRolloutError(
            f"{label} contains invalid JSON: {source}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProductionGuardedRolloutError(f"{label} must contain a JSON object")
    return payload


def _verify_checksum(
    payload: Mapping[str, Any],
    *,
    field: str,
    label: str,
) -> str:
    stored = str(payload.get(field) or "").strip().lower()
    unsigned = dict(payload)
    unsigned.pop(field, None)
    if not stored or stored != _canonical_sha256(unsigned):
        raise ProductionGuardedRolloutError(
            f"{label} checksum is missing or invalid"
        )
    return stored


def _ids(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ProductionGuardedRolloutError(f"{label} must be a JSON array")
    source_ids = [str(item or "").strip() for item in value]
    if any(not source_id for source_id in source_ids):
        raise ProductionGuardedRolloutError(f"{label} contains an empty source id")
    if len(source_ids) != len(set(source_ids)):
        raise ProductionGuardedRolloutError(f"{label} contains duplicate source ids")
    return source_ids


def _integer(value: Any, *, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_phase7d3a_evidence(
    *,
    cohort_path: Path,
    quality_report_path: Path,
    production_plan_path: Path,
    semantic_closeout_path: Path,
    expected_inventory: int = 102,
    expected_cohort_size: int = 26,
    expected_seeded_sources: int = 2,
    expected_seeded_jobs: int = 20,
    expected_updated_jobs: int = 2,
) -> tuple[
    Phase6AIngestionPlan,
    Phase7D2PilotSelection,
    dict[str, Any],
    str,
]:
    """Load Phase 7D1 and 7D2C.1 evidence without network or database access."""

    plan, selection = load_phase7d2a_pilot_inputs(
        cohort_path=cohort_path,
        quality_report_path=quality_report_path,
        plan_path=production_plan_path,
        expected_inventory=expected_inventory,
        expected_cohort_size=expected_cohort_size,
        expected_new_sources=expected_seeded_sources,
    )
    semantic = _load_json(
        semantic_closeout_path,
        label="Phase 7D2C.1 semantic closeout",
    )
    semantic_sha256 = _verify_checksum(
        semantic,
        field="report_sha256",
        label="Phase 7D2C.1 semantic closeout",
    )
    if (
        str(semantic.get("contract_version") or ""),
        str(semantic.get("phase") or ""),
        str(semantic.get("status") or ""),
        semantic.get("ready_for_phase7d3"),
    ) != ("1.0", "7D2C1", "passed_with_bounded_content_variance", True):
        raise ProductionGuardedRolloutError(
            "Phase 7D2C.1 did not authorize the guarded rollout"
        )
    if semantic.get("blockers") != []:
        raise ProductionGuardedRolloutError(
            "Phase 7D2C.1 semantic closeout contains blockers"
        )
    if str(semantic.get("plan_id") or "") != plan.plan_id:
        raise ProductionGuardedRolloutError(
            "Semantic closeout references a different production plan"
        )
    if str(semantic.get("cohort_sha256") or "") != plan.cohort_sha256:
        raise ProductionGuardedRolloutError(
            "Semantic closeout references a different frozen cohort"
        )
    semantic_sources = _ids(
        semantic.get("source_ids"),
        label="semantic_closeout.source_ids",
    )
    if semantic_sources != selection.source_ids:
        raise ProductionGuardedRolloutError(
            "Semantic closeout sources differ from the signed repair additions"
        )

    counts = semantic.get("semantic_idempotency")
    if not isinstance(counts, dict):
        raise ProductionGuardedRolloutError(
            "Semantic idempotency accounting is missing"
        )
    expected_counts = {
        "accepted": expected_seeded_jobs,
        "inserted": 0,
        "updated": expected_updated_jobs,
        "unchanged": expected_seeded_jobs - expected_updated_jobs,
        "reactivated": 0,
        "quarantined": 0,
        "maximum_updated_jobs": expected_updated_jobs,
        "maximum_updated_jobs_per_source": 1,
    }
    for key, expected in expected_counts.items():
        if _integer(counts.get(key)) != expected:
            raise ProductionGuardedRolloutError(
                f"Semantic idempotency count is inconsistent: {key}"
            )
    if counts.get("identity_reuse_proven_by_zero_inserts") is not True:
        raise ProductionGuardedRolloutError(
            "Semantic closeout did not prove stable identity reuse"
        )

    source_results = semantic.get("source_results")
    if not isinstance(source_results, list) or len(source_results) != expected_seeded_sources:
        raise ProductionGuardedRolloutError(
            "Semantic closeout source results are incomplete"
        )
    by_id: dict[str, dict[str, Any]] = {}
    for result in source_results:
        if not isinstance(result, dict):
            raise ProductionGuardedRolloutError(
                "Semantic closeout contains a non-object source result"
            )
        source_id = str(result.get("source_id") or "").strip()
        if not source_id or source_id in by_id:
            raise ProductionGuardedRolloutError(
                "Semantic closeout contains an invalid source result identity"
            )
        by_id[source_id] = result
    if set(by_id) != set(semantic_sources):
        raise ProductionGuardedRolloutError(
            "Semantic closeout source-result partition is inconsistent"
        )
    expected_jobs_per_source = expected_seeded_jobs // expected_seeded_sources
    for source_id in semantic_sources:
        result = by_id[source_id]
        required = {
            "accepted": expected_jobs_per_source,
            "inserted": 0,
            "updated": 1,
            "unchanged": expected_jobs_per_source - 1,
            "reactivated": 0,
            "quarantined": 0,
            "rejected": 0,
        }
        for key, expected in required.items():
            if _integer(result.get(key)) != expected:
                raise ProductionGuardedRolloutError(
                    f"Semantic source result differs for {source_id}: {key}"
                )

    database_checks = semantic.get("database_checks")
    if not isinstance(database_checks, dict):
        raise ProductionGuardedRolloutError(
            "Semantic closeout database checks are missing"
        )
    required_checks = {
        "fleet_run_records": 2,
        "source_run_records_first": expected_seeded_sources,
        "source_run_records_rerun": expected_seeded_sources,
        "current_jobs_observed_on_rerun": expected_seeded_jobs,
        "active_jobs_observed_on_rerun": expected_seeded_jobs,
        "duplicate_identity_hashes": 0,
        "duplicate_external_job_keys": 0,
        "history_mismatches": 0,
        "unsafe_deactivated_jobs": 0,
        "quarantine_records_first": 0,
        "quarantine_records_rerun": 0,
    }
    for key, expected in required_checks.items():
        if _integer(database_checks.get(key)) != expected:
            raise ProductionGuardedRolloutError(
                f"Semantic closeout database check failed: {key}"
            )

    controls = semantic.get("controls")
    if not isinstance(controls, dict):
        raise ProductionGuardedRolloutError(
            "Semantic closeout controls are missing"
        )
    required_controls = {
        "network_requests_performed": False,
        "scraping_performed": False,
        "mongodb_reads_performed": False,
        "mongodb_writes_performed": False,
        "normalized_job_writes_enabled": False,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
        "original_strict_failure_preserved": True,
        "variance_bound_relaxed": False,
    }
    for key, expected in required_controls.items():
        if controls.get(key) is not expected:
            raise ProductionGuardedRolloutError(
                f"Unsafe or inconsistent semantic-closeout control: {key}"
            )

    evidence = semantic.get("evidence")
    if not isinstance(evidence, dict):
        raise ProductionGuardedRolloutError(
            "Semantic closeout evidence links are missing"
        )
    required_evidence_keys = {
        "phase7d2b_report_sha256",
        "first_write_manifest_sha256",
        "phase7d2c_strict_report_sha256",
        "rerun_manifest_sha256",
        "phase6f_closeout_sha256",
    }
    if set(evidence) != required_evidence_keys or any(
        len(str(evidence.get(key) or "")) != 64
        or any(character not in "0123456789abcdef" for character in str(evidence.get(key) or ""))
        for key in required_evidence_keys
    ):
        raise ProductionGuardedRolloutError(
            "Semantic closeout evidence links are incomplete"
        )
    strict_gate = semantic.get("strict_gate")
    if (
        not isinstance(strict_gate, dict)
        or strict_gate.get("status") != "failed"
        or strict_gate.get("ready_for_phase7d3") is not False
        or not isinstance(strict_gate.get("blockers"), list)
        or not strict_gate.get("blockers")
        or any(
            "updated_job_count" not in str(blocker)
            and "unchanged_job_count" not in str(blocker)
            and ":updated_count" not in str(blocker)
            and ":unchanged_count" not in str(blocker)
            for blocker in strict_gate.get("blockers")
        )
    ):
        raise ProductionGuardedRolloutError(
            "Semantic closeout does not preserve the bounded strict-gate failure"
        )
    return plan, selection, semantic, semantic_sha256


def build_phase7d3a_rollout_plan(
    *,
    production_plan: Phase6AIngestionPlan,
    selection: Phase7D2PilotSelection,
    semantic_closeout: Mapping[str, Any],
    semantic_closeout_sha256: str,
    batch_size: int = 4,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    if batch_size != 4:
        raise ProductionGuardedRolloutError(
            "Production Phase 7D3A uses an immutable batch size of four"
        )
    seeded_source_ids = [str(value or "") for value in semantic_closeout.get("source_ids") or []]
    if seeded_source_ids != selection.source_ids:
        raise ProductionGuardedRolloutError(
            "Seeded sources differ from the signed Phase 7D2 selection"
        )
    full_source_ids = list(production_plan.selected_source_ids)
    seeded_set = set(seeded_source_ids)
    if len(seeded_set) != len(seeded_source_ids) or not seeded_set.issubset(
        set(full_source_ids)
    ):
        raise ProductionGuardedRolloutError(
            "Seeded sources are not a unique subset of the frozen cohort"
        )
    initial_backfill_source_ids = list(full_source_ids)
    if (
        len(full_source_ids) != 26
        or len(seeded_source_ids) != 2
        or len(initial_backfill_source_ids) != 26
    ):
        raise ProductionGuardedRolloutError(
            "Production rollout must contain all 26 sources in complete backfill"
        )
    if not initial_backfill_source_ids:
        raise ProductionGuardedRolloutError("No initial-backfill sources remain")

    rollout_seed = {
        "plan_id": production_plan.plan_id,
        "cohort_sha256": production_plan.cohort_sha256,
        "semantic_closeout_sha256": semantic_closeout_sha256,
        "seeded_source_ids": seeded_source_ids,
        "initial_backfill_source_ids": initial_backfill_source_ids,
        "steady_state_rescrape_source_ids": full_source_ids,
        "batch_size": batch_size,
        "rescrape_cadence_hours": 72,
    }
    rollout_id = (
        "phase7d3a_"
        + production_plan.cohort_sha256[:12]
        + "_"
        + _canonical_sha256(rollout_seed)[:12]
    )
    def make_batches(
        purpose: Literal["initial_backfill", "steady_state_rescrape"],
        source_scope: list[str],
    ) -> list[Phase7D3ABatch]:
        batches: list[Phase7D3ABatch] = []
        for index in range(0, len(source_scope), batch_size):
            source_ids = source_scope[index : index + batch_size]
            ordinal = len(batches) + 1
            batch_seed = {
                "rollout_id": rollout_id,
                "purpose": purpose,
                "ordinal": ordinal,
                "source_ids": source_ids,
            }
            batches.append(
                Phase7D3ABatch(
                    batch_id=(
                        f"{rollout_id}_{purpose}_{ordinal:02d}_"
                        + _canonical_sha256(batch_seed)[:10]
                    ),
                    ordinal=ordinal,
                    purpose=purpose,
                    source_count=len(source_ids),
                    source_ids=source_ids,
                )
            )
        return batches

    initial_backfill_batches = make_batches(
        "initial_backfill",
        initial_backfill_source_ids,
    )
    steady_state_rescrape_batches = make_batches(
        "steady_state_rescrape",
        full_source_ids,
    )

    semantic_counts = semantic_closeout.get("semantic_idempotency") or {}
    body = Phase7D3ARolloutPlanBody(
        rollout_id=rollout_id,
        generated_at=generated_at or datetime.now(timezone.utc),
        plan_id=production_plan.plan_id,
        cohort_sha256=production_plan.cohort_sha256,
        quality_report_sha256=selection.quality_report_sha256,
        semantic_closeout_sha256=semantic_closeout_sha256,
        full_source_count=len(full_source_ids),
        full_source_ids=full_source_ids,
        seeded_source_count=len(seeded_source_ids),
        seeded_source_ids=seeded_source_ids,
        seeded_job_count=_integer(semantic_counts.get("accepted"), default=0),
        initial_backfill_source_count=len(initial_backfill_source_ids),
        initial_backfill_source_ids=initial_backfill_source_ids,
        initial_backfill_batch_size=batch_size,
        initial_backfill_batch_count=len(initial_backfill_batches),
        initial_backfill_batches=initial_backfill_batches,
        steady_state_rescrape_source_count=len(full_source_ids),
        steady_state_rescrape_source_ids=full_source_ids,
        steady_state_rescrape_batch_size=batch_size,
        steady_state_rescrape_batch_count=len(steady_state_rescrape_batches),
        steady_state_rescrape_batches=steady_state_rescrape_batches,
        catalog_completion_policy={
            "mode": "complete_catalog",
            "max_jobs_per_source": None,
            "stop_only_on_verified_exhaustion": True,
            "page_safety_cap": 500,
            "safety_cap_marks_run_incomplete": True,
            "resume_incomplete_sources": True,
            "partial_run_reconciliation_safe": False,
        },
        rescrape_policy={
            "cadence_hours": 72,
            "include_all_production_sources": True,
            "start_after_initial_backfill_closeout": True,
            "require_complete_successful_cycle": True,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "deactivation_requires_two_complete_successful_cycles": True,
            "changed_only_downstream_processing": True,
        },
        execution_limits={
            "max_source_concurrency": 1,
            "detail_concurrency": 1,
            "max_attempts": 2,
            "detail_retry_attempts": 1,
            "requests_per_minute": 30,
            "source_timeout_seconds": 1800,
            "acquisition_timeout_seconds": 25,
            "gpu_llm_concurrency": 1,
            "allow_llm_fallback": True,
        },
        evidence={
            "phase7d2c1_generated_from_run_id": semantic_closeout.get(
                "generated_from_run_id"
            ),
            "semantic_closeout_sha256": semantic_closeout_sha256,
            "phase7d2_evidence": dict(semantic_closeout.get("evidence") or {}),
        },
        controls={
            "plan_only": True,
            "network_requests_performed": False,
            "mongodb_reads_performed": False,
            "mongodb_writes_performed": False,
            "normalized_job_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "anti_bot_bypass_enabled": False,
            "automatic_rollback_enabled": False,
            "stop_after_failed_batch": True,
            "require_batch_evidence_before_next_batch": True,
            "pilot_seeded_sources_included_in_initial_backfill": True,
            "all_sources_included_in_steady_state_rescrape": True,
        },
    )
    payload = body.model_dump(mode="json")
    payload["plan_sha256"] = _canonical_sha256(payload)
    return payload


def read_phase7d3a_rollout_plan(path: Path) -> dict[str, Any]:
    payload = _load_json(path, label="Phase 7D3A guarded rollout plan")
    _verify_checksum(
        payload,
        field="plan_sha256",
        label="Phase 7D3A guarded rollout plan",
    )
    unsigned = dict(payload)
    unsigned.pop("plan_sha256", None)
    try:
        Phase7D3ARolloutPlanBody.model_validate(unsigned)
    except (TypeError, ValueError) as exc:
        raise ProductionGuardedRolloutError(
            f"Invalid Phase 7D3A guarded rollout plan: {exc}"
        ) from exc
    return payload


def _semantic_plan_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    ignored = {"generated_at", "plan_sha256"}
    return {key: value for key, value in payload.items() if key not in ignored}


def write_phase7d3a_rollout_plan(
    path: Path,
    payload: Mapping[str, Any],
) -> tuple[Path, dict[str, Any], bool]:
    target = Path(path).resolve()
    candidate = dict(payload)
    _verify_checksum(
        candidate,
        field="plan_sha256",
        label="Candidate Phase 7D3A guarded rollout plan",
    )
    candidate_unsigned = dict(candidate)
    candidate_unsigned.pop("plan_sha256", None)
    Phase7D3ARolloutPlanBody.model_validate(candidate_unsigned)
    if target.exists():
        existing = read_phase7d3a_rollout_plan(target)
        if _semantic_plan_view(existing) != _semantic_plan_view(candidate):
            raise ProductionGuardedRolloutError(
                "Existing Phase 7D3A plan differs; refusing to overwrite rollout state"
            )
        return target, existing, False

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(candidate, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, target)
    return target, candidate, True

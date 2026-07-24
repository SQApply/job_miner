from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .certification import PortalInventoryEntry, read_portal_inventory
from .production_target25 import read_target25_plan, validate_target25_plan
from .production_target25_repair import (
    PHASE_7D3C1,
    PHASE_7D3C1_CONTRACT_VERSION,
)


PHASE_7D4A_CONTRACT_VERSION = "1.0"
PHASE_7D4A = "7D4A"
PHASE_7D4A_COHORT_STATUS = "frozen_truthful_23_source_cohort"
PHASE_7D4A_ROLLOUT_STATUS = "truthful_23_source_rollout_plan_ready"


class ProductionCohort23Error(RuntimeError):
    """Raised when the signed 23-source production evidence is inconsistent."""


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
        raise ProductionCohort23Error(
            f"{label} contains invalid JSON: {source}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProductionCohort23Error(f"{label} must contain a JSON object")
    return payload


def _source_ids(values: Any, *, label: str) -> list[str]:
    if not isinstance(values, list):
        raise ProductionCohort23Error(f"{label} must be a JSON array")
    normalized = [str(value or "").strip() for value in values]
    if any(not value for value in normalized):
        raise ProductionCohort23Error(f"{label} contains an empty source id")
    if len(normalized) != len(set(normalized)):
        raise ProductionCohort23Error(f"{label} contains duplicate source ids")
    return normalized


def _source_object_ids(values: Any, *, label: str) -> list[str]:
    if not isinstance(values, list):
        raise ProductionCohort23Error(f"{label} must be a JSON array")
    if any(not isinstance(value, dict) for value in values):
        raise ProductionCohort23Error(f"{label} contains a non-object source")
    return _source_ids(
        [value.get("source_id") for value in values],
        label=f"{label} source ids",
    )


def _require_exact_ints(
    payload: Mapping[str, Any],
    expected: Mapping[str, int],
    *,
    label: str,
) -> None:
    for key, expected_value in expected.items():
        try:
            actual = int(payload.get(key))
        except (TypeError, ValueError):
            actual = -1
        if actual != expected_value:
            raise ProductionCohort23Error(
                f"{label}.{key} must be {expected_value}, found {actual}"
            )


def load_phase7d4a_policy(path: Path) -> dict[str, Any]:
    policy = _load_json(path, label="Phase 7D4A 23-source policy")
    descriptor = (
        str(policy.get("contract_version") or ""),
        str(policy.get("phase") or ""),
        str(policy.get("policy_status") or ""),
    )
    if descriptor != (
        PHASE_7D4A_CONTRACT_VERSION,
        PHASE_7D4A,
        "truthful_23_source_rollout_policy",
    ):
        raise ProductionCohort23Error("Unsupported Phase 7D4A policy")
    _require_exact_ints(
        policy,
        {
            "expected_inventory_count": 102,
            "expected_current_complete_count": 5,
            "expected_current_partial_safe_count": 11,
            "expected_candidate_complete_count": 2,
            "expected_candidate_partial_safe_count": 5,
            "expected_failed_candidate_count": 2,
            "expected_final_complete_count": 7,
            "expected_final_partial_safe_count": 16,
            "expected_final_recurring_count": 23,
            "expected_promoted_backfill_count": 7,
            "expected_deferred_count": 79,
        },
        label="policy",
    )
    limits = policy.get("execution_limits")
    if not isinstance(limits, dict):
        raise ProductionCohort23Error("Phase 7D4A execution limits are missing")
    expected_limits: dict[str, int | float] = {
        "source_concurrency": 1,
        "detail_concurrency": 1,
        "gpu_llm_concurrency": 1,
        "requests_per_minute": 30,
        "source_timeout_seconds": 1800,
        "acquisition_timeout_seconds": 25,
        "page_safety_cap": 500,
        "batch_size": 4,
    }
    for key, expected_value in expected_limits.items():
        if limits.get(key) != expected_value:
            raise ProductionCohort23Error(
                f"Unsafe or inconsistent Phase 7D4A execution limit: {key}"
            )
    initial = policy.get("initial_backfill_policy")
    recurring = policy.get("recurring_policy")
    lifecycle = policy.get("lifecycle_policy")
    safety = policy.get("safety")
    if not all(
        isinstance(value, dict)
        for value in (initial, recurring, lifecycle, safety)
    ):
        raise ProductionCohort23Error("Phase 7D4A policy sections are missing")
    if initial.get("source_scope") != "newly_promoted_sources_only":
        raise ProductionCohort23Error(
            "Initial backfill must include only newly promoted sources"
        )
    if initial.get("catalog_mode") != "complete_catalog":
        raise ProductionCohort23Error(
            "Initial backfill must attempt complete-catalog discovery"
        )
    if initial.get("max_jobs_per_source") is not None:
        raise ProductionCohort23Error(
            "Initial production backfill cannot retain the ten-job test limit"
        )
    if int(recurring.get("cadence_hours") or 0) != 72:
        raise ProductionCohort23Error("Recurring cadence must be 72 hours")
    if recurring.get("include_all_frozen_sources") is not True:
        raise ProductionCohort23Error(
            "Recurring execution must include all 23 frozen sources"
        )
    if recurring.get("max_jobs_per_source") is not None:
        raise ProductionCohort23Error(
            "Recurring production execution cannot retain the ten-job test limit"
        )
    complete = lifecycle.get("complete_catalog")
    partial = lifecycle.get("partial_safe")
    if not isinstance(complete, dict) or not isinstance(partial, dict):
        raise ProductionCohort23Error("Tier lifecycle policies are missing")
    if complete.get("missing_reconciliation_enabled") is not False:
        raise ProductionCohort23Error(
            "Complete sources require two clean cycles before reconciliation"
        )
    if int(
        complete.get("clean_complete_cycles_required_before_reconciliation") or 0
    ) != 2:
        raise ProductionCohort23Error(
            "Complete sources must require two clean complete cycles"
        )
    for key in (
        "missing_reconciliation_enabled",
        "deactivation_enabled",
        "incomplete_cycles_may_increment_missing_count",
    ):
        if partial.get(key) is not False:
            raise ProductionCohort23Error(
                f"Partial-safe lifecycle control {key} must be false"
            )
    required_false = (
        "network_requests_performed",
        "mongodb_reads_performed",
        "mongodb_writes_performed",
        "lifecycle_reconciliation_enabled",
        "deactivation_enabled",
        "anti_bot_bypass_enabled",
    )
    if safety.get("plan_only") is not True:
        raise ProductionCohort23Error("Phase 7D4A must remain plan-only")
    for key in required_false:
        if safety.get(key) is not False:
            raise ProductionCohort23Error(
                f"Phase 7D4A safety control {key} must be false"
            )
    return policy


def validate_phase7d3c1_final_report(payload: Mapping[str, Any]) -> None:
    candidate = dict(payload)
    stored_hash = str(candidate.pop("report_sha256", "") or "")
    if not stored_hash or stored_hash != _canonical_sha256(candidate):
        raise ProductionCohort23Error(
            "Phase 7D3C1 final report checksum is missing or invalid"
        )
    if (
        candidate.get("contract_version") != PHASE_7D3C1_CONTRACT_VERSION
        or candidate.get("phase") != PHASE_7D3C1
        or candidate.get("status") != "target_25_not_reached"
    ):
        raise ProductionCohort23Error(
            "Unsupported Phase 7D3C1 final report descriptor"
        )
    counts = candidate.get("counts")
    if not isinstance(counts, dict):
        raise ProductionCohort23Error("Phase 7D3C1 final counts are missing")
    _require_exact_ints(
        counts,
        {
            "previously_usable": 16,
            "candidate_count": 9,
            "candidate_complete_catalog": 2,
            "candidate_partial_safe": 5,
            "candidate_not_productive_or_not_run": 2,
            "promoted": 7,
            "final_recurring_usable": 23,
            "target": 25,
            "shortfall": 2,
        },
        label="final_report.counts",
    )
    if candidate.get("target_achieved") is not False:
        raise ProductionCohort23Error(
            "Phase 7D4A requires the truthful 23-of-25 closeout"
        )
    controls = candidate.get("controls")
    if not isinstance(controls, dict):
        raise ProductionCohort23Error("Phase 7D3C1 controls are missing")
    for key in (
        "mongodb_reads_performed",
        "mongodb_writes_performed",
        "lifecycle_reconciliation_enabled",
        "deactivation_enabled",
        "partial_safe_missing_reconciliation_enabled",
        "anti_bot_bypass_enabled",
    ):
        if controls.get(key) is not False:
            raise ProductionCohort23Error(
                f"Unsafe Phase 7D3C1 final control: {key}"
            )


def read_phase7d3c1_final_report(path: Path) -> dict[str, Any]:
    payload = _load_json(path, label="Phase 7D3C1 final report")
    validate_phase7d3c1_final_report(payload)
    return payload


def _validate_partitions(
    *,
    plan: Mapping[str, Any],
    final_report: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, list[str]]:
    validate_target25_plan(plan)
    validate_phase7d3c1_final_report(final_report)
    if final_report.get("plan_id") != plan.get("plan_id"):
        raise ProductionCohort23Error(
            "Phase 7D3C1 final report belongs to a different target-25 plan"
        )
    if final_report.get("plan_sha256") != plan.get("plan_sha256"):
        raise ProductionCohort23Error(
            "Phase 7D3C1 final report plan checksum does not match"
        )
    current_complete = _source_object_ids(
        plan.get("complete_catalog_sources"),
        label="current complete sources",
    )
    current_partial = _source_object_ids(
        plan.get("partial_safe_sources"),
        label="current partial-safe sources",
    )
    candidate_ids = _source_ids(
        plan.get("promotion_candidate_source_ids"),
        label="promotion candidate source ids",
    )
    candidate_complete = _source_ids(
        final_report.get("complete_candidate_source_ids"),
        label="complete candidate source ids",
    )
    candidate_partial = _source_ids(
        final_report.get("partial_safe_candidate_source_ids"),
        label="partial-safe candidate source ids",
    )
    failed = _source_ids(
        final_report.get("failed_candidate_source_ids"),
        label="failed candidate source ids",
    )
    expected_counts = {
        "current_complete": int(policy["expected_current_complete_count"]),
        "current_partial": int(policy["expected_current_partial_safe_count"]),
        "candidate_complete": int(policy["expected_candidate_complete_count"]),
        "candidate_partial": int(policy["expected_candidate_partial_safe_count"]),
        "failed": int(policy["expected_failed_candidate_count"]),
    }
    actual_values = {
        "current_complete": current_complete,
        "current_partial": current_partial,
        "candidate_complete": candidate_complete,
        "candidate_partial": candidate_partial,
        "failed": failed,
    }
    for key, values in actual_values.items():
        if len(values) != expected_counts[key]:
            raise ProductionCohort23Error(
                f"Phase 7D4A partition {key} has {len(values)} sources; "
                f"expected {expected_counts[key]}"
            )
    current = current_complete + current_partial
    if current != list(plan.get("current_recurring_source_ids") or []):
        raise ProductionCohort23Error(
            "Target-25 current recurring source order is inconsistent"
        )
    candidate_groups = candidate_complete + candidate_partial + failed
    if len(candidate_groups) != len(set(candidate_groups)):
        raise ProductionCohort23Error(
            "Candidate complete, partial-safe, and failed partitions overlap"
        )
    if set(candidate_groups) != set(candidate_ids):
        raise ProductionCohort23Error(
            "Candidate partitions do not account for all nine candidates"
        )
    promoted = candidate_complete + candidate_partial
    final_recurring = _source_ids(
        final_report.get("final_recurring_source_ids"),
        label="final recurring source ids",
    )
    if final_recurring != current + promoted:
        raise ProductionCohort23Error(
            "Phase 7D3C1 final recurring source order is inconsistent"
        )
    results = final_report.get("results")
    if not isinstance(results, list) or len(results) != len(candidate_ids):
        raise ProductionCohort23Error(
            "Phase 7D3C1 final candidate results are incomplete"
        )
    result_ids = _source_ids(
        [result.get("source_id") for result in results if isinstance(result, dict)],
        label="final candidate result source ids",
    )
    if set(result_ids) != set(candidate_ids):
        raise ProductionCohort23Error(
            "Phase 7D3C1 final candidate results do not match the plan"
        )
    return {
        "current_complete": current_complete,
        "current_partial": current_partial,
        "candidate_complete": candidate_complete,
        "candidate_partial": candidate_partial,
        "failed": failed,
        "promoted": promoted,
        "final_recurring": final_recurring,
    }


def _batches(
    source_ids: Sequence[str],
    *,
    batch_size: int,
    prefix: str,
) -> list[dict[str, Any]]:
    values = list(source_ids)
    return [
        {
            "batch_id": f"{prefix}_{ordinal:02d}",
            "batch_ordinal": ordinal,
            "source_count": len(values[start : start + batch_size]),
            "source_ids": values[start : start + batch_size],
        }
        for ordinal, start in enumerate(range(0, len(values), batch_size), start=1)
    ]


def _evidence_for_source(
    source_id: str,
    *,
    plan: Mapping[str, Any],
    final_report: Mapping[str, Any],
) -> dict[str, Any]:
    for key in ("complete_catalog_sources", "partial_safe_sources"):
        for source in plan.get(key) or []:
            if (
                isinstance(source, dict)
                and str(source.get("source_id") or "") == source_id
            ):
                evidence = source.get("phase7d3b_evidence")
                value = evidence if isinstance(evidence, dict) else {}
                return {
                    "origin": "phase7d3b_guarded_execution",
                    "status": value.get("status"),
                    "discovered_jobs": int(value.get("discovered_count") or 0),
                    "accepted_jobs": int(value.get("accepted_count") or 0),
                    "catalog_complete": value.get("catalog_complete") is True,
                    "error_type": value.get("error_type"),
                }
    for result in final_report.get("results") or []:
        if (
            isinstance(result, dict)
            and str(result.get("source_id") or "") == source_id
        ):
            return {
                "origin": "phase7d3c_candidate_certification",
                "status": result.get("status"),
                "discovered_jobs": int(result.get("discovered_urls") or 0),
                "accepted_jobs": int(result.get("extracted_jobs") or 0),
                "catalog_complete": result.get("catalog_complete") is True,
                "error_type": result.get("error_type"),
            }
    raise ProductionCohort23Error(
        f"No production evidence exists for source {source_id}"
    )


def build_phase7d4a_artifacts(
    *,
    inventory: Sequence[PortalInventoryEntry],
    target25_plan: Mapping[str, Any],
    final_report: Mapping[str, Any],
    policy: Mapping[str, Any],
    evidence: Mapping[str, Any],
    generated_at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    partitions = _validate_partitions(
        plan=target25_plan,
        final_report=final_report,
        policy=policy,
    )
    expected_inventory = int(policy["expected_inventory_count"])
    if len(inventory) != expected_inventory:
        raise ProductionCohort23Error(
            f"Expected {expected_inventory} inventory sources, found {len(inventory)}"
        )
    inventory_ids = [entry.source_id for entry in inventory]
    if len(inventory_ids) != len(set(inventory_ids)):
        raise ProductionCohort23Error("Portal inventory contains duplicate source ids")
    inventory_by_id = {entry.source_id: entry for entry in inventory}
    missing = [
        source_id
        for source_id in partitions["final_recurring"] + partitions["failed"]
        if source_id not in inventory_by_id
    ]
    if missing:
        raise ProductionCohort23Error(
            "Phase 7D4A evidence contains sources outside the inventory: "
            + ", ".join(missing)
        )
    complete_set = set(
        partitions["current_complete"] + partitions["candidate_complete"]
    )
    partial_set = set(
        partitions["current_partial"] + partitions["candidate_partial"]
    )
    recurring_set = complete_set | partial_set
    ordered_complete = [
        source_id for source_id in inventory_ids if source_id in complete_set
    ]
    ordered_partial = [
        source_id for source_id in inventory_ids if source_id in partial_set
    ]
    ordered_recurring = [
        source_id for source_id in inventory_ids if source_id in recurring_set
    ]
    promoted_set = set(partitions["promoted"])
    ordered_promoted = [
        source_id for source_id in inventory_ids if source_id in promoted_set
    ]
    ordered_failed = [
        source_id
        for source_id in inventory_ids
        if source_id in set(partitions["failed"])
    ]
    ordered_deferred = [
        source_id for source_id in inventory_ids if source_id not in recurring_set
    ]
    expected = {
        "complete": int(policy["expected_final_complete_count"]),
        "partial": int(policy["expected_final_partial_safe_count"]),
        "recurring": int(policy["expected_final_recurring_count"]),
        "promoted": int(policy["expected_promoted_backfill_count"]),
        "deferred": int(policy["expected_deferred_count"]),
    }
    actual = {
        "complete": len(ordered_complete),
        "partial": len(ordered_partial),
        "recurring": len(ordered_recurring),
        "promoted": len(ordered_promoted),
        "deferred": len(ordered_deferred),
    }
    if actual != expected:
        raise ProductionCohort23Error(
            f"Phase 7D4A cohort counts are inconsistent: {actual}"
        )
    if set(ordered_recurring) & set(ordered_deferred):
        raise ProductionCohort23Error("Recurring and deferred source sets overlap")
    if set(ordered_promoted) - set(ordered_recurring):
        raise ProductionCohort23Error(
            "Promoted backfill sources must belong to the recurring cohort"
        )
    now = generated_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ProductionCohort23Error("generated_at must be timezone-aware")
    generated_at_text = now.isoformat()
    sources: list[dict[str, Any]] = []
    for source_id in ordered_recurring:
        entry = inventory_by_id[source_id]
        tier = "complete_catalog" if source_id in complete_set else "partial_safe"
        source_evidence = _evidence_for_source(
            source_id,
            plan=target25_plan,
            final_report=final_report,
        )
        sources.append(
            {
                "source_id": source_id,
                "source_row": entry.source_row,
                "display_name": entry.display_name,
                "listing_url": entry.listing_url,
                "tier": tier,
                "evidence": source_evidence,
                "execution": {
                    "recurring_enabled": True,
                    "initial_backfill_required": source_id in promoted_set,
                    "catalog_mode": "complete_catalog",
                    "max_jobs_per_source": None,
                    "upsert_new_and_changed_jobs": True,
                    "skip_unchanged_jobs": True,
                    "changed_only_downstream_processing": True,
                },
                "lifecycle": {
                    "missing_reconciliation_enabled": False,
                    "deactivation_enabled": False,
                    "clean_complete_cycles_required": 2,
                    "incomplete_cycles_may_increment_missing_count": False,
                },
            }
        )
    cohort: dict[str, Any] = {
        "contract_version": PHASE_7D4A_CONTRACT_VERSION,
        "phase": PHASE_7D4A,
        "cohort_status": PHASE_7D4A_COHORT_STATUS,
        "frozen_at": generated_at_text,
        "inventory": {
            "path": str(evidence.get("inventory_name") or ""),
            "sha256": str(evidence.get("inventory_sha256") or ""),
            "source_count": len(inventory_ids),
        },
        "evidence": {
            "target25_plan_id": target25_plan["plan_id"],
            "target25_plan_sha256": target25_plan["plan_sha256"],
            "target25_plan_file_sha256": str(
                evidence.get("target25_plan_file_sha256") or ""
            ),
            "phase7d3c1_report_sha256": final_report["report_sha256"],
            "phase7d3c1_report_file_sha256": str(
                evidence.get("final_report_file_sha256") or ""
            ),
            "policy_sha256": str(evidence.get("policy_sha256") or ""),
        },
        "counts": {
            "inventory": len(inventory_ids),
            "complete_catalog": len(ordered_complete),
            "partial_safe": len(ordered_partial),
            "recurring_usable": len(ordered_recurring),
            "promoted_backfill": len(ordered_promoted),
            "failed_candidates_excluded": len(ordered_failed),
            "deferred": len(ordered_deferred),
        },
        "cohort_source_count": len(ordered_recurring),
        "deferred_source_count": len(ordered_deferred),
        "cohort_source_ids": ordered_recurring,
        "complete_catalog_source_ids": ordered_complete,
        "partial_safe_source_ids": ordered_partial,
        "promoted_backfill_source_ids": ordered_promoted,
        "failed_candidate_source_ids": ordered_failed,
        "deferred_source_ids": ordered_deferred,
        "sources": sources,
        "safety": {
            "tiered_recurring_ingestion_enabled": True,
            "partial_safe_sources_included": True,
            "production_ingestion_enabled": False,
            "network_requests_performed": False,
            "mongodb_reads_performed": False,
            "mongodb_writes_performed": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "failed_or_partial_runs_may_increment_missing_count": False,
            "two_clean_complete_cycles_required_before_reconciliation": True,
            "changed_only_downstream_processing": True,
        },
    }
    cohort["cohort_sha256"] = _canonical_sha256(cohort)
    batch_size = int(policy["execution_limits"]["batch_size"])
    initial_batches = _batches(
        ordered_promoted,
        batch_size=batch_size,
        prefix="phase7d4a_backfill",
    )
    recurring_batches = _batches(
        ordered_recurring,
        batch_size=batch_size,
        prefix="phase7d4a_recurring",
    )
    rollout_seed = {
        "cohort_sha256": cohort["cohort_sha256"],
        "promoted_backfill_source_ids": ordered_promoted,
        "steady_state_source_ids": ordered_recurring,
        "cadence_hours": policy["recurring_policy"]["cadence_hours"],
    }
    rollout: dict[str, Any] = {
        "contract_version": PHASE_7D4A_CONTRACT_VERSION,
        "phase": PHASE_7D4A,
        "status": PHASE_7D4A_ROLLOUT_STATUS,
        "rollout_id": "phase7d4a_" + _canonical_sha256(rollout_seed)[:20],
        "generated_at": generated_at_text,
        "cohort_sha256": cohort["cohort_sha256"],
        "counts": dict(cohort["counts"]),
        "initial_backfill": {
            "source_scope": "newly_promoted_sources_only",
            "source_count": len(ordered_promoted),
            "source_ids": ordered_promoted,
            "batch_size": batch_size,
            "batch_count": len(initial_batches),
            "batches": initial_batches,
            "catalog_mode": "complete_catalog",
            "max_jobs_per_source": None,
        },
        "steady_state_rescrape": {
            "source_scope": "all_frozen_sources",
            "source_count": len(ordered_recurring),
            "source_ids": ordered_recurring,
            "batch_size": batch_size,
            "batch_count": len(recurring_batches),
            "batches": recurring_batches,
            "cadence_hours": int(policy["recurring_policy"]["cadence_hours"]),
            "catalog_mode": "complete_catalog",
            "max_jobs_per_source": None,
        },
        "tier_controls": {
            "complete_catalog": dict(
                policy["lifecycle_policy"]["complete_catalog"]
            ),
            "partial_safe": dict(policy["lifecycle_policy"]["partial_safe"]),
        },
        "execution_limits": dict(policy["execution_limits"]),
        "controls": {
            "plan_only": True,
            "network_requests_performed": False,
            "mongodb_reads_performed": False,
            "mongodb_writes_performed": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "partial_safe_missing_reconciliation_enabled": False,
            "all_frozen_sources_in_recurring_schedule": True,
            "changed_only_downstream_processing": True,
        },
    }
    rollout["rollout_plan_sha256"] = _canonical_sha256(rollout)
    validate_phase7d4a_cohort(cohort)
    validate_phase7d4a_rollout(rollout, cohort=cohort)
    return cohort, rollout


def validate_phase7d4a_cohort(payload: Mapping[str, Any]) -> None:
    candidate = dict(payload)
    stored_hash = str(candidate.pop("cohort_sha256", "") or "")
    if not stored_hash or stored_hash != _canonical_sha256(candidate):
        raise ProductionCohort23Error(
            "Phase 7D4A cohort checksum is missing or invalid"
        )
    if (
        candidate.get("contract_version") != PHASE_7D4A_CONTRACT_VERSION
        or candidate.get("phase") != PHASE_7D4A
        or candidate.get("cohort_status") != PHASE_7D4A_COHORT_STATUS
    ):
        raise ProductionCohort23Error("Unsupported Phase 7D4A cohort descriptor")
    recurring = _source_ids(
        candidate.get("cohort_source_ids"),
        label="cohort source ids",
    )
    complete = _source_ids(
        candidate.get("complete_catalog_source_ids"),
        label="complete source ids",
    )
    partial = _source_ids(
        candidate.get("partial_safe_source_ids"),
        label="partial-safe source ids",
    )
    promoted = _source_ids(
        candidate.get("promoted_backfill_source_ids"),
        label="promoted backfill source ids",
    )
    deferred = _source_ids(
        candidate.get("deferred_source_ids"),
        label="deferred source ids",
    )
    if (
        len(recurring) != 23
        or len(complete) != 7
        or len(partial) != 16
        or len(promoted) != 7
        or len(deferred) != 79
    ):
        raise ProductionCohort23Error("Phase 7D4A cohort count invariant failed")
    if set(complete) & set(partial) or set(complete + partial) != set(recurring):
        raise ProductionCohort23Error("Phase 7D4A tier partition is inconsistent")
    if set(promoted) - set(recurring):
        raise ProductionCohort23Error(
            "Phase 7D4A promoted sources are outside the cohort"
        )
    if set(recurring) & set(deferred):
        raise ProductionCohort23Error("Phase 7D4A cohort and deferred sets overlap")
    raw_sources = candidate.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) != len(recurring):
        raise ProductionCohort23Error("Phase 7D4A source records are incomplete")
    if [str(source.get("source_id") or "") for source in raw_sources] != recurring:
        raise ProductionCohort23Error(
            "Phase 7D4A source records are not aligned with cohort order"
        )
    for source in raw_sources:
        lifecycle = source.get("lifecycle")
        if not isinstance(lifecycle, dict):
            raise ProductionCohort23Error("Source lifecycle controls are missing")
        if lifecycle.get("missing_reconciliation_enabled") is not False:
            raise ProductionCohort23Error(
                "Frozen sources cannot reconcile before closeout"
            )
        if lifecycle.get("deactivation_enabled") is not False:
            raise ProductionCohort23Error(
                "Frozen sources cannot deactivate before closeout"
            )
    safety = candidate.get("safety")
    if not isinstance(safety, dict):
        raise ProductionCohort23Error("Phase 7D4A safety controls are missing")
    for key in (
        "production_ingestion_enabled",
        "network_requests_performed",
        "mongodb_reads_performed",
        "mongodb_writes_performed",
        "lifecycle_reconciliation_enabled",
        "deactivation_enabled",
        "failed_or_partial_runs_may_increment_missing_count",
    ):
        if safety.get(key) is not False:
            raise ProductionCohort23Error(
                f"Unsafe Phase 7D4A cohort control: {key}"
            )


def validate_phase7d4a_rollout(
    payload: Mapping[str, Any],
    *,
    cohort: Mapping[str, Any],
) -> None:
    validate_phase7d4a_cohort(cohort)
    candidate = dict(payload)
    stored_hash = str(candidate.pop("rollout_plan_sha256", "") or "")
    if not stored_hash or stored_hash != _canonical_sha256(candidate):
        raise ProductionCohort23Error(
            "Phase 7D4A rollout checksum is missing or invalid"
        )
    if (
        candidate.get("contract_version") != PHASE_7D4A_CONTRACT_VERSION
        or candidate.get("phase") != PHASE_7D4A
        or candidate.get("status") != PHASE_7D4A_ROLLOUT_STATUS
    ):
        raise ProductionCohort23Error("Unsupported Phase 7D4A rollout descriptor")
    if candidate.get("cohort_sha256") != cohort.get("cohort_sha256"):
        raise ProductionCohort23Error(
            "Phase 7D4A rollout references a different cohort"
        )
    initial = candidate.get("initial_backfill")
    recurring = candidate.get("steady_state_rescrape")
    if not isinstance(initial, dict) or not isinstance(recurring, dict):
        raise ProductionCohort23Error("Phase 7D4A rollout sections are missing")
    if list(initial.get("source_ids") or []) != list(
        cohort["promoted_backfill_source_ids"]
    ):
        raise ProductionCohort23Error(
            "Initial backfill differs from the promoted source set"
        )
    if list(recurring.get("source_ids") or []) != list(
        cohort["cohort_source_ids"]
    ):
        raise ProductionCohort23Error(
            "Recurring schedule does not include all frozen sources"
        )
    if int(recurring.get("cadence_hours") or 0) != 72:
        raise ProductionCohort23Error("Recurring schedule must run every 72 hours")
    flattened_initial = [
        source_id
        for batch in initial.get("batches") or []
        for source_id in batch.get("source_ids") or []
    ]
    flattened_recurring = [
        source_id
        for batch in recurring.get("batches") or []
        for source_id in batch.get("source_ids") or []
    ]
    if flattened_initial != list(initial["source_ids"]):
        raise ProductionCohort23Error("Initial backfill batches are inconsistent")
    if flattened_recurring != list(recurring["source_ids"]):
        raise ProductionCohort23Error("Recurring batches are inconsistent")
    controls = candidate.get("controls")
    if not isinstance(controls, dict) or controls.get("plan_only") is not True:
        raise ProductionCohort23Error("Phase 7D4A rollout must remain plan-only")
    for key in (
        "network_requests_performed",
        "mongodb_reads_performed",
        "mongodb_writes_performed",
        "lifecycle_reconciliation_enabled",
        "deactivation_enabled",
        "partial_safe_missing_reconciliation_enabled",
    ):
        if controls.get(key) is not False:
            raise ProductionCohort23Error(
                f"Unsafe Phase 7D4A rollout control: {key}"
            )


def read_phase7d4a_cohort(path: Path) -> dict[str, Any]:
    payload = _load_json(path, label="Phase 7D4A production cohort")
    validate_phase7d4a_cohort(payload)
    return payload


def read_phase7d4a_rollout(
    path: Path,
    *,
    cohort: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _load_json(path, label="Phase 7D4A rollout plan")
    validate_phase7d4a_rollout(payload, cohort=cohort)
    return payload


def build_phase7d4a_from_paths(
    *,
    input_path: Path,
    target25_plan_path: Path,
    final_report_path: Path,
    policy_path: Path,
    generated_at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    input_path = Path(input_path).resolve()
    target25_plan_path = Path(target25_plan_path).resolve()
    final_report_path = Path(final_report_path).resolve()
    policy_path = Path(policy_path).resolve()
    inventory = read_portal_inventory(input_path)
    plan = read_target25_plan(target25_plan_path)
    report = read_phase7d3c1_final_report(final_report_path)
    policy = load_phase7d4a_policy(policy_path)
    return build_phase7d4a_artifacts(
        inventory=inventory,
        target25_plan=plan,
        final_report=report,
        policy=policy,
        evidence={
            "inventory_name": input_path.name,
            "inventory_sha256": _file_sha256(input_path),
            "target25_plan_file_sha256": _file_sha256(target25_plan_path),
            "final_report_file_sha256": _file_sha256(final_report_path),
            "policy_sha256": _file_sha256(policy_path),
        },
        generated_at=generated_at,
    )


def _immutable_text(path: Path, content: str) -> tuple[Path, bool]:
    target = Path(path).resolve()
    if target.exists():
        if target.read_text(encoding="utf-8-sig") != content:
            raise ProductionCohort23Error(
                f"Refusing to overwrite a different frozen artifact: {target}"
            )
        return target, False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, target)
    return target, True


def write_phase7d4a_artifacts(
    *,
    cohort_path: Path,
    rollout_path: Path,
    cohort: Mapping[str, Any],
    rollout: Mapping[str, Any],
) -> tuple[dict[str, str], bool]:
    validate_phase7d4a_cohort(cohort)
    validate_phase7d4a_rollout(rollout, cohort=cohort)
    cohort_target = Path(cohort_path).resolve()
    rollout_target = Path(rollout_path).resolve()
    files: dict[str, tuple[Path, str]] = {
        "cohort": (
            cohort_target,
            json.dumps(dict(cohort), indent=2, ensure_ascii=False) + "\n",
        ),
        "rollout": (
            rollout_target,
            json.dumps(dict(rollout), indent=2, ensure_ascii=False) + "\n",
        ),
        "cohort_source_ids": (
            cohort_target.parent / "phase7d4_production_source_ids.txt",
            "\n".join(cohort["cohort_source_ids"]) + "\n",
        ),
        "complete_source_ids": (
            cohort_target.parent / "phase7d4_complete_source_ids.txt",
            "\n".join(cohort["complete_catalog_source_ids"]) + "\n",
        ),
        "partial_safe_source_ids": (
            cohort_target.parent / "phase7d4_partial_safe_source_ids.txt",
            "\n".join(cohort["partial_safe_source_ids"]) + "\n",
        ),
        "promoted_backfill_source_ids": (
            cohort_target.parent / "phase7d4_promoted_backfill_source_ids.txt",
            "\n".join(cohort["promoted_backfill_source_ids"]) + "\n",
        ),
        "deferred_source_ids": (
            cohort_target.parent / "phase7d4_deferred_source_ids.txt",
            "\n".join(cohort["deferred_source_ids"]) + "\n",
        ),
    }
    existing = [path.exists() for path, _ in files.values()]
    if any(existing) and not all(existing):
        raise ProductionCohort23Error(
            "Phase 7D4A artifact set is only partially present; "
            "refusing to complete or overwrite it"
        )
    artifacts: dict[str, str] = {}
    created_values: list[bool] = []
    for key, (path, content) in files.items():
        written, created = _immutable_text(path, content)
        artifacts[key] = str(written)
        created_values.append(created)
    return artifacts, all(created_values)


def validate_phase7d4a_from_paths(
    *,
    input_path: Path,
    target25_plan_path: Path,
    final_report_path: Path,
    policy_path: Path,
    cohort_path: Path,
    rollout_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    stored_cohort = read_phase7d4a_cohort(cohort_path)
    stored_rollout = read_phase7d4a_rollout(
        rollout_path,
        cohort=stored_cohort,
    )
    frozen_at = datetime.fromisoformat(str(stored_cohort.get("frozen_at") or ""))
    rebuilt_cohort, rebuilt_rollout = build_phase7d4a_from_paths(
        input_path=input_path,
        target25_plan_path=target25_plan_path,
        final_report_path=final_report_path,
        policy_path=policy_path,
        generated_at=frozen_at,
    )
    if stored_cohort != rebuilt_cohort or stored_rollout != rebuilt_rollout:
        raise ProductionCohort23Error(
            "Frozen Phase 7D4A artifacts no longer match their signed evidence"
        )
    return stored_cohort, stored_rollout

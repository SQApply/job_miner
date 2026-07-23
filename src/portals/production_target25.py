from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .certification import PortalInventoryEntry
from .production_guarded_execution import read_phase7d3b_report
from .production_ingestion import Phase6AIngestionPlan


PHASE_7D3C_CONTRACT_VERSION = "1.0"
PHASE_7D3C = "7D3C"


class ProductionTarget25Error(RuntimeError):
    """Raised when target-25 evidence or safety invariants are inconsistent."""


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
        raise ProductionTarget25Error(f"{label} contains invalid JSON: {source}") from exc
    if not isinstance(payload, dict):
        raise ProductionTarget25Error(f"{label} must contain a JSON object")
    return payload


def _source_ids(values: Any, *, label: str) -> list[str]:
    if not isinstance(values, list):
        raise ProductionTarget25Error(f"{label} must be a JSON array")
    normalized = [str(value or "").strip() for value in values]
    if any(not value for value in normalized):
        raise ProductionTarget25Error(f"{label} contains an empty source id")
    if len(normalized) != len(set(normalized)):
        raise ProductionTarget25Error(f"{label} contains duplicate source ids")
    return normalized


def load_target25_policy(path: Path) -> dict[str, Any]:
    policy = _load_json(path, label="Phase 7D3C target-25 policy")
    descriptor = (
        str(policy.get("contract_version") or ""),
        str(policy.get("phase") or ""),
        str(policy.get("policy_status") or ""),
    )
    if descriptor != (
        PHASE_7D3C_CONTRACT_VERSION,
        PHASE_7D3C,
        "target_25_promotion_policy",
    ):
        raise ProductionTarget25Error("Unsupported Phase 7D3C target-25 policy")
    candidates = policy.get("promotion_candidates")
    if not isinstance(candidates, list) or len(candidates) != 9:
        raise ProductionTarget25Error(
            "Phase 7D3C requires exactly nine promotion candidates"
        )
    candidate_ids = _source_ids(
        [candidate.get("source_id") for candidate in candidates],
        label="promotion candidate source ids",
    )
    if len(candidate_ids) != 9:
        raise ProductionTarget25Error("Promotion candidate count must be nine")
    expected_counts = {
        "expected_inventory_count": 102,
        "expected_phase7d3b_source_count": 26,
        "expected_complete_catalog_count": 5,
        "expected_partial_safe_count": 11,
        "expected_nonproductive_count": 10,
        "target_production_source_count": 25,
    }
    for key, expected in expected_counts.items():
        if int(policy.get(key) or -1) != expected:
            raise ProductionTarget25Error(
                f"Unsafe or inconsistent target-25 policy value: {key}"
            )
    recurring = policy.get("recurring_policy")
    limits = policy.get("execution_limits")
    if not isinstance(recurring, dict) or int(recurring.get("cadence_hours") or 0) != 72:
        raise ProductionTarget25Error("Target-25 recurring cadence must be 72 hours")
    if not isinstance(limits, dict):
        raise ProductionTarget25Error("Target-25 execution limits are missing")
    required_limits = {
        "source_concurrency": 1,
        "detail_concurrency": 1,
        "gpu_llm_concurrency": 1,
        "requests_per_minute": 30,
        "source_timeout_seconds": 1800,
        "acquisition_timeout_seconds": 25,
        "page_safety_cap": 500,
    }
    for key, expected in required_limits.items():
        if limits.get(key) != expected:
            raise ProductionTarget25Error(
                f"Unsafe or inconsistent target-25 execution limit: {key}"
            )
    partial = recurring.get("partial_safe_sources")
    complete = recurring.get("complete_catalog_sources")
    if not isinstance(partial, dict) or not isinstance(complete, dict):
        raise ProductionTarget25Error("Target-25 tier lifecycle policies are missing")
    if partial.get("missing_from_listing_reconciliation_enabled") is not False:
        raise ProductionTarget25Error(
            "Partial sources cannot reconcile missing jobs from incomplete discovery"
        )
    if partial.get("deactivation_from_incomplete_discovery_enabled") is not False:
        raise ProductionTarget25Error(
            "Partial sources cannot deactivate jobs from incomplete discovery"
        )
    if complete.get("missing_from_listing_reconciliation_enabled") is not False:
        raise ProductionTarget25Error(
            "Reconciliation remains disabled until two clean complete cycles"
        )
    return policy


def classify_phase7d3b_source_results(
    source_results: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    tiers: dict[str, list[dict[str, Any]]] = {
        "complete_catalog": [],
        "partial_safe": [],
        "nonproductive": [],
    }
    seen: set[str] = set()
    for raw in source_results:
        source_id = str(raw.get("source_id") or "").strip()
        if not source_id:
            raise ProductionTarget25Error("Phase 7D3B source result is missing source_id")
        if source_id in seen:
            raise ProductionTarget25Error(
                f"Duplicate Phase 7D3B source result: {source_id}"
            )
        seen.add(source_id)
        accepted = int(raw.get("accepted_count") or 0)
        catalog_complete = raw.get("catalog_complete") is True
        status = str(raw.get("status") or "")
        normalized = {
            "source_id": source_id,
            "display_name": str(raw.get("display_name") or source_id),
            "status": status,
            "certification_status": raw.get("certification_status"),
            "discovered_count": int(raw.get("discovered_count") or 0),
            "attempted_count": int(raw.get("attempted_count") or 0),
            "extracted_count": int(raw.get("extracted_count") or 0),
            "accepted_count": accepted,
            "quarantined_count": int(raw.get("quarantined_count") or 0),
            "catalog_complete": catalog_complete,
            "error_type": raw.get("error_type"),
            "error_message": raw.get("error_message"),
        }
        if catalog_complete and status == "success" and accepted > 0:
            tiers["complete_catalog"].append(normalized)
        elif accepted > 0:
            tiers["partial_safe"].append(normalized)
        else:
            tiers["nonproductive"].append(normalized)
    return tiers


def load_phase7d3b_tiers(
    checkpoint_dir: Path,
    *,
    expected_source_ids: list[str],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    root = Path(checkpoint_dir).resolve()
    reports: list[dict[str, Any]] = []
    source_results: list[dict[str, Any]] = []
    rollout_identity: tuple[str, str, str, str] | None = None
    for ordinal in range(1, 8):
        path = root / f"phase7d3b_batch_{ordinal:02d}_checkpoint.json"
        report = read_phase7d3b_report(path)
        if int(report.get("batch_ordinal") or 0) != ordinal:
            raise ProductionTarget25Error(
                f"Phase 7D3B checkpoint ordinal mismatch: {path}"
            )
        identity = (
            str(report.get("rollout_id") or ""),
            str(report.get("rollout_plan_sha256") or ""),
            str(report.get("production_plan_id") or ""),
            str(report.get("cohort_sha256") or ""),
        )
        if rollout_identity is None:
            rollout_identity = identity
        elif identity != rollout_identity:
            raise ProductionTarget25Error(
                "Phase 7D3B checkpoints belong to different rollouts"
            )
        report_results = report.get("source_results")
        if not isinstance(report_results, list):
            raise ProductionTarget25Error(
                f"Phase 7D3B checkpoint has no source_results: {path}"
            )
        if [str(item.get("source_id") or "") for item in report_results] != list(
            report.get("source_ids") or []
        ):
            raise ProductionTarget25Error(
                f"Phase 7D3B checkpoint source result order is inconsistent: {path}"
            )
        reports.append(report)
        source_results.extend(report_results)

    actual_source_ids = [str(result.get("source_id") or "") for result in source_results]
    if actual_source_ids != expected_source_ids:
        raise ProductionTarget25Error(
            "Phase 7D3B checkpoints do not exactly match the Phase 7D1 production plan"
        )
    tiers = classify_phase7d3b_source_results(source_results)
    if (
        len(tiers["complete_catalog"]) != 5
        or len(tiers["partial_safe"]) != 11
        or len(tiers["nonproductive"]) != 10
    ):
        raise ProductionTarget25Error(
            "Expected Phase 7D3B evidence to classify as 5 complete, "
            f"11 partial-safe, and 10 nonproductive; found "
            f"{len(tiers['complete_catalog'])}/"
            f"{len(tiers['partial_safe'])}/"
            f"{len(tiers['nonproductive'])}"
        )
    return tiers, {
        "checkpoint_count": len(reports),
        "rollout_id": rollout_identity[0] if rollout_identity else None,
        "rollout_plan_sha256": rollout_identity[1] if rollout_identity else None,
        "production_plan_id": rollout_identity[2] if rollout_identity else None,
        "cohort_sha256": rollout_identity[3] if rollout_identity else None,
        "accepted_jobs": sum(
            int(result.get("accepted_count") or 0) for result in source_results
        ),
    }


def _inventory_payload(
    entry: PortalInventoryEntry,
    *,
    tier: str,
    evidence: Mapping[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_id": entry.source_id,
        "display_name": entry.display_name,
        "listing_url": entry.listing_url,
        "source_row": entry.source_row,
        "tier": tier,
    }
    if evidence is not None:
        payload["phase7d3b_evidence"] = dict(evidence)
    if reason:
        payload["promotion_evidence"] = reason
    return payload


def build_target25_plan(
    *,
    inventory: list[PortalInventoryEntry],
    production_plan: Phase6AIngestionPlan,
    tiers: Mapping[str, list[dict[str, Any]]],
    checkpoint_evidence: Mapping[str, Any],
    policy: Mapping[str, Any],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    if len(inventory) != int(policy["expected_inventory_count"]):
        raise ProductionTarget25Error(
            f"Expected {policy['expected_inventory_count']} inventory sources, "
            f"found {len(inventory)}"
        )
    inventory_by_id = {entry.source_id: entry for entry in inventory}
    if len(inventory_by_id) != len(inventory):
        raise ProductionTarget25Error("Portal inventory contains duplicate source ids")

    complete_evidence = list(tiers.get("complete_catalog") or [])
    partial_evidence = list(tiers.get("partial_safe") or [])
    nonproductive_evidence = list(tiers.get("nonproductive") or [])
    current_ids = [
        item["source_id"]
        for group in (complete_evidence, partial_evidence, nonproductive_evidence)
        for item in group
    ]
    if set(current_ids) != set(production_plan.selected_source_ids):
        raise ProductionTarget25Error(
            "Classified Phase 7D3B sources differ from the production plan"
        )

    candidates = list(policy.get("promotion_candidates") or [])
    candidate_ids = [str(candidate.get("source_id") or "") for candidate in candidates]
    if set(candidate_ids) & set(current_ids):
        raise ProductionTarget25Error(
            "Promotion candidates overlap the already tested Phase 7D3B cohort"
        )
    missing_inventory = [
        source_id
        for source_id in current_ids + candidate_ids
        if source_id not in inventory_by_id
    ]
    if missing_inventory:
        raise ProductionTarget25Error(
            "Target-25 sources are absent from the portal inventory: "
            + ", ".join(missing_inventory)
        )

    complete_sources = [
        _inventory_payload(
            inventory_by_id[item["source_id"]],
            tier="complete_catalog",
            evidence=item,
        )
        for item in complete_evidence
    ]
    partial_sources = [
        _inventory_payload(
            inventory_by_id[item["source_id"]],
            tier="partial_safe",
            evidence=item,
        )
        for item in partial_evidence
    ]
    nonproductive_sources = [
        _inventory_payload(
            inventory_by_id[item["source_id"]],
            tier="deferred_nonproductive",
            evidence=item,
        )
        for item in nonproductive_evidence
    ]
    promotion_sources = [
        _inventory_payload(
            inventory_by_id[source_id],
            tier="promotion_candidate",
            reason=str(candidate.get("evidence") or ""),
        )
        for candidate, source_id in zip(candidates, candidate_ids)
    ]

    recurring_source_ids = [
        source["source_id"] for source in complete_sources + partial_sources
    ]
    target_source_ids = recurring_source_ids + candidate_ids
    if len(recurring_source_ids) != 16 or len(target_source_ids) != 25:
        raise ProductionTarget25Error(
            "Target-25 plan must contain 16 currently productive sources "
            "and nine promotion candidates"
        )
    now = generated_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ProductionTarget25Error("generated_at must be timezone-aware")

    plan_seed = {
        "phase7d3b_rollout_id": checkpoint_evidence.get("rollout_id"),
        "production_plan_id": production_plan.plan_id,
        "recurring_source_ids": recurring_source_ids,
        "promotion_candidate_source_ids": candidate_ids,
        "target_source_ids": target_source_ids,
    }
    payload: dict[str, Any] = {
        "contract_version": PHASE_7D3C_CONTRACT_VERSION,
        "phase": PHASE_7D3C,
        "status": "target_25_promotion_plan_ready",
        "plan_id": "phase7d3c_" + _canonical_sha256(plan_seed)[:20],
        "generated_at": now.isoformat(),
        "inventory_source_count": len(inventory),
        "current_counts": {
            "complete_catalog": len(complete_sources),
            "partial_safe": len(partial_sources),
            "recurring_usable": len(recurring_source_ids),
            "nonproductive": len(nonproductive_sources),
            "promotion_candidates": len(promotion_sources),
            "remaining_to_target": 9,
        },
        "target_production_source_count": 25,
        "target_achieved": False,
        "complete_catalog_sources": complete_sources,
        "partial_safe_sources": partial_sources,
        "phase7d3b_nonproductive_sources": nonproductive_sources,
        "promotion_candidates": promotion_sources,
        "current_recurring_source_ids": recurring_source_ids,
        "promotion_candidate_source_ids": candidate_ids,
        "target_source_ids": target_source_ids,
        "recurring_policy": dict(policy["recurring_policy"]),
        "promotion_test_policy": {
            "catalog_mode": "complete_catalog",
            "max_jobs_per_source": None,
            "target_only": True,
            "mongodb_reads_performed": False,
            "mongodb_writes_performed": False,
            "normalized_job_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "anti_bot_bypass_enabled": False,
        },
        "execution_limits": dict(policy["execution_limits"]),
        "evidence": {
            **dict(checkpoint_evidence),
            "production_plan_id": production_plan.plan_id,
            "production_plan_cohort_sha256": production_plan.cohort_sha256,
            "policy_sha256": _canonical_sha256(policy),
        },
        "controls": {
            "plan_only": True,
            "network_requests_performed": False,
            "mongodb_reads_performed": False,
            "mongodb_writes_performed": False,
            "candidate_facing_jobs_may_use_partial_safe_sources": True,
            "incomplete_cycles_may_reconcile_missing_jobs": False,
            "incomplete_cycles_may_deactivate_jobs": False,
            "changed_only_downstream_processing": True,
            "gpu_llm_concurrency": 1,
        },
    }
    payload["plan_sha256"] = _canonical_sha256(payload)
    validate_target25_plan(payload)
    return payload


def validate_target25_plan(payload: Mapping[str, Any]) -> None:
    candidate = dict(payload)
    stored_hash = str(candidate.pop("plan_sha256", "") or "")
    if not stored_hash or stored_hash != _canonical_sha256(candidate):
        raise ProductionTarget25Error("Target-25 plan checksum is missing or invalid")
    if (
        candidate.get("contract_version") != PHASE_7D3C_CONTRACT_VERSION
        or candidate.get("phase") != PHASE_7D3C
        or candidate.get("status") != "target_25_promotion_plan_ready"
    ):
        raise ProductionTarget25Error("Unsupported target-25 plan descriptor")
    current = _source_ids(
        candidate.get("current_recurring_source_ids"),
        label="current recurring source ids",
    )
    promotion = _source_ids(
        candidate.get("promotion_candidate_source_ids"),
        label="promotion candidate source ids",
    )
    target = _source_ids(candidate.get("target_source_ids"), label="target source ids")
    if len(current) != 16 or len(promotion) != 9 or len(target) != 25:
        raise ProductionTarget25Error("Target-25 plan count invariant failed")
    if set(current) & set(promotion) or target != current + promotion:
        raise ProductionTarget25Error("Target-25 plan partition invariant failed")
    controls = candidate.get("controls")
    test_policy = candidate.get("promotion_test_policy")
    if not isinstance(controls, dict) or not isinstance(test_policy, dict):
        raise ProductionTarget25Error("Target-25 safety controls are missing")
    for key in (
        "mongodb_reads_performed",
        "mongodb_writes_performed",
        "normalized_job_writes_enabled",
        "lifecycle_reconciliation_enabled",
        "deactivation_enabled",
        "anti_bot_bypass_enabled",
    ):
        if test_policy.get(key) is not False:
            raise ProductionTarget25Error(f"Unsafe promotion test control: {key}")
    if controls.get("incomplete_cycles_may_reconcile_missing_jobs") is not False:
        raise ProductionTarget25Error("Incomplete cycles cannot reconcile missing jobs")
    if controls.get("incomplete_cycles_may_deactivate_jobs") is not False:
        raise ProductionTarget25Error("Incomplete cycles cannot deactivate jobs")


def read_target25_plan(path: Path) -> dict[str, Any]:
    payload = _load_json(path, label="Phase 7D3C target-25 plan")
    validate_target25_plan(payload)
    return payload


def write_target25_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    checksum_field: str,
) -> Path:
    target = Path(path).resolve()
    candidate = dict(payload)
    stored_hash = str(candidate.get(checksum_field) or "")
    unsigned = dict(candidate)
    unsigned.pop(checksum_field, None)
    if not stored_hash or stored_hash != _canonical_sha256(unsigned):
        raise ProductionTarget25Error(
            f"Candidate {target.name} checksum is missing or invalid"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(candidate, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, target)
    return target


def evaluate_target25_promotion(
    *,
    plan: Mapping[str, Any],
    certification_summary: Mapping[str, Any],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    validate_target25_plan(plan)
    candidate_ids = list(plan["promotion_candidate_source_ids"])
    records = certification_summary.get("records")
    if not isinstance(records, list):
        raise ProductionTarget25Error("Promotion summary records are missing")
    by_id = {
        str(record.get("source_id") or ""): record
        for record in records
        if str(record.get("source_id") or "") in set(candidate_ids)
    }
    complete: list[str] = []
    partial_safe: list[str] = []
    failed: list[str] = []
    results: list[dict[str, Any]] = []
    for source_id in candidate_ids:
        record = by_id.get(source_id)
        if record is None:
            classification = "not_run"
            failed.append(source_id)
            results.append(
                {
                    "source_id": source_id,
                    "classification": classification,
                    "status": None,
                    "extracted_jobs": 0,
                    "catalog_complete": False,
                    "error_type": "missing_result",
                }
            )
            continue
        status = str(record.get("status") or "")
        extracted = int(record.get("extracted_jobs") or 0)
        catalog_complete = record.get("catalog_complete") is True
        if status == "success" and catalog_complete and extracted > 0:
            classification = "complete_catalog"
            complete.append(source_id)
        elif status == "partial" and extracted > 0:
            classification = "partial_safe"
            partial_safe.append(source_id)
        else:
            classification = "not_productive"
            failed.append(source_id)
        results.append(
            {
                "source_id": source_id,
                "classification": classification,
                "status": status,
                "certification_status": record.get("certification_status"),
                "discovered_urls": int(record.get("discovered_urls") or 0),
                "attempted_urls": int(record.get("attempted_urls") or 0),
                "extracted_jobs": extracted,
                "catalog_complete": catalog_complete,
                "error_type": record.get("error_type"),
                "error_message": record.get("error_message"),
            }
        )
    promoted = complete + partial_safe
    final_usable = len(plan["current_recurring_source_ids"]) + len(promoted)
    now = generated_at or datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "contract_version": PHASE_7D3C_CONTRACT_VERSION,
        "phase": PHASE_7D3C,
        "status": (
            "target_25_reached" if final_usable >= 25 else "target_25_not_reached"
        ),
        "generated_at": now.isoformat(),
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["plan_sha256"],
        "counts": {
            "previously_usable": len(plan["current_recurring_source_ids"]),
            "candidate_count": len(candidate_ids),
            "candidate_complete_catalog": len(complete),
            "candidate_partial_safe": len(partial_safe),
            "candidate_not_productive_or_not_run": len(failed),
            "promoted": len(promoted),
            "final_recurring_usable": final_usable,
            "target": 25,
            "shortfall": max(0, 25 - final_usable),
        },
        "target_achieved": final_usable >= 25,
        "complete_candidate_source_ids": complete,
        "partial_safe_candidate_source_ids": partial_safe,
        "failed_candidate_source_ids": failed,
        "final_recurring_source_ids": list(plan["current_recurring_source_ids"])
        + promoted,
        "results": results,
        "controls": {
            "network_results_evaluated": True,
            "mongodb_reads_performed": False,
            "mongodb_writes_performed": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "partial_safe_missing_reconciliation_enabled": False,
        },
    }
    payload["report_sha256"] = _canonical_sha256(payload)
    return payload

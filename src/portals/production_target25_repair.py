from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .production_target25 import (
    evaluate_target25_promotion,
    validate_target25_plan,
)


PHASE_7D3C1_CONTRACT_VERSION = "1.0"
PHASE_7D3C1 = "7D3C1"
EXPECTED_PREVIOUS_USABLE = 21
EXPECTED_REPAIR_SOURCE_COUNT = 4
FINAL_REPAIR_DETAIL_BUDGET = 10


class ProductionTarget25RepairError(RuntimeError):
    """Raised when final target-25 repair evidence is incomplete or inconsistent."""


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Required {label} does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ProductionTarget25RepairError(
            f"{label} contains invalid JSON: {source}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProductionTarget25RepairError(f"{label} must contain a JSON object")
    return payload


def read_prior_promotion_report(path: Path) -> dict[str, Any]:
    report = _read_json(path, label="Phase 7D3C promotion report")
    unsigned = dict(report)
    stored_hash = str(unsigned.pop("report_sha256", "") or "")
    if not stored_hash or stored_hash != _canonical_sha256(unsigned):
        raise ProductionTarget25RepairError(
            "Phase 7D3C promotion report checksum is missing or invalid"
        )
    if report.get("phase") != "7D3C":
        raise ProductionTarget25RepairError("Unsupported prior promotion report phase")
    return report


def final_repair_source_ids(
    *,
    plan: Mapping[str, Any],
    prior_report: Mapping[str, Any],
) -> list[str]:
    validate_target25_plan(plan)
    if prior_report.get("plan_id") != plan.get("plan_id"):
        raise ProductionTarget25RepairError(
            "Prior promotion report belongs to a different target-25 plan"
        )
    if prior_report.get("plan_sha256") != plan.get("plan_sha256"):
        raise ProductionTarget25RepairError(
            "Prior promotion report plan checksum does not match"
        )
    counts = prior_report.get("counts")
    if not isinstance(counts, dict):
        raise ProductionTarget25RepairError("Prior promotion counts are missing")
    if (
        prior_report.get("target_achieved") is not False
        or int(counts.get("final_recurring_usable") or 0)
        != EXPECTED_PREVIOUS_USABLE
        or int(counts.get("shortfall") or 0) != EXPECTED_REPAIR_SOURCE_COUNT
    ):
        raise ProductionTarget25RepairError(
            "Final repair requires the verified 21-of-25 promotion baseline"
        )
    values = prior_report.get("failed_candidate_source_ids")
    if not isinstance(values, list):
        raise ProductionTarget25RepairError("Prior failed candidate ids are missing")
    source_ids = [str(value or "").strip() for value in values]
    if (
        len(source_ids) != EXPECTED_REPAIR_SOURCE_COUNT
        or any(not value for value in source_ids)
        or len(source_ids) != len(set(source_ids))
    ):
        raise ProductionTarget25RepairError(
            "Final repair must contain exactly four unique failed candidates"
        )
    candidate_ids = list(plan["promotion_candidate_source_ids"])
    if any(source_id not in candidate_ids for source_id in source_ids):
        raise ProductionTarget25RepairError(
            "Final repair contains a source outside the target-25 candidate set"
        )
    return source_ids


def merge_repair_certification_records(
    *,
    plan: Mapping[str, Any],
    baseline_summary: Mapping[str, Any],
    repair_summary: Mapping[str, Any],
    repair_source_ids: Sequence[str],
) -> dict[str, Any]:
    """Replace only the four failed baseline records with their repair results."""

    validate_target25_plan(plan)
    candidate_ids = list(plan["promotion_candidate_source_ids"])
    expected_repair_ids = [str(value) for value in repair_source_ids]
    baseline_records = baseline_summary.get("records")
    repair_records = repair_summary.get("records")
    if not isinstance(baseline_records, list) or not isinstance(repair_records, list):
        raise ProductionTarget25RepairError(
            "Baseline and repair summaries must both contain records"
        )

    baseline_by_id = {
        str(record.get("source_id") or ""): record
        for record in baseline_records
        if isinstance(record, dict)
        and str(record.get("source_id") or "") in set(candidate_ids)
    }
    repair_by_id = {
        str(record.get("source_id") or ""): record
        for record in repair_records
        if isinstance(record, dict)
    }
    if set(baseline_by_id) != set(candidate_ids):
        raise ProductionTarget25RepairError(
            "Baseline promotion summary does not contain exactly the nine candidates"
        )
    if set(repair_by_id) != set(expected_repair_ids):
        raise ProductionTarget25RepairError(
            "Repair summary does not contain exactly the four selected repair sources"
        )

    merged_records = [
        dict(repair_by_id.get(source_id) or baseline_by_id[source_id])
        for source_id in candidate_ids
    ]
    return {
        "records": merged_records,
        "merge": {
            "strategy": "replace_failed_candidates_only",
            "baseline_candidate_count": len(baseline_by_id),
            "repair_source_ids": expected_repair_ids,
            "repair_record_count": len(repair_by_id),
        },
    }


def build_final_repair_report(
    *,
    plan: Mapping[str, Any],
    prior_report: Mapping[str, Any],
    baseline_summary: Mapping[str, Any],
    repair_summary: Mapping[str, Any],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    repair_source_ids = final_repair_source_ids(
        plan=plan,
        prior_report=prior_report,
    )
    merged = merge_repair_certification_records(
        plan=plan,
        baseline_summary=baseline_summary,
        repair_summary=repair_summary,
        repair_source_ids=repair_source_ids,
    )
    evaluated = evaluate_target25_promotion(
        plan=plan,
        certification_summary=merged,
        generated_at=generated_at,
    )
    evaluated.pop("report_sha256", None)
    repair_records = {
        str(record.get("source_id") or ""): record
        for record in repair_summary.get("records") or []
        if isinstance(record, dict)
    }
    productive = [
        source_id
        for source_id in repair_source_ids
        if int((repair_records.get(source_id) or {}).get("extracted_jobs") or 0) > 0
        and str((repair_records.get(source_id) or {}).get("status") or "")
        in {"success", "partial"}
    ]
    now = generated_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ProductionTarget25RepairError("generated_at must be timezone-aware")
    evaluated.update(
        {
            "contract_version": PHASE_7D3C1_CONTRACT_VERSION,
            "phase": PHASE_7D3C1,
            "generated_at": now.isoformat(),
            "repair": {
                "strategy": "four_source_generic_repair",
                "source_ids": repair_source_ids,
                "productive_source_ids": productive,
                "still_failed_source_ids": [
                    source_id
                    for source_id in repair_source_ids
                    if source_id not in productive
                ],
                "detail_budget_per_source": FINAL_REPAIR_DETAIL_BUDGET,
                "baseline_recurring_usable": EXPECTED_PREVIOUS_USABLE,
                "network_results_evaluated": True,
            },
        }
    )
    controls = dict(evaluated.get("controls") or {})
    controls.update(
        {
            "mongodb_reads_performed": False,
            "mongodb_writes_performed": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "anti_bot_bypass_enabled": False,
            "partial_safe_missing_reconciliation_enabled": False,
        }
    )
    evaluated["controls"] = controls
    evaluated["report_sha256"] = _canonical_sha256(evaluated)
    return evaluated

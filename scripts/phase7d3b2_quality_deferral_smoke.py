from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_guarded_execution import (
    Phase7D3BBatchReportBody,
    Phase7D3BSourceSnapshot,
    _canonical_sha256,
    can_defer_phase7d3b_quality_shortfall,
    reclassify_phase7d3b_deferred_quality_checkpoint,
)


def _source(source_id: str, *, quarantined: int = 0, rejected: int = 0) -> dict:
    return {
        "source_id": source_id,
        "status": "failed",
        "error_type": "catalog_incomplete",
        "catalog_complete": False,
        "discovery_complete": False,
        "accepted_count": 92 if quarantined else 0,
        "quarantined_count": quarantined,
        "rejected_count": rejected,
        "inserted_count": 92 if quarantined else 0,
        "updated_count": 0,
        "unchanged_count": 0,
        "reactivated_count": 0,
    }


def main() -> None:
    source_ids = [f"cert_source_{index}" for index in range(1, 5)]
    rwr = _source(source_ids[-1], quarantined=2)
    source_results = [_source(source_id) for source_id in source_ids[:-1]] + [rwr]
    snapshot = Phase7D3BSourceSnapshot(
        source_ids=source_ids,
        current_by_source={source_id: 0 for source_id in source_ids},
        active_by_source={source_id: 0 for source_id in source_ids},
        current_total=0,
        active_total=0,
    )
    blocker = f"source_quality_shortfall:{source_ids[-1]}"
    body = Phase7D3BBatchReportBody(
        status="failed",
        ready_for_next_batch_or_closeout=False,
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        rollout_id="phase7d3b-smoke",
        rollout_plan_sha256="a" * 64,
        production_plan_id="phase7d3b-smoke-plan",
        cohort_sha256="b" * 64,
        batch_id="batch-03",
        batch_ordinal=3,
        batch_count=7,
        run_id="phase7d3b-smoke-run",
        source_count=4,
        source_ids=source_ids,
        successful_source_count=0,
        complete_catalog_source_count=0,
        productive_source_count=1,
        deferred_source_count=4,
        deferred_source_ids=source_ids,
        deferred_reasons={source_id: "catalog_incomplete" for source_id in source_ids},
        counters={"quarantined": 2},
        before=snapshot,
        after=snapshot,
        source_results=source_results,
        blockers=[blocker],
        controls={
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
        },
    )
    payload = body.model_dump(mode="json")
    payload["report_sha256"] = _canonical_sha256(payload)
    revised = reclassify_phase7d3b_deferred_quality_checkpoint(payload)

    rejected = dict(rwr)
    rejected["rejected_count"] = 1
    if not can_defer_phase7d3b_quality_shortfall(rwr):
        raise SystemExit("PHASE_7D3B2_SMOKE_FAILED safe quarantine was rejected")
    if can_defer_phase7d3b_quality_shortfall(rejected):
        raise SystemExit("PHASE_7D3B2_SMOKE_FAILED rejected persistence error was deferred")
    if revised["status"] != "passed_with_deferred" or revised["blockers"]:
        raise SystemExit("PHASE_7D3B2_SMOKE_FAILED checkpoint did not progress")
    if revised["controls"]["lifecycle_reconciliation_enabled"] is not False:
        raise SystemExit("PHASE_7D3B2_SMOKE_FAILED reconciliation was enabled")
    if revised["controls"]["deactivation_enabled"] is not False:
        raise SystemExit("PHASE_7D3B2_SMOKE_FAILED deactivation was enabled")

    print(
        "PHASE_7D3B2_QUALITY_DEFERRAL_SMOKE_OK",
        json.dumps(
            {
                "deferred_sources": revised["deferred_source_count"],
                "mongodb_writes": False,
                "next_batch_allowed": revised["ready_for_next_batch_or_closeout"],
                "quarantined_jobs": revised["counters"]["quarantined"],
                "reconciliation": False,
                "rejected_records_deferrable": False,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

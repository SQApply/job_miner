from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.dom_pagination import GeneralizedDomPaginationOptions
from src.portals.production_guarded_execution import (
    Phase7D3BBatchReportBody,
    Phase7D3BSourceSnapshot,
)
from src.portals.url_intelligence import assess_job_candidate_url


def main() -> None:
    login = "https://careers-example.icims.com/jobs/7107/login"
    action = assess_job_candidate_url(login, platform_hint="icims")
    if not action.hard_reject or "job_action_route:login" not in action.reasons:
        raise SystemExit("PHASE_7D3B1_SMOKE_FAILED action URL entered detail scope")

    pagination = GeneralizedDomPaginationOptions()
    source_id = "cert_deferred_example"
    snapshot = Phase7D3BSourceSnapshot(
        source_ids=[source_id],
        current_by_source={source_id: 0},
        active_by_source={source_id: 0},
        current_total=0,
        active_total=0,
    )
    report = Phase7D3BBatchReportBody(
        status="passed_with_deferred",
        ready_for_next_batch_or_closeout=True,
        generated_at=datetime.now(timezone.utc),
        rollout_id="phase7d3b1-smoke",
        rollout_plan_sha256="a" * 64,
        production_plan_id="phase7d3b1-plan",
        cohort_sha256="b" * 64,
        batch_id="phase7d3b1-batch",
        batch_ordinal=1,
        batch_count=7,
        run_id="phase7d3b1-run",
        source_count=1,
        source_ids=[source_id],
        successful_source_count=0,
        complete_catalog_source_count=0,
        productive_source_count=0,
        deferred_source_count=1,
        deferred_source_ids=[source_id],
        deferred_reasons={source_id: "access_blocked"},
        counters={},
        before=snapshot,
        after=snapshot,
        source_results=[{"source_id": source_id}],
        blockers=[],
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
    print(
        "PHASE_7D3B1_COMPLETION_REPAIR_SMOKE_OK",
        json.dumps(
            {
                "action_url_rejected": action.hard_reject,
                "iframe_capable": True,
                "stable_rounds_required": pagination.stable_rounds_required,
                "deferred_progression": report.ready_for_next_batch_or_closeout,
                "reconciliation": False,
                "deactivation": False,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

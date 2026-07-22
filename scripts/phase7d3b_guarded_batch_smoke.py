from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_guarded_execution import (
    PHASE_7D3B_WRITE_CONFIRMATION,
    Phase7D3BSourceSnapshot,
    build_phase7d3b_batch_report,
    load_phase7d3b_authorization,
    read_phase7d3b_report,
    require_phase7d3b_write_confirmation,
    write_phase7d3b_report,
)
from src.portals.production_guarded_rollout import (
    build_phase7d3a_rollout_plan,
    write_phase7d3a_rollout_plan,
)
from src.portals.production_ingestion import Phase6AIngestionPlan, Phase6ASource
from src.portals.production_pilot import Phase7D2PilotSelection


class _SourceResult:
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        self.display_name = source_id
        self.status = "success"
        self.certification_status = "passed"
        self.discovered_count = 1
        self.attempted_count = 1
        self.extracted_count = 1
        self.accepted_count = 1
        self.quarantined_count = 0
        self.rejected_count = 0
        self.inserted_count = 1
        self.updated_count = 0
        self.unchanged_count = 0
        self.reactivated_count = 0
        self.catalog_mode = "complete_catalog"
        self.discovery_complete = True
        self.catalog_complete = True

    def model_dump(self, *, mode: str) -> dict[str, object]:
        del mode
        return dict(self.__dict__)


class _Manifest:
    def __init__(self, authorization, results: list[_SourceResult]) -> None:
        self.run_id = "phase7d3b-smoke-run"
        self.plan_id = authorization.production_plan_id
        self.cohort_sha256 = authorization.cohort_sha256
        self.execution_mode = "write"
        self.selected_source_ids = authorization.source_ids
        self.requested_source_count = authorization.source_count
        self.completed_source_count = authorization.source_count
        self.successful_source_count = authorization.source_count
        self.failed_source_count = 0
        self.blocked_source_count = 0
        self.cancelled_source_count = 0
        self.extracted_job_count = authorization.source_count
        self.accepted_job_count = authorization.source_count
        self.quarantined_job_count = 0
        self.inserted_job_count = authorization.source_count
        self.updated_job_count = 0
        self.unchanged_job_count = 0
        self.reactivated_job_count = 0
        self.source_results = results
        self.controls = {
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


def main() -> None:
    source_ids = [f"cert_guarded_{index:02d}" for index in range(1, 27)]
    sources = [
        Phase6ASource(
            source_id=source_id,
            source_row=index,
            display_name=source_id,
            listing_url=f"https://guarded-{index}.example/jobs",
            detected_platform="custom_listing",
            bounded_extracted_jobs=10,
            evidence_run_id="phase7d1-smoke",
        )
        for index, source_id in enumerate(source_ids, start=1)
    ]
    production_plan = Phase6AIngestionPlan(
        plan_id="phase7d3b-smoke-plan",
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        cohort_sha256="a" * 64,
        cohort_source_count=26,
        selected_source_count=26,
        deferred_source_count=76,
        selected_source_ids=source_ids,
        sources=sources,
        controls={
            "execution_mode": "plan_only",
            "production_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
        },
    )
    seeded = [source_ids[4], source_ids[22]]
    selection = Phase7D2PilotSelection(
        plan_id=production_plan.plan_id,
        cohort_sha256=production_plan.cohort_sha256,
        quality_report_sha256="b" * 64,
        full_cohort_source_count=26,
        deferred_source_count=76,
        source_ids=seeded,
        expected_source_count=2,
    )
    rollout = build_phase7d3a_rollout_plan(
        production_plan=production_plan,
        selection=selection,
        semantic_closeout={
            "source_ids": seeded,
            "generated_from_run_id": "phase7d2c-smoke",
            "semantic_idempotency": {"accepted": 20},
            "evidence": {},
        },
        semantic_closeout_sha256="c" * 64,
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        production_path = root / "production.json"
        production_path.write_text(
            json.dumps(production_plan.model_dump(mode="json")),
            encoding="utf-8",
        )
        rollout_path = root / "rollout.json"
        write_phase7d3a_rollout_plan(rollout_path, rollout)
        _, authorization = load_phase7d3b_authorization(
            rollout_plan_path=rollout_path,
            production_plan_path=production_path,
            batch_ordinal=1,
        )
        require_phase7d3b_write_confirmation(PHASE_7D3B_WRITE_CONFIRMATION)
        results = [_SourceResult(source_id) for source_id in authorization.source_ids]
        manifest = _Manifest(authorization, results)
        empty = {source_id: 0 for source_id in authorization.source_ids}
        one = {source_id: 1 for source_id in authorization.source_ids}
        report = build_phase7d3b_batch_report(
            authorization=authorization,
            manifest=manifest,
            before=Phase7D3BSourceSnapshot(
                source_ids=authorization.source_ids,
                current_by_source=empty,
                active_by_source=empty,
                current_total=0,
                active_total=0,
            ),
            after=Phase7D3BSourceSnapshot(
                source_ids=authorization.source_ids,
                current_by_source=one,
                active_by_source=one,
                current_total=authorization.source_count,
                active_total=authorization.source_count,
            ),
        )
        report_path = write_phase7d3b_report(root / "report.json", report)
        validated = read_phase7d3b_report(report_path)

    assert validated["status"] == "passed"
    assert validated["source_count"] == 4
    assert validated["complete_catalog_source_count"] == 4
    assert validated["controls"]["max_jobs_per_source"] is None
    assert validated["controls"]["lifecycle_reconciliation_enabled"] is False
    print(
        "PHASE_7D3B_GUARDED_BATCH_SMOKE_OK",
        json.dumps(
            {
                "batch": "1/7",
                "sources": 4,
                "catalog_mode": "complete",
                "max_jobs_per_source": None,
                "page_safety_cap": 500,
                "checkpoint_guard": True,
                "reconciliation": False,
                "deactivation": False,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

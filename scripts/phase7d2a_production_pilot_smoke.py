from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_pilot import (
    Phase7D2PilotSelection,
    ProductionPilotError,
    ReadOnlyDatabaseProxy,
    build_phase7d2a_pilot_report,
)


class _Collection:
    def find_one(self, query: dict) -> dict:
        return {"query": query}

    def update_one(self, *args, **kwargs) -> None:
        del args, kwargs
        raise AssertionError("Underlying write method must never be reached")


class _Database:
    def __init__(self) -> None:
        self.collection = _Collection()

    def __getitem__(self, name: str) -> _Collection:
        del name
        return self.collection


def main() -> None:
    selection = Phase7D2PilotSelection(
        plan_id="phase6a-smoke-plan",
        cohort_sha256="a" * 64,
        quality_report_sha256="b" * 64,
        full_cohort_source_count=26,
        deferred_source_count=76,
        source_ids=["repair_alpha", "repair_beta"],
        expected_source_count=2,
    )
    source_results = [
        {
            "source_id": source_id,
            "status": "success",
            "attempts": [{"attempt_number": 1, "status": "success"}],
            "discovered_count": 10,
            "extracted_count": 10,
            "accepted_count": 10,
            "quarantined_count": 0,
            "rejected_count": 0,
        }
        for source_id in selection.source_ids
    ]
    report = build_phase7d2a_pilot_report(
        selection=selection,
        manifest={
            "run_id": "phase7d2a-smoke",
            "generated_at": "2026-07-21T00:00:00+00:00",
            "plan_id": selection.plan_id,
            "cohort_sha256": selection.cohort_sha256,
            "execution_mode": "dry_run",
            "selected_source_ids": selection.source_ids,
            "requested_source_count": 2,
            "completed_source_count": 2,
            "successful_source_count": 2,
            "failed_source_count": 0,
            "blocked_source_count": 0,
            "cancelled_source_count": 0,
            "extracted_job_count": 20,
            "accepted_job_count": 20,
            "quarantined_job_count": 0,
            "inserted_job_count": 20,
            "updated_job_count": 0,
            "unchanged_job_count": 0,
            "reactivated_job_count": 0,
            "source_results": source_results,
            "controls": {
                "normalized_job_writes_enabled": False,
                "lifecycle_reconciliation_enabled": False,
                "deactivation_enabled": False,
                "max_source_concurrency": 1,
                "max_attempts": 1,
                "max_jobs_per_source": 10,
                "bounded_pilot_execution": True,
            },
        },
        database_write_guard_enabled=True,
    )
    proxy = ReadOnlyDatabaseProxy(_Database())
    if proxy["jobs_current"].find_one({"job_id": "1"})["query"] != {"job_id": "1"}:
        raise SystemExit("Phase 7D2A read-only preview failed")
    write_blocked = False
    try:
        proxy["jobs_current"].update_one({}, {"$set": {"title": "changed"}})
    except ProductionPilotError:
        write_blocked = True
    if report["status"] != "passed" or not write_blocked:
        raise SystemExit("Phase 7D2A safety smoke failed")
    print(
        "PHASE_7D2A_PRODUCTION_PILOT_SMOKE_OK",
        json.dumps(
            {
                "pilot_sources": 2,
                "read_only_guard": True,
                "ready_for_phase7d2b": True,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

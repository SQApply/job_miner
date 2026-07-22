from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_canary import (
    PHASE_7D2B_WRITE_CONFIRMATION,
    Phase7D2BSourceSnapshot,
    Phase7D2BWriteAuthorization,
    ProductionCanaryError,
    build_phase7d2b_write_report,
    require_phase7d2b_write_confirmation,
)


class _Collection:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    @staticmethod
    def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
        for key, expected in query.items():
            actual = document.get(key)
            if isinstance(expected, dict) and "$in" in expected:
                if actual not in expected["$in"]:
                    return False
            elif actual != expected:
                return False
        return True

    def find(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        return [document for document in self.documents if self._matches(document, query)]

    def count_documents(self, query: dict[str, Any]) -> int:
        return len(self.find(query))


class _Database:
    def __init__(self, collections: dict[str, list[dict[str, Any]]]) -> None:
        self.collections = {
            name: _Collection(documents) for name, documents in collections.items()
        }

    def __getitem__(self, name: str) -> _Collection:
        return self.collections.setdefault(name, _Collection([]))


def main() -> None:
    source_ids = ["repair_alpha", "repair_beta"]
    authorization = Phase7D2BWriteAuthorization(
        plan_id="phase6a-smoke",
        cohort_sha256="a" * 64,
        quality_report_sha256="b" * 64,
        pilot_report_sha256="c" * 64,
        pilot_manifest_sha256="d" * 64,
        pilot_run_id="phase7d2a-smoke",
        source_ids=source_ids,
        expected_source_count=2,
    )
    confirmation_blocked = False
    try:
        require_phase7d2b_write_confirmation("")
    except ProductionCanaryError:
        confirmation_blocked = True
    require_phase7d2b_write_confirmation(PHASE_7D2B_WRITE_CONFIRMATION)

    jobs = [
        {
            "job_id": f"job-{index}",
            "source_id": source_id,
            "external_job_id": f"REQ-{index}",
            "identity_hash": str(index) * 64,
            "last_fleet_run_id": "phase7d2b-smoke",
            "is_active": True,
            "deactivated_at": None,
            "version": 1,
        }
        for index, source_id in enumerate(source_ids, start=1)
    ]
    database = _Database(
        {
            "production_ingestion_fleet_runs": [
                {"fleet_run_id": "phase7d2b-smoke", "status": "completed"}
            ],
            "production_ingestion_source_runs": [
                {
                    "fleet_run_id": "phase7d2b-smoke",
                    "source_id": source_id,
                    "source_run_id": f"source-{index}",
                    "status": "success",
                }
                for index, source_id in enumerate(source_ids, start=1)
            ],
            "production_raw_job_evidence": [
                {"fleet_run_id": "phase7d2b-smoke", "source_id": source_id}
                for source_id in source_ids
            ],
            "production_job_quarantine": [],
            "jobs_current": jobs,
            "jobs_history": [
                {"job_id": f"job-{index}"} for index in range(1, 3)
            ],
        }
    )
    results = [
        {
            "source_id": source_id,
            "source_run_id": f"source-{index}",
            "status": "success",
            "attempts": [{"attempt_number": 1, "status": "success"}],
            "discovered_count": 1,
            "extracted_count": 1,
            "accepted_count": 1,
            "quarantined_count": 0,
            "rejected_count": 0,
            "inserted_count": 1,
            "updated_count": 0,
            "unchanged_count": 0,
            "reactivated_count": 0,
        }
        for index, source_id in enumerate(source_ids, start=1)
    ]
    report = build_phase7d2b_write_report(
        authorization=authorization,
        manifest={
            "run_id": "phase7d2b-smoke",
            "generated_at": "2026-07-22T00:00:00+00:00",
            "plan_id": authorization.plan_id,
            "cohort_sha256": authorization.cohort_sha256,
            "execution_mode": "write",
            "selected_source_ids": source_ids,
            "requested_source_count": 2,
            "completed_source_count": 2,
            "successful_source_count": 2,
            "failed_source_count": 0,
            "blocked_source_count": 0,
            "cancelled_source_count": 0,
            "extracted_job_count": 2,
            "accepted_job_count": 2,
            "quarantined_job_count": 0,
            "inserted_job_count": 2,
            "updated_job_count": 0,
            "unchanged_job_count": 0,
            "reactivated_job_count": 0,
            "source_results": results,
            "controls": {
                "normalized_job_writes_enabled": True,
                "lifecycle_reconciliation_enabled": False,
                "deactivation_enabled": False,
                "max_source_concurrency": 1,
                "max_attempts": 1,
                "max_jobs_per_source": 10,
                "bounded_pilot_execution": True,
            },
        },
        database=database,
        before=Phase7D2BSourceSnapshot(
            source_ids=source_ids,
            current_jobs=0,
            active_jobs=0,
            inactive_jobs=0,
            source_job_counts={source_id: 0 for source_id in source_ids},
            duplicate_identity_hashes=0,
            duplicate_external_job_keys=0,
        ),
        index_definitions_ensured={
            "production_ingestion_fleet_runs": 3,
            "production_ingestion_source_runs": 4,
            "production_raw_job_evidence": 5,
            "production_job_quarantine": 5,
            "jobs_current": 12,
            "jobs_history": 2,
        },
    )
    if not confirmation_blocked or report["status"] != "passed":
        raise SystemExit("Phase 7D2B write-canary smoke failed")
    print(
        "PHASE_7D2B_WRITE_CANARY_SMOKE_OK",
        json.dumps(
            {
                "confirmation_required": True,
                "pilot_sources": 2,
                "ready_for_phase7d2c": True,
                "writes_performed": False,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

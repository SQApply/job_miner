from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_canary import Phase7D2BSourceSnapshot
from src.portals.production_idempotency import (
    PHASE_7D2C_WRITE_CONFIRMATION,
    Phase7D2CIdempotencyAuthorization,
    ProductionIdempotencyError,
    build_phase7d2c_report,
    require_phase7d2c_write_confirmation,
)


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        actual = document.get(key)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif actual != expected:
            return False
    return True


class _Collection:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    def find(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        return [row.copy() for row in self.documents if _matches(row, query)]

    def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for row in self.documents if _matches(row, query))


class _Database:
    def __init__(self, source_ids: list[str], run_id: str) -> None:
        self.collections = {
            "jobs_current": _Collection(
                [
                    {
                        "job_id": f"job-{index}",
                        "source_id": source_id,
                        "external_job_id": f"REQ-{index}",
                        "identity_hash": str(index) * 64,
                        "last_fleet_run_id": run_id,
                        "is_active": True,
                        "deactivated_at": None,
                        "version": 1,
                    }
                    for index, source_id in enumerate(source_ids, start=1)
                ]
            )
        }

    def __getitem__(self, name: str) -> _Collection:
        return self.collections.setdefault(name, _Collection([]))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    confirmation_required = False
    try:
        require_phase7d2c_write_confirmation("")
    except ProductionIdempotencyError:
        confirmation_required = True
    require_phase7d2c_write_confirmation(PHASE_7D2C_WRITE_CONFIRMATION)

    source_ids = ["cert_alpha", "cert_beta"]
    run_id = "phase7d2c_smoke"
    authorization = Phase7D2CIdempotencyAuthorization(
        plan_id="phase6a-smoke-plan",
        cohort_sha256="a" * 64,
        phase7d2b_report_sha256="b" * 64,
        first_write_manifest_sha256="c" * 64,
        first_write_run_id="phase7d2b_smoke",
        source_ids=source_ids,
        expected_source_count=2,
        expected_job_count=2,
        expected_source_job_counts={source_id: 1 for source_id in source_ids},
    )
    source_results = [
        {
            "source_id": source_id,
            "source_run_id": f"{run_id}-{index}",
            "status": "success",
            "attempts": [{"attempt_number": 1, "status": "success"}],
            "accepted_count": 1,
            "inserted_count": 0,
            "updated_count": 0,
            "unchanged_count": 1,
            "reactivated_count": 0,
            "quarantined_count": 0,
            "rejected_count": 0,
        }
        for index, source_id in enumerate(source_ids, start=1)
    ]
    rerun = {
        "run_id": run_id,
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
        "accepted_job_count": 2,
        "inserted_job_count": 0,
        "updated_job_count": 0,
        "unchanged_job_count": 2,
        "reactivated_job_count": 0,
        "quarantined_job_count": 0,
        "source_results": source_results,
        "controls": {
            "normalized_job_writes_enabled": True,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "max_source_concurrency": 1,
            "max_attempts": 1,
            "max_jobs_per_source": 10,
            "bounded_pilot_execution": True,
        },
    }
    before = Phase7D2BSourceSnapshot(
        source_ids=source_ids,
        current_jobs=2,
        active_jobs=2,
        inactive_jobs=0,
        source_job_counts={source_id: 1 for source_id in source_ids},
        duplicate_identity_hashes=0,
        duplicate_external_job_keys=0,
    )

    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        manifest_path = directory / "rerun.json"
        manifest_path.write_text(json.dumps(rerun), encoding="utf-8")
        closeout = {
            "status": "passed",
            "ready_for_phase7": True,
            "plan_id": authorization.plan_id,
            "cohort_sha256": authorization.cohort_sha256,
            "expected_source_count": 2,
            "selected_source_ids": source_ids,
            "first_write_run_id": authorization.first_write_run_id,
            "rerun_run_id": run_id,
            "first_write_manifest_sha256": authorization.first_write_manifest_sha256,
            "rerun_manifest_sha256": _sha256(manifest_path),
            "database_checks": {
                "fleet_run_records": 2,
                "source_run_records_first": 2,
                "source_run_records_rerun": 2,
                "current_jobs_observed_on_rerun": 2,
                "active_jobs_observed_on_rerun": 2,
                "duplicate_identity_hashes": 0,
                "duplicate_external_job_keys": 0,
                "history_mismatches": 0,
                "unsafe_deactivated_jobs": 0,
                "quarantine_records_first": 0,
                "quarantine_records_rerun": 0,
            },
            "controls": {
                "normalized_job_writes_enabled": True,
                "lifecycle_reconciliation_enabled": False,
                "deactivation_enabled": False,
            },
            "issues": [],
        }
        closeout_path = directory / "closeout.json"
        closeout_path.write_text(json.dumps(closeout), encoding="utf-8")
        report = build_phase7d2c_report(
            authorization=authorization,
            rerun_manifest=rerun,
            rerun_manifest_path=manifest_path,
            closeout_report=closeout,
            closeout_report_path=closeout_path,
            database=_Database(source_ids, run_id),
            before=before,
        )

    assert confirmation_required
    assert report["status"] == "passed"
    assert report["ready_for_phase7d3"] is True
    assert report["idempotency_counts"] == {
        "expected_jobs": 2,
        "accepted": 2,
        "inserted": 0,
        "updated": 0,
        "unchanged": 2,
        "reactivated": 0,
        "quarantined": 0,
    }
    print(
        "PHASE_7D2C_IDEMPOTENCY_SMOKE_OK",
        json.dumps(
            {
                "confirmation_required": confirmation_required,
                "expected_jobs": 2,
                "inserted": 0,
                "updated": 0,
                "ready_for_phase7d3": True,
                "writes_performed": False,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

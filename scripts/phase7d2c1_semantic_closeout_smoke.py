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

from src.portals.production_semantic_closeout import (
    PHASE_7D2C1_ACKNOWLEDGEMENT,
    ProductionSemanticCloseoutError,
    build_phase7d2c1_semantic_closeout,
    require_phase7d2c1_acknowledgement,
)


def _canonical(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    acknowledgement_required = False
    try:
        require_phase7d2c1_acknowledgement("")
    except ProductionSemanticCloseoutError:
        acknowledgement_required = True
    require_phase7d2c1_acknowledgement(PHASE_7D2C1_ACKNOWLEDGEMENT)

    source_ids = ["cert_rxrelief", "cert_optech"]
    plan_id = "phase6a-smoke"
    cohort_sha = "a" * 64
    first_run = "phase7d2b_smoke"
    rerun_run = "phase7d2c_smoke"
    first_results = [
        {
            "source_id": source_id,
            "status": "success",
            "accepted_count": 10,
            "inserted_count": 10,
        }
        for source_id in source_ids
    ]
    rerun_results = [
        {
            "source_id": source_id,
            "status": "success",
            "attempts": [{"attempt_number": 1, "status": "success"}],
            "accepted_count": 10,
            "inserted_count": 0,
            "updated_count": 1,
            "unchanged_count": 9,
            "reactivated_count": 0,
            "quarantined_count": 0,
            "rejected_count": 0,
        }
        for source_id in source_ids
    ]

    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        first_report: dict[str, Any] = {
            "contract_version": "1.0",
            "phase": "7D2B",
            "status": "passed",
            "ready_for_phase7d2c": True,
            "blockers": [],
            "run_id": first_run,
            "plan_id": plan_id,
            "cohort_sha256": cohort_sha,
        }
        first_report["report_sha256"] = _canonical(first_report)
        first_report_path = _write(directory / "first_report.json", first_report)
        first_manifest = {
            "run_id": first_run,
            "plan_id": plan_id,
            "cohort_sha256": cohort_sha,
            "selected_source_ids": source_ids,
            "accepted_job_count": 20,
            "inserted_job_count": 20,
            "updated_job_count": 0,
            "unchanged_job_count": 0,
            "reactivated_job_count": 0,
            "quarantined_job_count": 0,
            "source_results": first_results,
        }
        first_manifest_path = _write(directory / "first_manifest.json", first_manifest)
        rerun = {
            "run_id": rerun_run,
            "plan_id": plan_id,
            "cohort_sha256": cohort_sha,
            "execution_mode": "write",
            "selected_source_ids": source_ids,
            "requested_source_count": 2,
            "completed_source_count": 2,
            "successful_source_count": 2,
            "failed_source_count": 0,
            "blocked_source_count": 0,
            "cancelled_source_count": 0,
            "accepted_job_count": 20,
            "inserted_job_count": 0,
            "updated_job_count": 2,
            "unchanged_job_count": 18,
            "reactivated_job_count": 0,
            "quarantined_job_count": 0,
            "source_results": rerun_results,
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
        rerun_path = _write(directory / "rerun.json", rerun)
        checks = {
            "fleet_run_records": 2,
            "source_run_records_first": 2,
            "source_run_records_rerun": 2,
            "current_jobs_observed_on_rerun": 20,
            "active_jobs_observed_on_rerun": 20,
            "duplicate_identity_hashes": 0,
            "duplicate_external_job_keys": 0,
            "history_mismatches": 0,
            "unsafe_deactivated_jobs": 0,
            "quarantine_records_first": 0,
            "quarantine_records_rerun": 0,
        }
        closeout = {
            "status": "passed",
            "ready_for_phase7": True,
            "plan_id": plan_id,
            "cohort_sha256": cohort_sha,
            "selected_source_ids": source_ids,
            "first_write_run_id": first_run,
            "rerun_run_id": rerun_run,
            "first_write_manifest_sha256": _sha(first_manifest_path),
            "rerun_manifest_sha256": _sha(rerun_path),
            "controls": {
                "normalized_job_writes_enabled": True,
                "lifecycle_reconciliation_enabled": False,
                "deactivation_enabled": False,
            },
            "database_checks": checks,
            "issues": [],
        }
        closeout_path = _write(directory / "closeout.json", closeout)
        strict_blockers = [
            "rerun_manifest_count_mismatch:updated_job_count",
            "rerun_manifest_count_mismatch:unchanged_job_count",
        ]
        for source_id in source_ids:
            strict_blockers.extend(
                [
                    f"rerun_source_count_mismatch:{source_id}:updated_count",
                    f"rerun_source_count_mismatch:{source_id}:unchanged_count",
                ]
            )
        strict: dict[str, Any] = {
            "contract_version": "1.0",
            "phase": "7D2C",
            "status": "failed",
            "ready_for_phase7d3": False,
            "run_id": rerun_run,
            "plan_id": plan_id,
            "cohort_sha256": cohort_sha,
            "phase7d2b_report_sha256": first_report["report_sha256"],
            "first_write_manifest_sha256": _sha(first_manifest_path),
            "first_write_run_id": first_run,
            "rerun_manifest_sha256": _sha(rerun_path),
            "phase6f_closeout_sha256": _sha(closeout_path),
            "source_ids": source_ids,
            "controls": {
                "explicit_rerun_confirmation_required": True,
                "normalized_job_writes_enabled": True,
                "index_creation_performed": False,
                "lifecycle_reconciliation_enabled": False,
                "deactivation_enabled": False,
                "full_cohort_execution_performed": False,
                "automatic_rollback_enabled": False,
            },
            "blockers": strict_blockers,
        }
        strict["report_sha256"] = _canonical(strict)
        strict_path = _write(directory / "strict.json", strict)
        report = build_phase7d2c1_semantic_closeout(
            first_write_report_path=first_report_path,
            first_write_manifest_path=first_manifest_path,
            rerun_manifest_path=rerun_path,
            strict_report_path=strict_path,
            phase6f_closeout_path=closeout_path,
        )

    assert acknowledgement_required
    assert report["ready_for_phase7d3"] is True
    assert report["semantic_idempotency"]["inserted"] == 0
    assert report["semantic_idempotency"]["updated"] == 2
    assert report["semantic_idempotency"]["unchanged"] == 18
    assert report["controls"]["network_requests_performed"] is False
    assert report["controls"]["mongodb_writes_performed"] is False
    print(
        "PHASE_7D2C1_SEMANTIC_CLOSEOUT_SMOKE_OK",
        json.dumps(
            {
                "acknowledgement_required": True,
                "inserted": 0,
                "updated": 2,
                "unchanged": 18,
                "ready_for_phase7d3": True,
                "network": False,
                "mongodb_writes": False,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

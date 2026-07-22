from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from src.portals.production_semantic_closeout import (
    PHASE_7D2C1_ACKNOWLEDGEMENT,
    ProductionSemanticCloseoutError,
    build_phase7d2c1_semantic_closeout,
    require_phase7d2c1_acknowledgement,
)


SOURCE_IDS = ["cert_rxrelief", "cert_optech"]


def _canonical(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _results(*, first: bool, inserted: int = 0) -> list[dict[str, Any]]:
    return [
        {
            "source_id": source_id,
            "status": "success",
            "attempts": [{"attempt_number": 1, "status": "success"}],
            "accepted_count": 10,
            "inserted_count": 10 if first else inserted,
            "updated_count": 0 if first else 1,
            "unchanged_count": 0 if first else 9 - inserted,
            "reactivated_count": 0,
            "quarantined_count": 0,
            "rejected_count": 0,
        }
        for source_id in SOURCE_IDS
    ]


def _fixture(
    root: Path,
    *,
    rerun_inserted: int = 0,
    closeout_passed: bool = True,
    extra_strict_blocker: str | None = None,
) -> dict[str, Path]:
    plan_id = "phase6a-test"
    cohort_sha = "a" * 64
    first_run = "phase7d2b_first"
    rerun_run = "phase7d2c_rerun"
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
    first_report_path = _write(root / "first_report.json", first_report)

    first_manifest = {
        "run_id": first_run,
        "plan_id": plan_id,
        "cohort_sha256": cohort_sha,
        "selected_source_ids": SOURCE_IDS,
        "accepted_job_count": 20,
        "inserted_job_count": 20,
        "updated_job_count": 0,
        "unchanged_job_count": 0,
        "reactivated_job_count": 0,
        "quarantined_job_count": 0,
        "source_results": _results(first=True),
    }
    first_manifest_path = _write(root / "first_manifest.json", first_manifest)

    inserted_total = rerun_inserted * 2
    rerun = {
        "run_id": rerun_run,
        "plan_id": plan_id,
        "cohort_sha256": cohort_sha,
        "execution_mode": "write",
        "selected_source_ids": SOURCE_IDS,
        "requested_source_count": 2,
        "completed_source_count": 2,
        "successful_source_count": 2,
        "failed_source_count": 0,
        "blocked_source_count": 0,
        "cancelled_source_count": 0,
        "accepted_job_count": 20,
        "inserted_job_count": inserted_total,
        "updated_job_count": 2,
        "unchanged_job_count": 18 - inserted_total,
        "reactivated_job_count": 0,
        "quarantined_job_count": 0,
        "source_results": _results(first=False, inserted=rerun_inserted),
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
    rerun_path = _write(root / "rerun.json", rerun)

    closeout = {
        "status": "passed" if closeout_passed else "failed",
        "ready_for_phase7": closeout_passed,
        "plan_id": plan_id,
        "cohort_sha256": cohort_sha,
        "selected_source_ids": SOURCE_IDS,
        "first_write_run_id": first_run,
        "rerun_run_id": rerun_run,
        "first_write_manifest_sha256": _sha(first_manifest_path),
        "rerun_manifest_sha256": _sha(rerun_path),
        "controls": {
            "normalized_job_writes_enabled": True,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
        },
        "database_checks": {
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
        },
        "issues": [] if closeout_passed else ["closeout failed"],
    }
    closeout_path = _write(root / "closeout.json", closeout)

    strict_blockers = [
        "rerun_manifest_count_mismatch:updated_job_count",
        "rerun_manifest_count_mismatch:unchanged_job_count",
        "rerun_source_count_mismatch:cert_rxrelief:updated_count",
        "rerun_source_count_mismatch:cert_rxrelief:unchanged_count",
        "rerun_source_count_mismatch:cert_optech:updated_count",
        "rerun_source_count_mismatch:cert_optech:unchanged_count",
    ]
    if extra_strict_blocker:
        strict_blockers.append(extra_strict_blocker)
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
        "source_ids": SOURCE_IDS,
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
    strict_path = _write(root / "strict.json", strict)
    return {
        "first_report": first_report_path,
        "first_manifest": first_manifest_path,
        "rerun": rerun_path,
        "strict": strict_path,
        "closeout": closeout_path,
    }


def _build(paths: dict[str, Path]) -> dict[str, Any]:
    return build_phase7d2c1_semantic_closeout(
        first_write_report_path=paths["first_report"],
        first_write_manifest_path=paths["first_manifest"],
        rerun_manifest_path=paths["rerun"],
        strict_report_path=paths["strict"],
        phase6f_closeout_path=paths["closeout"],
    )


class Phase7D2C1SemanticCloseoutTests(unittest.TestCase):
    def test_exact_observed_variance_passes_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            report = _build(_fixture(Path(raw)))
        self.assertEqual(report["status"], "passed_with_bounded_content_variance")
        self.assertTrue(report["ready_for_phase7d3"])
        self.assertEqual(report["semantic_idempotency"]["updated"], 2)
        self.assertEqual(report["semantic_idempotency"]["unchanged"], 18)
        self.assertFalse(report["controls"]["mongodb_writes_performed"])

    def test_explicit_acknowledgement_is_required(self) -> None:
        with self.assertRaisesRegex(
            ProductionSemanticCloseoutError,
            "accept-bounded-content-variance",
        ):
            require_phase7d2c1_acknowledgement("")
        require_phase7d2c1_acknowledgement(PHASE_7D2C1_ACKNOWLEDGEMENT)

    def test_tampered_strict_report_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            paths = _fixture(Path(raw))
            strict = json.loads(paths["strict"].read_text(encoding="utf-8"))
            strict["ready_for_phase7d3"] = True
            _write(paths["strict"], strict)
            with self.assertRaisesRegex(
                ProductionSemanticCloseoutError,
                "checksum",
            ):
                _build(paths)

    def test_new_insert_remains_production_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            report = _build(_fixture(Path(raw), rerun_inserted=1))
        self.assertFalse(report["ready_for_phase7d3"])
        self.assertIn("rerun_inserted_new_identities", report["blockers"])

    def test_failed_phase6f_closeout_remains_production_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            report = _build(_fixture(Path(raw), closeout_passed=False))
        self.assertFalse(report["ready_for_phase7d3"])
        self.assertIn("phase6f_closeout_did_not_pass", report["blockers"])

    def test_non_variance_strict_blocker_cannot_be_waived(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            report = _build(
                _fixture(
                    Path(raw),
                    extra_strict_blocker="rerun_created_duplicate_identity_hashes",
                )
            )
        self.assertFalse(report["ready_for_phase7d3"])
        self.assertIn(
            "strict_failure_contains_non_variance_blockers",
            report["blockers"],
        )


if __name__ == "__main__":
    unittest.main()

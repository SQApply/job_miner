from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from specs.test_phase7d2a_production_pilot import (
    _fixture as phase7d2a_input_fixture,
    _successful_manifest as phase7d2a_successful_manifest,
)
from src.portals.production_canary import (
    Phase7D2BSourceSnapshot,
    build_phase7d2b_write_report,
    load_phase7d2b_write_authorization,
)
from src.portals.production_idempotency import (
    PHASE_7D2C_WRITE_CONFIRMATION,
    Phase7D2CIdempotencyAuthorization,
    ProductionIdempotencyError,
    build_phase7d2c_report,
    load_phase7d2c_authorization,
    require_phase7d2c_database_preflight,
    require_phase7d2c_write_confirmation,
)
from src.portals.production_pilot import (
    build_phase7d2a_pilot_report,
    load_phase7d2a_pilot_inputs,
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
    def __init__(self, documents: list[dict[str, Any]] | None = None) -> None:
        self.documents = [copy.deepcopy(document) for document in (documents or [])]

    def find(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        return [copy.deepcopy(row) for row in self.documents if _matches(row, query)]

    def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for row in self.documents if _matches(row, query))


class _Database:
    def __init__(self, collections: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.collections = {
            name: _Collection(rows) for name, rows in (collections or {}).items()
        }

    def __getitem__(self, name: str) -> _Collection:
        return self.collections.setdefault(name, _Collection())


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _indexes() -> dict[str, int]:
    return {
        "production_ingestion_fleet_runs": 3,
        "production_ingestion_source_runs": 4,
        "production_raw_job_evidence": 5,
        "production_job_quarantine": 5,
        "jobs_current": 12,
        "jobs_history": 2,
    }


def _manifest(
    source_ids: list[str],
    *,
    run_id: str,
    inserted: int,
    unchanged: int,
    updated: int = 0,
) -> dict[str, Any]:
    first_write = inserted > 0
    results = [
        {
            "source_id": source_id,
            "source_run_id": f"{run_id}-source-{index}",
            "status": "success",
            "attempts": [{"attempt_number": 1, "status": "success"}],
            "discovered_count": 1,
            "extracted_count": 1,
            "accepted_count": 1,
            "quarantined_count": 0,
            "rejected_count": 0,
            "inserted_count": 1 if first_write else 0,
            "updated_count": updated // len(source_ids),
            "unchanged_count": 0 if first_write else 1,
            "reactivated_count": 0,
            "error_type": None,
        }
        for index, source_id in enumerate(source_ids, start=1)
    ]
    return {
        "contract_version": "1.0",
        "phase": "6E",
        "run_id": run_id,
        "generated_at": "2026-07-22T00:00:00+00:00",
        "plan_id": "phase6a-test-plan",
        "cohort_sha256": "",
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
        "inserted_job_count": inserted,
        "updated_job_count": updated,
        "unchanged_job_count": unchanged,
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
    }


def _database(source_ids: list[str], *, rerun: bool) -> _Database:
    last_run = "phase7d2c_rerun" if rerun else "phase7d2b-first"
    fleets = [
        {
            "fleet_run_id": "phase7d2b-first",
            "plan_id": "phase6a-test-plan",
            "status": "completed",
        }
    ]
    source_runs = [
        {
            "fleet_run_id": "phase7d2b-first",
            "source_id": source_id,
            "source_run_id": f"phase7d2b-first-source-{index}",
            "status": "success",
        }
        for index, source_id in enumerate(source_ids, start=1)
    ]
    raw = [
        {"fleet_run_id": "phase7d2b-first", "source_id": source_id}
        for source_id in source_ids
    ]
    if rerun:
        fleets.append(
            {
                "fleet_run_id": "phase7d2c_rerun",
                "plan_id": "phase6a-test-plan",
                "status": "completed",
            }
        )
        source_runs.extend(
            {
                "fleet_run_id": "phase7d2c_rerun",
                "source_id": source_id,
                "source_run_id": f"phase7d2c_rerun-source-{index}",
                "status": "success",
            }
            for index, source_id in enumerate(source_ids, start=1)
        )
        raw.extend(
            {"fleet_run_id": "phase7d2c_rerun", "source_id": source_id}
            for source_id in source_ids
        )
    jobs = [
        {
            "job_id": f"job-{index}",
            "source_id": source_id,
            "external_job_id": f"REQ-{index}",
            "identity_hash": str(index) * 64,
            "last_fleet_run_id": last_run,
            "is_active": True,
            "deactivated_at": None,
            "version": 1,
        }
        for index, source_id in enumerate(source_ids, start=1)
    ]
    return _Database(
        {
            "production_ingestion_fleet_runs": fleets,
            "production_ingestion_source_runs": source_runs,
            "production_raw_job_evidence": raw,
            "production_job_quarantine": [],
            "jobs_current": jobs,
            "jobs_history": [
                {"job_id": f"job-{index}"} for index in range(1, 3)
            ],
        }
    )


def _artifact_chain(directory: Path):
    cohort, quality, plan_path, source_ids = phase7d2a_input_fixture(directory)
    plan, selection = load_phase7d2a_pilot_inputs(
        cohort_path=cohort,
        quality_report_path=quality,
        plan_path=plan_path,
        expected_inventory=4,
        expected_cohort_size=3,
        expected_new_sources=2,
    )
    pilot_manifest = phase7d2a_successful_manifest(selection)
    pilot_manifest.update({"contract_version": "1.0", "phase": "6E"})
    pilot_report = build_phase7d2a_pilot_report(
        selection=selection,
        manifest=pilot_manifest,
        database_write_guard_enabled=True,
    )
    pilot_manifest_path = _write(directory / "pilot_manifest.json", pilot_manifest)
    pilot_report_path = _write(directory / "pilot_report.json", pilot_report)
    _, canary = load_phase7d2b_write_authorization(
        cohort_path=cohort,
        quality_report_path=quality,
        plan_path=plan_path,
        pilot_report_path=pilot_report_path,
        pilot_manifest_path=pilot_manifest_path,
        expected_inventory=4,
        expected_cohort_size=3,
        expected_source_count=2,
    )
    first = _manifest(source_ids, run_id="phase7d2b-first", inserted=2, unchanged=0)
    first["plan_id"] = plan.plan_id
    first["cohort_sha256"] = plan.cohort_sha256
    before = Phase7D2BSourceSnapshot(
        source_ids=source_ids,
        current_jobs=0,
        active_jobs=0,
        inactive_jobs=0,
        source_job_counts={source_id: 0 for source_id in source_ids},
        duplicate_identity_hashes=0,
        duplicate_external_job_keys=0,
    )
    first_report = build_phase7d2b_write_report(
        authorization=canary,
        manifest=first,
        database=_database(source_ids, rerun=False),
        before=before,
        index_definitions_ensured=_indexes(),
    )
    first_manifest_path = _write(directory / "first_manifest.json", first)
    first_report_path = _write(directory / "first_report.json", first_report)
    return {
        "cohort": cohort,
        "quality": quality,
        "plan": plan_path,
        "pilot_report": pilot_report_path,
        "pilot_manifest": pilot_manifest_path,
        "first_report": first_report_path,
        "first_manifest": first_manifest_path,
        "source_ids": source_ids,
        "plan_model": plan,
    }


def _authorization(source_ids: list[str]) -> Phase7D2CIdempotencyAuthorization:
    return Phase7D2CIdempotencyAuthorization(
        plan_id="phase6a-test-plan",
        cohort_sha256="a" * 64,
        phase7d2b_report_sha256="b" * 64,
        first_write_manifest_sha256="c" * 64,
        first_write_run_id="phase7d2b-first",
        source_ids=source_ids,
        expected_source_count=2,
        expected_job_count=2,
        expected_source_job_counts={source_id: 1 for source_id in source_ids},
    )


def _closeout(auth: Phase7D2CIdempotencyAuthorization, rerun_path: Path) -> dict[str, Any]:
    return {
        "status": "passed",
        "ready_for_phase7": True,
        "plan_id": auth.plan_id,
        "cohort_sha256": auth.cohort_sha256,
        "expected_source_count": 2,
        "selected_source_ids": auth.source_ids,
        "first_write_run_id": auth.first_write_run_id,
        "rerun_run_id": "phase7d2c-rerun",
        "first_write_manifest_sha256": auth.first_write_manifest_sha256,
        "rerun_manifest_sha256": _sha(rerun_path),
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


class Phase7D2CIdempotencyTests(unittest.TestCase):
    def test_linked_phase7d2b_evidence_authorizes_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = _artifact_chain(Path(raw))
            _, authorization = load_phase7d2c_authorization(
                cohort_path=fixture["cohort"],
                quality_report_path=fixture["quality"],
                plan_path=fixture["plan"],
                pilot_report_path=fixture["pilot_report"],
                pilot_manifest_path=fixture["pilot_manifest"],
                first_write_report_path=fixture["first_report"],
                first_write_manifest_path=fixture["first_manifest"],
                expected_inventory=4,
                expected_cohort_size=3,
                expected_source_count=2,
            )
        self.assertEqual(authorization.source_ids, fixture["source_ids"])
        self.assertEqual(authorization.expected_job_count, 2)

    def test_tampered_first_write_report_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = _artifact_chain(Path(raw))
            payload = json.loads(fixture["first_report"].read_text(encoding="utf-8"))
            payload["status"] = "failed"
            _write(fixture["first_report"], payload)
            with self.assertRaisesRegex(ProductionIdempotencyError, "checksum"):
                load_phase7d2c_authorization(
                    cohort_path=fixture["cohort"], quality_report_path=fixture["quality"],
                    plan_path=fixture["plan"], pilot_report_path=fixture["pilot_report"],
                    pilot_manifest_path=fixture["pilot_manifest"],
                    first_write_report_path=fixture["first_report"],
                    first_write_manifest_path=fixture["first_manifest"],
                    expected_inventory=4, expected_cohort_size=3, expected_source_count=2,
                )

    def test_confirmation_is_required(self) -> None:
        with self.assertRaisesRegex(ProductionIdempotencyError, "confirm-idempotency-rerun"):
            require_phase7d2c_write_confirmation("")
        require_phase7d2c_write_confirmation(PHASE_7D2C_WRITE_CONFIRMATION)

    def test_database_preflight_accepts_first_write_and_rejects_existing_rerun(self) -> None:
        source_ids = ["repair_alpha", "repair_beta"]
        auth = _authorization(source_ids)
        snapshot = require_phase7d2c_database_preflight(
            _database(source_ids, rerun=False), authorization=auth
        )
        self.assertEqual(snapshot.current_jobs, 2)
        with self.assertRaisesRegex(ProductionIdempotencyError, "ALREADY_EXECUTED"):
            require_phase7d2c_database_preflight(
                _database(source_ids, rerun=True), authorization=auth
            )

    def test_clean_unchanged_rerun_passes(self) -> None:
        source_ids = ["repair_alpha", "repair_beta"]
        auth = _authorization(source_ids)
        rerun = _manifest(source_ids, run_id="phase7d2c-rerun", inserted=0, unchanged=2)
        rerun["cohort_sha256"] = auth.cohort_sha256
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rerun_path = _write(root / "rerun.json", rerun)
            closeout = _closeout(auth, rerun_path)
            closeout_path = _write(root / "closeout.json", closeout)
            report = build_phase7d2c_report(
                authorization=auth, rerun_manifest=rerun,
                rerun_manifest_path=rerun_path, closeout_report=closeout,
                closeout_report_path=closeout_path,
                database=_database(source_ids, rerun=True),
                before=Phase7D2BSourceSnapshot(
                    source_ids=source_ids, current_jobs=2, active_jobs=2, inactive_jobs=0,
                    source_job_counts={source_id: 1 for source_id in source_ids},
                    duplicate_identity_hashes=0, duplicate_external_job_keys=0,
                ),
            )
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["ready_for_phase7d3"])

    def test_new_insert_or_update_blocks_phase7d3(self) -> None:
        source_ids = ["repair_alpha", "repair_beta"]
        auth = _authorization(source_ids)
        rerun = _manifest(source_ids, run_id="phase7d2c-rerun", inserted=2, unchanged=0)
        rerun["cohort_sha256"] = auth.cohort_sha256
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rerun_path = _write(root / "rerun.json", rerun)
            closeout = _closeout(auth, rerun_path)
            closeout_path = _write(root / "closeout.json", closeout)
            report = build_phase7d2c_report(
                authorization=auth, rerun_manifest=rerun,
                rerun_manifest_path=rerun_path, closeout_report=closeout,
                closeout_report_path=closeout_path,
                database=_database(source_ids, rerun=True),
                before=Phase7D2BSourceSnapshot(
                    source_ids=source_ids, current_jobs=2, active_jobs=2, inactive_jobs=0,
                    source_job_counts={source_id: 1 for source_id in source_ids},
                    duplicate_identity_hashes=0, duplicate_external_job_keys=0,
                ),
            )
        self.assertEqual(report["status"], "failed")
        self.assertIn("rerun_manifest_count_mismatch:inserted_job_count", report["blockers"])

    def test_failed_phase6f_closeout_blocks_phase7d3(self) -> None:
        source_ids = ["repair_alpha", "repair_beta"]
        auth = _authorization(source_ids)
        rerun = _manifest(source_ids, run_id="phase7d2c-rerun", inserted=0, unchanged=2)
        rerun["cohort_sha256"] = auth.cohort_sha256
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rerun_path = _write(root / "rerun.json", rerun)
            closeout = _closeout(auth, rerun_path)
            closeout.update({"status": "failed", "ready_for_phase7": False, "issues": ["duplicate"]})
            closeout_path = _write(root / "closeout.json", closeout)
            report = build_phase7d2c_report(
                authorization=auth, rerun_manifest=rerun,
                rerun_manifest_path=rerun_path, closeout_report=closeout,
                closeout_report_path=closeout_path,
                database=_database(source_ids, rerun=True),
                before=Phase7D2BSourceSnapshot(
                    source_ids=source_ids, current_jobs=2, active_jobs=2, inactive_jobs=0,
                    source_job_counts={source_id: 1 for source_id in source_ids},
                    duplicate_identity_hashes=0, duplicate_external_job_keys=0,
                ),
            )
        self.assertEqual(report["status"], "failed")
        self.assertIn("phase6f_closeout_did_not_pass", report["blockers"])


if __name__ == "__main__":
    unittest.main()

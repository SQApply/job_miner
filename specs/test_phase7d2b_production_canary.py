from __future__ import annotations

import copy
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
    PHASE_7D2B_WRITE_CONFIRMATION,
    Phase7D2BSourceSnapshot,
    Phase7D2BWriteAuthorization,
    ProductionCanaryError,
    build_phase7d2b_write_report,
    capture_phase7d2b_source_snapshot,
    load_phase7d2b_write_authorization,
    require_empty_phase7d2b_source_scope,
    require_phase7d2b_write_confirmation,
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
        return [
            copy.deepcopy(document)
            for document in self.documents
            if _matches(document, query)
        ]

    def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for document in self.documents if _matches(document, query))


class _Database:
    def __init__(self, collections: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.collections = {
            name: _Collection(documents)
            for name, documents in (collections or {}).items()
        }

    def __getitem__(self, name: str) -> _Collection:
        return self.collections.setdefault(name, _Collection())


def _authorization() -> Phase7D2BWriteAuthorization:
    return Phase7D2BWriteAuthorization(
        plan_id="phase6a-plan",
        cohort_sha256="a" * 64,
        quality_report_sha256="b" * 64,
        pilot_report_sha256="c" * 64,
        pilot_manifest_sha256="d" * 64,
        pilot_run_id="phase7d2a-pilot",
        source_ids=["repair_alpha", "repair_beta"],
        expected_source_count=2,
    )


def _manifest() -> dict[str, Any]:
    source_ids = _authorization().source_ids
    results = [
        {
            "source_id": source_id,
            "source_run_id": f"source-run-{index}",
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
    return {
        "run_id": "phase7d2b-run",
        "generated_at": "2026-07-22T00:00:00+00:00",
        "plan_id": "phase6a-plan",
        "cohort_sha256": "a" * 64,
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
    }


def _before() -> Phase7D2BSourceSnapshot:
    source_ids = _authorization().source_ids
    return Phase7D2BSourceSnapshot(
        source_ids=source_ids,
        current_jobs=0,
        active_jobs=0,
        inactive_jobs=0,
        source_job_counts={source_id: 0 for source_id in source_ids},
        duplicate_identity_hashes=0,
        duplicate_external_job_keys=0,
    )


def _database() -> _Database:
    source_ids = _authorization().source_ids
    jobs = [
        {
            "job_id": f"job-{index}",
            "source_id": source_id,
            "external_job_id": f"REQ-{index}",
            "identity_hash": str(index) * 64,
            "last_fleet_run_id": "phase7d2b-run",
            "is_active": True,
            "deactivated_at": None,
            "version": 1,
        }
        for index, source_id in enumerate(source_ids, start=1)
    ]
    return _Database(
        {
            "production_ingestion_fleet_runs": [
                {"fleet_run_id": "phase7d2b-run", "status": "completed"}
            ],
            "production_ingestion_source_runs": [
                {
                    "fleet_run_id": "phase7d2b-run",
                    "source_id": source_id,
                    "source_run_id": f"source-run-{index}",
                    "status": "success",
                }
                for index, source_id in enumerate(source_ids, start=1)
            ],
            "production_raw_job_evidence": [
                {"fleet_run_id": "phase7d2b-run", "source_id": source_id}
                for source_id in source_ids
            ],
            "production_job_quarantine": [],
            "jobs_current": jobs,
            "jobs_history": [
                {"history_id": f"history-{index}", "job_id": f"job-{index}"}
                for index in range(1, 3)
            ],
        }
    )


def _indexes() -> dict[str, int]:
    return {
        "production_ingestion_fleet_runs": 3,
        "production_ingestion_source_runs": 4,
        "production_raw_job_evidence": 5,
        "production_job_quarantine": 5,
        "jobs_current": 12,
        "jobs_history": 2,
    }


class Phase7D2BProductionCanaryTests(unittest.TestCase):
    def test_authorization_requires_linked_clean_phase7d2a_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            cohort, quality, plan_path, expected_ids = phase7d2a_input_fixture(directory)
            plan, selection = load_phase7d2a_pilot_inputs(
                cohort_path=cohort,
                quality_report_path=quality,
                plan_path=plan_path,
                expected_inventory=4,
                expected_cohort_size=3,
                expected_new_sources=2,
            )
            manifest = phase7d2a_successful_manifest(selection)
            manifest.update({"contract_version": "1.0", "phase": "6E"})
            report = build_phase7d2a_pilot_report(
                selection=selection,
                manifest=manifest,
                database_write_guard_enabled=True,
            )
            manifest_path = directory / "pilot_manifest.json"
            report_path = directory / "pilot_report.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            report_path.write_text(json.dumps(report), encoding="utf-8")
            loaded_plan, authorization = load_phase7d2b_write_authorization(
                cohort_path=cohort,
                quality_report_path=quality,
                plan_path=plan_path,
                pilot_report_path=report_path,
                pilot_manifest_path=manifest_path,
                expected_inventory=4,
                expected_cohort_size=3,
                expected_source_count=2,
            )
        self.assertEqual(authorization.source_ids, expected_ids)
        self.assertEqual(authorization.pilot_run_id, "phase7d2a-test-run")
        self.assertEqual(loaded_plan.plan_id, plan.plan_id)

    def test_tampered_pilot_report_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            cohort, quality, plan_path, _ = phase7d2a_input_fixture(directory)
            _, selection = load_phase7d2a_pilot_inputs(
                cohort_path=cohort,
                quality_report_path=quality,
                plan_path=plan_path,
                expected_inventory=4,
                expected_cohort_size=3,
                expected_new_sources=2,
            )
            manifest = phase7d2a_successful_manifest(selection)
            manifest.update({"contract_version": "1.0", "phase": "6E"})
            report = build_phase7d2a_pilot_report(
                selection=selection,
                manifest=manifest,
                database_write_guard_enabled=True,
            )
            report["status"] = "failed"
            manifest_path = directory / "pilot_manifest.json"
            report_path = directory / "pilot_report.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ProductionCanaryError, "checksum"):
                load_phase7d2b_write_authorization(
                    cohort_path=cohort,
                    quality_report_path=quality,
                    plan_path=plan_path,
                    pilot_report_path=report_path,
                    pilot_manifest_path=manifest_path,
                    expected_inventory=4,
                    expected_cohort_size=3,
                    expected_source_count=2,
                )

    def test_write_confirmation_is_exact_and_case_sensitive(self) -> None:
        with self.assertRaisesRegex(ProductionCanaryError, "confirm-production-writes"):
            require_phase7d2b_write_confirmation("")
        with self.assertRaises(ProductionCanaryError):
            require_phase7d2b_write_confirmation(PHASE_7D2B_WRITE_CONFIRMATION.lower())
        require_phase7d2b_write_confirmation(PHASE_7D2B_WRITE_CONFIRMATION)

    def test_first_write_scope_must_be_empty(self) -> None:
        database = _Database(
            {
                "jobs_current": [
                    {
                        "source_id": "repair_alpha",
                        "identity_hash": "a" * 64,
                        "is_active": True,
                    }
                ]
            }
        )
        snapshot = capture_phase7d2b_source_snapshot(
            database,
            source_ids=_authorization().source_ids,
        )
        self.assertEqual(snapshot.current_jobs, 1)
        with self.assertRaisesRegex(ProductionCanaryError, "SOURCE_SCOPE_NOT_EMPTY"):
            require_empty_phase7d2b_source_scope(snapshot)

    def test_clean_first_write_report_passes(self) -> None:
        report = build_phase7d2b_write_report(
            authorization=_authorization(),
            manifest=_manifest(),
            database=_database(),
            before=_before(),
            index_definitions_ensured=_indexes(),
        )
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["ready_for_phase7d2c"])
        self.assertEqual(report["blockers"], [])
        self.assertEqual(report["after"]["current_jobs"], 2)

    def test_database_delta_mismatch_blocks_phase7d2c(self) -> None:
        database = _database()
        database["jobs_current"].documents.pop()
        report = build_phase7d2b_write_report(
            authorization=_authorization(),
            manifest=_manifest(),
            database=database,
            before=_before(),
            index_definitions_ensured=_indexes(),
        )
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["ready_for_phase7d2c"])
        self.assertIn("jobs_current_delta_mismatch", report["blockers"])

    def test_quality_rejection_and_unsafe_controls_block_phase7d2c(self) -> None:
        manifest = _manifest()
        manifest["successful_source_count"] = 1
        manifest["failed_source_count"] = 1
        manifest["quarantined_job_count"] = 1
        manifest["controls"]["deactivation_enabled"] = True
        manifest["source_results"][1]["status"] = "failed"
        manifest["source_results"][1]["accepted_count"] = 0
        manifest["source_results"][1]["inserted_count"] = 0
        manifest["source_results"][1]["quarantined_count"] = 1
        report = build_phase7d2b_write_report(
            authorization=_authorization(),
            manifest=manifest,
            database=_database(),
            before=_before(),
            index_definitions_ensured=_indexes(),
        )
        self.assertEqual(report["status"], "failed")
        self.assertIn(
            "unsafe_or_inconsistent_manifest_control:deactivation_enabled",
            report["blockers"],
        )


if __name__ == "__main__":
    unittest.main()

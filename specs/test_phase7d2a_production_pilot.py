from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.portals.production_ingestion import Phase6AIngestionPlan, Phase6ASource
from src.portals.production_pilot import (
    Phase7D2PilotSelection,
    ProductionPilotError,
    ReadOnlyDatabaseProxy,
    build_phase7d2a_pilot_report,
    load_phase7d2a_pilot_inputs,
)


def _canonical_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source(source_id: str, row: int) -> Phase6ASource:
    return Phase6ASource(
        source_id=source_id,
        source_row=row,
        display_name=source_id.replace("_", " ").title(),
        listing_url=f"https://{source_id}.example/jobs",
        detected_platform="custom_listing",
        bounded_extracted_jobs=10,
        evidence_run_id="repair-run",
    )


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fixture(directory: Path) -> tuple[Path, Path, Path, list[str]]:
    source_ids = ["baseline", "repair_alpha", "repair_beta", "deferred"]
    ready_ids = source_ids[:3]
    new_ids = source_ids[1:3]
    sources = [_source(source_id, index + 1) for index, source_id in enumerate(ready_ids)]

    quality: dict = {
        "contract_version": "1.0",
        "phase": "7D1",
        "report_status": "truthful_evidence_revalidation_complete",
        "generated_at": "2026-07-21T00:00:00+00:00",
        "counts": {
            "inventory": 4,
            "baseline_production_ready": 1,
            "repair_records": 2,
            "newly_production_ready": 2,
            "final_production_ready": 3,
            "deferred": 1,
        },
        "newly_production_ready_source_ids": new_ids,
        "final_production_ready_source_ids": ready_ids,
        "deferred_source_ids": [source_ids[3]],
        "records": [
            {
                "source_id": source_id,
                "evidence_origin": "repair" if source_id in new_ids else "baseline",
                "production_ready": source_id in ready_ids,
                "classification": "production_ready" if source_id in ready_ids else "deferred",
            }
            for source_id in source_ids
        ],
    }
    quality["report_sha256"] = _canonical_sha256(quality)

    cohort: dict = {
        "contract_version": "1.1",
        "phase": "7D1",
        "cohort_status": "frozen_truthful_production_cohort",
        "frozen_at": "2026-07-21T00:00:00+00:00",
        "inventory": {"source_count": 4},
        "evidence": {"quality_report_sha256": quality["report_sha256"]},
        "cohort_source_count": 3,
        "deferred_source_count": 1,
        "cohort_source_ids": ready_ids,
        "deferred_source_ids": [source_ids[3]],
        "sources": [source.model_dump(mode="json") for source in sources],
    }
    cohort["cohort_sha256"] = _canonical_sha256(cohort)

    plan = Phase6AIngestionPlan(
        plan_id="phase6a-test-plan",
        generated_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        cohort_sha256=cohort["cohort_sha256"],
        cohort_source_count=3,
        selected_source_count=3,
        deferred_source_count=1,
        selected_source_ids=ready_ids,
        sources=sources,
        controls={
            "execution_mode": "plan_only",
            "max_source_concurrency": 1,
            "production_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
        },
    )
    return (
        _write(directory / "cohort.json", cohort),
        _write(directory / "quality.json", quality),
        _write(directory / "plan.json", plan.model_dump(mode="json")),
        new_ids,
    )


def _successful_manifest(selection: Phase7D2PilotSelection) -> dict:
    results = [
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
    return {
        "run_id": "phase7d2a-test-run",
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
        "reactivated_job_count": 0,
        "unchanged_job_count": 0,
        "source_results": results,
        "controls": {
            "normalized_job_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "max_source_concurrency": 1,
            "max_attempts": 1,
            "max_jobs_per_source": 10,
            "bounded_pilot_execution": True,
        },
    }


class _FakeCollection:
    def __init__(self) -> None:
        self.write_calls = 0

    def find_one(self, query: dict) -> dict:
        return {"query": query}

    def update_one(self, *args, **kwargs) -> None:
        del args, kwargs
        self.write_calls += 1

    def create_index(self, *args, **kwargs) -> None:
        del args, kwargs
        self.write_calls += 1

    def with_options(self, *args, **kwargs) -> "_FakeCollection":
        del args, kwargs
        return self


class _FakeDatabase:
    def __init__(self) -> None:
        self.collection = _FakeCollection()

    def __getitem__(self, name: str) -> _FakeCollection:
        del name
        return self.collection

    def get_collection(self, name: str, *args, **kwargs) -> _FakeCollection:
        del name, args, kwargs
        return self.collection

    def command(self, *args, **kwargs) -> dict:
        del args, kwargs
        return {"ok": 1}


class Phase7D2AProductionPilotTests(unittest.TestCase):
    def test_selection_is_derived_from_linked_phase7d1_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cohort, quality, plan, expected = _fixture(Path(raw))
            loaded_plan, selection = load_phase7d2a_pilot_inputs(
                cohort_path=cohort,
                quality_report_path=quality,
                plan_path=plan,
                expected_inventory=4,
                expected_cohort_size=3,
                expected_new_sources=2,
            )
        self.assertEqual(selection.source_ids, expected)
        self.assertEqual(selection.full_cohort_source_count, 3)
        self.assertEqual(loaded_plan.selected_source_count, 3)

    def test_tampered_quality_report_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cohort, quality, plan, _ = _fixture(Path(raw))
            payload = json.loads(quality.read_text(encoding="utf-8"))
            payload["counts"]["newly_production_ready"] = 1
            _write(quality, payload)
            with self.assertRaisesRegex(ProductionPilotError, "checksum"):
                load_phase7d2a_pilot_inputs(
                    cohort_path=cohort,
                    quality_report_path=quality,
                    plan_path=plan,
                    expected_inventory=4,
                    expected_cohort_size=3,
                    expected_new_sources=2,
                )

    def test_plan_source_definition_must_match_signed_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cohort, quality, plan, _ = _fixture(Path(raw))
            payload = json.loads(plan.read_text(encoding="utf-8"))
            payload["sources"][1]["listing_url"] = "https://attacker.example/jobs"
            _write(plan, payload)
            with self.assertRaisesRegex(ProductionPilotError, "source definitions"):
                load_phase7d2a_pilot_inputs(
                    cohort_path=cohort,
                    quality_report_path=quality,
                    plan_path=plan,
                    expected_inventory=4,
                    expected_cohort_size=3,
                    expected_new_sources=2,
                )

    def test_unsafe_plan_concurrency_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cohort, quality, plan, _ = _fixture(Path(raw))
            payload = json.loads(plan.read_text(encoding="utf-8"))
            payload["controls"]["max_source_concurrency"] = 2
            _write(plan, payload)
            with self.assertRaisesRegex(ProductionPilotError, "max_source_concurrency"):
                load_phase7d2a_pilot_inputs(
                    cohort_path=cohort,
                    quality_report_path=quality,
                    plan_path=plan,
                    expected_inventory=4,
                    expected_cohort_size=3,
                    expected_new_sources=2,
                )

    def test_report_passes_only_when_both_sources_are_clean(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cohort, quality, plan, _ = _fixture(Path(raw))
            _, selection = load_phase7d2a_pilot_inputs(
                cohort_path=cohort,
                quality_report_path=quality,
                plan_path=plan,
                expected_inventory=4,
                expected_cohort_size=3,
                expected_new_sources=2,
            )
        report = build_phase7d2a_pilot_report(
            selection=selection,
            manifest=_successful_manifest(selection),
            database_write_guard_enabled=True,
        )
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["ready_for_phase7d2b"])
        self.assertEqual(report["blockers"], [])

    def test_quarantine_or_source_failure_blocks_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cohort, quality, plan, _ = _fixture(Path(raw))
            _, selection = load_phase7d2a_pilot_inputs(
                cohort_path=cohort,
                quality_report_path=quality,
                plan_path=plan,
                expected_inventory=4,
                expected_cohort_size=3,
                expected_new_sources=2,
            )
        manifest = _successful_manifest(selection)
        manifest["successful_source_count"] = 1
        manifest["failed_source_count"] = 1
        manifest["quarantined_job_count"] = 1
        manifest["source_results"][1]["status"] = "failed"
        manifest["source_results"][1]["accepted_count"] = 0
        manifest["source_results"][1]["quarantined_count"] = 1
        report = build_phase7d2a_pilot_report(
            selection=selection,
            manifest=manifest,
            database_write_guard_enabled=True,
        )
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["ready_for_phase7d2b"])
        self.assertIn("pilot_quarantined_jobs", report["blockers"])

    def test_database_proxy_allows_preview_reads_and_blocks_writes(self) -> None:
        database = _FakeDatabase()
        proxy = ReadOnlyDatabaseProxy(database)
        self.assertEqual(proxy["jobs_current"].find_one({"job_id": "1"})["query"], {"job_id": "1"})
        with self.assertRaisesRegex(ProductionPilotError, "DATABASE_WRITE_BLOCKED"):
            proxy["jobs_current"].update_one({}, {"$set": {"title": "changed"}})
        with self.assertRaisesRegex(ProductionPilotError, "DATABASE_WRITE_BLOCKED"):
            proxy.get_collection("jobs_current").create_index("job_id")
        with self.assertRaisesRegex(ProductionPilotError, "DATABASE_WRITE_BLOCKED"):
            proxy["jobs_current"].with_options()
        with self.assertRaisesRegex(ProductionPilotError, "DATABASE_OPERATION_BLOCKED"):
            proxy.command("ping")
        self.assertEqual(database.collection.write_calls, 0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from src.portals.certification import read_portal_inventory
from src.portals.job_evidence import job_title_rejection_reason
from src.portals.production_ingestion import (
    Phase6AIngestionConfig,
    ProductionIngestionError,
    build_phase6a_ingestion_plan,
    load_phase6a_production_cohort,
)
from src.portals.production_rollout import (
    ProductionRolloutError,
    assess_production_record,
    build_phase7d1_production_rollout,
    validate_phase7d1_production_rollout,
    write_phase7d1_production_rollout,
)
from src.warehouse.url_utils import canonical_job_url


def _canonical_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _job(title: str, url: str) -> dict:
    return {
        "title": title,
        "job_url": url,
        "apply_url": url,
        "company": None,
        "location_text": "Remote",
        "employment_type": "Full time",
        "duration": None,
        "compensation_text": None,
        "posted_date": None,
        "summary": (
            "Job description: lead delivery responsibilities, meet technical "
            "requirements, collaborate with stakeholders, and improve reliable services."
        ),
        "responsibilities": [],
        "required_skills": [],
        "preferred_skills": [],
        "job_reference": url.rsplit("-", 1)[-1],
    }


def _record(
    source_id: str,
    listing_url: str,
    *,
    status: str,
    jobs: list[dict],
    run_id: str,
) -> dict:
    return {
        "contract_version": "1.4",
        "run_id": run_id,
        "source_id": source_id,
        "display_name": source_id,
        "provided_url": listing_url,
        "effective_listing_url": listing_url,
        "status": status,
        "certification_status": "passed" if status == "success" else "needs_repair",
        "detected_platform": "custom_listing",
        "resolved_route_url": None,
        "extracted_jobs": len(jobs),
        "sample_jobs": jobs,
    }


def _summary(path: Path, inventory_path: Path, records: list[dict], run_id: str) -> Path:
    status_counts = dict(Counter(str(record["status"]) for record in records))
    payload = {
        "contract_version": "1.4",
        "run_id": run_id,
        "input_sha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
        "inventory_count": 4,
        "latest_result_count": len(records),
        "status_counts": status_counts,
        "options": {"max_jobs": 10},
        "records": records,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fixture(directory: Path) -> dict:
    inventory_path = directory / "portals.csv"
    inventory_path.write_text(
        "Alpha,https://alpha.example/openings\n"
        "Beta,https://beta.example/vacancies\n"
        "Gamma,https://gamma.example/roles\n"
        "Delta,https://delta.example/careers\n",
        encoding="utf-8",
    )
    entries = read_portal_inventory(inventory_path)
    alpha, beta, gamma, delta = entries
    baseline_records = [
        _record(
            alpha.source_id,
            alpha.listing_url,
            status="success",
            jobs=[_job("Platform Engineer", "https://alpha.example/openings/platform-engineer-1001")],
            run_id="baseline-run",
        ),
        _record(
            beta.source_id,
            beta.listing_url,
            status="success",
            jobs=[_job("FIND WORK", "https://beta.example/vacancies/data-analyst-2002")],
            run_id="baseline-run",
        ),
        _record(
            gamma.source_id,
            gamma.listing_url,
            status="partial",
            jobs=[_job("Support Specialist", "https://gamma.example/roles/support-specialist-3003")],
            run_id="baseline-run",
        ),
        _record(
            delta.source_id,
            delta.listing_url,
            status="failed",
            jobs=[],
            run_id="baseline-run",
        ),
    ]
    repaired_beta = _record(
        beta.source_id,
        beta.listing_url,
        status="success",
        jobs=[_job("Data Analyst", "https://beta.example/vacancies/data-analyst-2002")],
        run_id="repair-run",
    )
    baseline_path = _summary(
        directory / "baseline.json",
        inventory_path,
        baseline_records,
        "baseline-run",
    )
    repair_path = _summary(
        directory / "repair.json",
        inventory_path,
        [repaired_beta],
        "repair-run",
    )
    manifest_path = directory / "repair_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "cohort_id": "bounded-repair",
                "baseline_production_ready": 1,
                "maximum_attempts": 1,
                "groups": {"false_success_recheck": [beta.source_id]},
            }
        ),
        encoding="utf-8",
    )
    return {
        "inventory": inventory_path,
        "baseline": baseline_path,
        "repair": repair_path,
        "manifest": manifest_path,
        "ids": [entry.source_id for entry in entries],
    }


class Phase7D1ProductionRolloutTests(unittest.TestCase):
    def test_job_hash_routes_remain_distinct_but_page_anchors_are_removed(self) -> None:
        first = canonical_job_url("https://portal.example/#/jobs/26240")
        second = canonical_job_url("https://portal.example/#/jobs/26312")
        self.assertNotEqual(first, second)
        self.assertEqual(first, "https://portal.example/#/jobs/26240")
        self.assertEqual(
            canonical_job_url("https://portal.example/jobs/26240?utm_source=x#overview"),
            "https://portal.example/jobs/26240",
        )

    def test_generic_calls_to_action_are_not_job_titles(self) -> None:
        for title in (
            "FIND WORK",
            "Find a Job",
            "Find Jobs",
            "Browse Jobs",
            "Browse Openings",
            "View Openings",
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    job_title_rejection_reason(title),
                    "generic_navigation_title",
                )

    def test_success_with_one_cta_sample_is_deferred(self) -> None:
        record = {
            "source_id": "source",
            "status": "success",
            "extracted_jobs": 2,
            "effective_listing_url": "https://example.test/jobs",
            "sample_jobs": [
                _job("Platform Engineer", "https://example.test/jobs/platform-engineer-1001"),
                _job("FIND WORK", "https://example.test/jobs/data-analyst-1002"),
            ],
        }
        assessment = assess_production_record(record, evidence_origin="repair")
        self.assertFalse(assessment["production_ready"])
        self.assertEqual(assessment["valid_sample_job_count"], 1)
        self.assertIn("one_or_more_sample_jobs_failed_quality", assessment["rejection_reasons"])

    def test_offline_overlay_freezes_only_truthful_successes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            fixture = _fixture(directory)
            cohort, report = build_phase7d1_production_rollout(
                input_path=fixture["inventory"],
                baseline_summary_path=fixture["baseline"],
                repair_manifest_path=fixture["manifest"],
                repair_summary_path=fixture["repair"],
                expected_inventory=4,
                expected_baseline_ready=1,
                expected_repair_records=1,
                expected_final_ready=2,
                frozen_at="2026-07-21T00:00:00+00:00",
            )
            self.assertEqual(cohort["cohort_source_ids"], fixture["ids"][:2])
            self.assertEqual(cohort["deferred_source_ids"], fixture["ids"][2:])
            self.assertEqual(report["counts"]["newly_production_ready"], 1)
            partial = next(
                record for record in report["records"] if record["source_id"] == fixture["ids"][2]
            )
            self.assertFalse(partial["production_ready"])
            self.assertIn("status_partial_not_production_eligible", partial["rejection_reasons"])

            artifacts = write_phase7d1_production_rollout(
                output_dir=directory / "out",
                cohort=cohort,
                quality_report=report,
            )
            validated, _ = validate_phase7d1_production_rollout(
                cohort_path=Path(artifacts["cohort"]),
                quality_report_path=Path(artifacts["quality_report"]),
                input_path=fixture["inventory"],
                baseline_summary_path=fixture["baseline"],
                repair_manifest_path=fixture["manifest"],
                repair_summary_path=fixture["repair"],
                expected_inventory=4,
                expected_baseline_ready=1,
                expected_repair_records=1,
                expected_final_ready=2,
            )
            loaded = load_phase6a_production_cohort(
                Path(artifacts["cohort"]),
                expected_cohort_size=2,
            )
            self.assertEqual(validated["cohort_sha256"], loaded.cohort_sha256)
            plan = build_phase6a_ingestion_plan(
                cohort_path=Path(artifacts["cohort"]),
                config=Phase6AIngestionConfig(
                    expected_cohort_size=2,
                    requested_source_ids=[fixture["ids"][1]],
                ),
            )
            self.assertEqual(plan.selected_source_ids, [fixture["ids"][1]])
            self.assertFalse(plan.controls["lifecycle_reconciliation_enabled"])

    def test_phase7d_cohort_requires_the_new_safety_attestations(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            fixture = _fixture(directory)
            cohort, report = build_phase7d1_production_rollout(
                input_path=fixture["inventory"],
                baseline_summary_path=fixture["baseline"],
                repair_manifest_path=fixture["manifest"],
                repair_summary_path=fixture["repair"],
                expected_inventory=4,
                expected_baseline_ready=1,
                expected_repair_records=1,
                expected_final_ready=2,
            )
            cohort["safety"].pop("partial_sources_excluded")
            cohort.pop("cohort_sha256")
            cohort["cohort_sha256"] = _canonical_sha256(cohort)
            path = directory / "unsafe.json"
            path.write_text(json.dumps(cohort), encoding="utf-8")
            with self.assertRaisesRegex(ProductionIngestionError, "partial_sources_excluded"):
                load_phase6a_production_cohort(path, expected_cohort_size=2)
            self.assertEqual(report["counts"]["final_production_ready"], 2)

    def test_repair_summary_must_cover_the_manifest_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            fixture = _fixture(directory)
            repair = json.loads(fixture["repair"].read_text(encoding="utf-8"))
            repair["records"] = []
            repair["latest_result_count"] = 0
            repair["status_counts"] = {}
            fixture["repair"].write_text(json.dumps(repair), encoding="utf-8")
            with self.assertRaisesRegex(ProductionRolloutError, "source coverage"):
                build_phase7d1_production_rollout(
                    input_path=fixture["inventory"],
                    baseline_summary_path=fixture["baseline"],
                    repair_manifest_path=fixture["manifest"],
                    repair_summary_path=fixture["repair"],
                    expected_inventory=4,
                    expected_baseline_ready=1,
                    expected_repair_records=1,
                    expected_final_ready=2,
                )


if __name__ == "__main__":
    unittest.main()

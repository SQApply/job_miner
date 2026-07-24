from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.portals.production_cohort23 import (
    ProductionCohort23Error,
    build_phase7d4a_artifacts,
    load_phase7d4a_policy,
    read_phase7d4a_cohort,
    read_phase7d4a_rollout,
    validate_phase7d3c1_final_report,
    write_phase7d4a_artifacts,
)
from src.portals.production_target25 import (
    build_target25_plan,
    evaluate_target25_promotion,
)
from src.portals.production_target25_repair import build_final_repair_report
from specs.test_phase7d3c_target25 import NOW, _fixtures


ROOT = Path(__file__).resolve().parents[1]


def _canonical_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _phase7d4a_fixtures():
    tiers, target25_policy, inventory, production_plan = _fixtures()
    plan = build_target25_plan(
        inventory=inventory,
        production_plan=production_plan,
        tiers=tiers,
        checkpoint_evidence={
            "checkpoint_count": 7,
            "rollout_id": "phase7d4a-test-rollout",
        },
        policy=target25_policy,
        generated_at=NOW,
    )
    candidate_ids = list(plan["promotion_candidate_source_ids"])
    baseline_records = []
    for index, source_id in enumerate(candidate_ids):
        if index < 2:
            status = "success"
            catalog_complete = True
            extracted = 10
            certification_status = "source_exhausted"
            error_type = None
        elif index < 5:
            status = "partial"
            catalog_complete = False
            extracted = 8
            certification_status = "catalog_incomplete"
            error_type = "catalog_incomplete"
        else:
            status = "failed"
            catalog_complete = False
            extracted = 0
            certification_status = "needs_repair"
            error_type = "zero_discovery"
        baseline_records.append(
            {
                "source_id": source_id,
                "status": status,
                "certification_status": certification_status,
                "discovered_urls": extracted,
                "attempted_urls": extracted,
                "extracted_jobs": extracted,
                "catalog_complete": catalog_complete,
                "error_type": error_type,
                "error_message": None,
            }
        )
    prior = evaluate_target25_promotion(
        plan=plan,
        certification_summary={"records": baseline_records},
        generated_at=NOW,
    )
    repair_ids = list(prior["failed_candidate_source_ids"])
    repair_records = []
    for index, source_id in enumerate(repair_ids):
        productive = index < 2
        repair_records.append(
            {
                "source_id": source_id,
                "status": "partial" if productive else "failed",
                "certification_status": (
                    "catalog_incomplete" if productive else "needs_repair"
                ),
                "discovered_urls": 20 if productive else 0,
                "attempted_urls": 10 if productive else 0,
                "extracted_jobs": 7 if productive else 0,
                "catalog_complete": False,
                "error_type": (
                    "catalog_incomplete" if productive else "zero_discovery"
                ),
                "error_message": None,
            }
        )
    final_report = build_final_repair_report(
        plan=plan,
        prior_report=prior,
        baseline_summary={"records": baseline_records},
        repair_summary={"records": repair_records},
        generated_at=NOW,
    )
    policy = load_phase7d4a_policy(
        ROOT / "configs" / "portal_cohorts" / "phase7d4_23_source_policy.json"
    )
    cohort, rollout = build_phase7d4a_artifacts(
        inventory=inventory,
        target25_plan=plan,
        final_report=final_report,
        policy=policy,
        evidence={
            "inventory_name": "portal_links.xlsx",
            "inventory_sha256": "1" * 64,
            "target25_plan_file_sha256": "2" * 64,
            "final_report_file_sha256": "3" * 64,
            "policy_sha256": "4" * 64,
        },
        generated_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    return inventory, plan, final_report, policy, cohort, rollout


class Phase7D4AProductionCohortTests(unittest.TestCase):
    def test_truthful_cohort_freezes_exactly_23_sources(self) -> None:
        _, _, _, _, cohort, rollout = _phase7d4a_fixtures()
        self.assertEqual(cohort["cohort_source_count"], 23)
        self.assertEqual(cohort["deferred_source_count"], 79)
        self.assertEqual(len(cohort["complete_catalog_source_ids"]), 7)
        self.assertEqual(len(cohort["partial_safe_source_ids"]), 16)
        self.assertEqual(len(cohort["promoted_backfill_source_ids"]), 7)
        self.assertEqual(
            rollout["steady_state_rescrape"]["source_ids"],
            cohort["cohort_source_ids"],
        )

    def test_only_seven_promoted_sources_are_in_initial_backfill(self) -> None:
        _, plan, report, _, cohort, rollout = _phase7d4a_fixtures()
        expected = set(report["complete_candidate_source_ids"]) | set(
            report["partial_safe_candidate_source_ids"]
        )
        self.assertEqual(set(cohort["promoted_backfill_source_ids"]), expected)
        self.assertEqual(
            rollout["initial_backfill"]["source_ids"],
            cohort["promoted_backfill_source_ids"],
        )
        self.assertEqual(
            [
                batch["source_count"]
                for batch in rollout["initial_backfill"]["batches"]
            ],
            [4, 3],
        )
        self.assertTrue(
            set(plan["current_recurring_source_ids"]).isdisjoint(expected)
        )

    def test_all_23_sources_are_scheduled_every_72_hours(self) -> None:
        _, _, _, _, cohort, rollout = _phase7d4a_fixtures()
        recurring = rollout["steady_state_rescrape"]
        self.assertEqual(recurring["cadence_hours"], 72)
        self.assertEqual(recurring["source_count"], 23)
        self.assertEqual(
            [batch["source_count"] for batch in recurring["batches"]],
            [4, 4, 4, 4, 4, 3],
        )
        flattened = [
            source_id
            for batch in recurring["batches"]
            for source_id in batch["source_ids"]
        ]
        self.assertEqual(flattened, cohort["cohort_source_ids"])

    def test_failed_candidates_are_excluded_from_every_execution_set(self) -> None:
        _, _, report, _, cohort, rollout = _phase7d4a_fixtures()
        failed = set(report["failed_candidate_source_ids"])
        self.assertEqual(failed, set(cohort["failed_candidate_source_ids"]))
        self.assertTrue(failed.isdisjoint(cohort["cohort_source_ids"]))
        self.assertTrue(
            failed.isdisjoint(rollout["initial_backfill"]["source_ids"])
        )
        self.assertTrue(
            failed.isdisjoint(rollout["steady_state_rescrape"]["source_ids"])
        )

    def test_partial_safe_sources_cannot_reconcile_or_deactivate(self) -> None:
        _, _, _, _, cohort, rollout = _phase7d4a_fixtures()
        partial_ids = set(cohort["partial_safe_source_ids"])
        for source in cohort["sources"]:
            if source["source_id"] not in partial_ids:
                continue
            self.assertFalse(
                source["lifecycle"]["missing_reconciliation_enabled"]
            )
            self.assertFalse(source["lifecycle"]["deactivation_enabled"])
            self.assertFalse(
                source["lifecycle"][
                    "incomplete_cycles_may_increment_missing_count"
                ]
            )
        self.assertFalse(
            rollout["controls"]["partial_safe_missing_reconciliation_enabled"]
        )
        self.assertFalse(rollout["controls"]["deactivation_enabled"])

    def test_report_tampering_is_rejected(self) -> None:
        _, _, report, _, _, _ = _phase7d4a_fixtures()
        tampered = dict(report)
        tampered["counts"] = dict(report["counts"])
        tampered["counts"]["final_recurring_usable"] = 25
        with self.assertRaisesRegex(
            ProductionCohort23Error,
            "checksum",
        ):
            validate_phase7d3c1_final_report(tampered)

    def test_artifact_write_is_immutable_and_idempotent(self) -> None:
        _, _, _, _, cohort, rollout = _phase7d4a_fixtures()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cohort_path = root / "configs" / "cohort.json"
            rollout_path = root / "data" / "rollout.json"
            paths, created = write_phase7d4a_artifacts(
                cohort_path=cohort_path,
                rollout_path=rollout_path,
                cohort=cohort,
                rollout=rollout,
            )
            repeated_paths, created_again = write_phase7d4a_artifacts(
                cohort_path=cohort_path,
                rollout_path=rollout_path,
                cohort=cohort,
                rollout=rollout,
            )
            loaded_cohort = read_phase7d4a_cohort(cohort_path)
            loaded_rollout = read_phase7d4a_rollout(
                rollout_path,
                cohort=loaded_cohort,
            )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(paths, repeated_paths)
        self.assertEqual(loaded_cohort, cohort)
        self.assertEqual(loaded_rollout, rollout)

    def test_different_existing_frozen_artifact_is_not_overwritten(self) -> None:
        _, _, _, _, cohort, rollout = _phase7d4a_fixtures()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cohort_path = root / "configs" / "cohort.json"
            rollout_path = root / "data" / "rollout.json"
            write_phase7d4a_artifacts(
                cohort_path=cohort_path,
                rollout_path=rollout_path,
                cohort=cohort,
                rollout=rollout,
            )
            altered = dict(cohort)
            altered["cohort_sha256"] = _canonical_sha256(
                {key: value for key, value in altered.items() if key != "cohort_sha256"}
            )
            altered["counts"] = dict(cohort["counts"])
            altered["counts"]["deferred"] = 78
            altered["cohort_sha256"] = _canonical_sha256(
                {key: value for key, value in altered.items() if key != "cohort_sha256"}
            )
            with self.assertRaises(ProductionCohort23Error):
                write_phase7d4a_artifacts(
                    cohort_path=cohort_path,
                    rollout_path=rollout_path,
                    cohort=altered,
                    rollout=rollout,
                )

    def test_partial_artifact_set_is_rejected_before_missing_files_are_written(
        self,
    ) -> None:
        _, _, _, _, cohort, rollout = _phase7d4a_fixtures()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cohort_path = root / "configs" / "cohort.json"
            rollout_path = root / "data" / "rollout.json"
            cohort_path.parent.mkdir(parents=True)
            cohort_path.write_text(
                json.dumps(cohort, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ProductionCohort23Error,
                "partially present",
            ):
                write_phase7d4a_artifacts(
                    cohort_path=cohort_path,
                    rollout_path=rollout_path,
                    cohort=cohort,
                    rollout=rollout,
                )
            self.assertFalse(rollout_path.exists())


if __name__ == "__main__":
    unittest.main()

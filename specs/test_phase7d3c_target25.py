from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.portals.certification import PortalInventoryEntry
from src.portals.production_ingestion import Phase6AIngestionPlan, Phase6ASource
from src.portals.production_target25 import (
    ProductionTarget25Error,
    build_target25_plan,
    classify_phase7d3b_source_results,
    evaluate_target25_promotion,
    load_target25_policy,
    read_target25_plan,
    write_target25_json,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


def _source_id(index: int) -> str:
    return f"cert_example_{index:03d}"


def _result(index: int, *, accepted: int, complete: bool) -> dict:
    return {
        "source_id": _source_id(index),
        "display_name": f"Portal {index}",
        "status": "success" if complete else "failed",
        "certification_status": "source_exhausted" if complete else "catalog_incomplete",
        "discovered_count": max(accepted, 1),
        "attempted_count": max(accepted, 1),
        "extracted_count": accepted,
        "accepted_count": accepted,
        "quarantined_count": 0,
        "catalog_complete": complete,
        "error_type": None if complete else "catalog_incomplete",
        "error_message": None,
    }


def _fixtures():
    current_results = [
        *[_result(index, accepted=2, complete=True) for index in range(1, 6)],
        *[_result(index, accepted=2, complete=False) for index in range(6, 17)],
        *[_result(index, accepted=0, complete=False) for index in range(17, 27)],
    ]
    tiers = classify_phase7d3b_source_results(current_results)
    policy = load_target25_policy(
        ROOT / "configs" / "portal_cohorts" / "phase7d3c_target25_policy.json"
    )
    candidate_ids = [
        candidate["source_id"] for candidate in policy["promotion_candidates"]
    ]
    inventory_ids = [_source_id(index) for index in range(1, 27)] + candidate_ids
    inventory_ids += [
        f"cert_inventory_padding_{index:03d}"
        for index in range(1, 102 - len(inventory_ids) + 1)
    ]
    inventory = [
        PortalInventoryEntry(
            source_id=source_id,
            display_name=f"Portal {index}",
            listing_url=f"https://portal-{index}.example/jobs",
            source_row=index,
        )
        for index, source_id in enumerate(inventory_ids, start=1)
    ]
    production_sources = [
        Phase6ASource(
            source_id=_source_id(index),
            source_row=index,
            display_name=f"Portal {index}",
            listing_url=f"https://portal-{index}.example/jobs",
        )
        for index in range(1, 27)
    ]
    production_plan = Phase6AIngestionPlan(
        plan_id="phase7d3c-test-production-plan",
        generated_at=NOW,
        cohort_sha256="a" * 64,
        cohort_source_count=26,
        selected_source_count=26,
        deferred_source_count=76,
        selected_source_ids=[source.source_id for source in production_sources],
        sources=production_sources,
        controls={
            "execution_mode": "plan_only",
            "production_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
        },
    )
    return tiers, policy, inventory, production_plan


class Phase7D3CTarget25Tests(unittest.TestCase):
    def test_phase7d3b_results_classify_truthfully(self) -> None:
        tiers, _, _, _ = _fixtures()
        self.assertEqual(len(tiers["complete_catalog"]), 5)
        self.assertEqual(len(tiers["partial_safe"]), 11)
        self.assertEqual(len(tiers["nonproductive"]), 10)

    def test_target_plan_contains_16_current_and_9_candidates(self) -> None:
        tiers, policy, inventory, production_plan = _fixtures()
        plan = build_target25_plan(
            inventory=inventory,
            production_plan=production_plan,
            tiers=tiers,
            checkpoint_evidence={
                "checkpoint_count": 7,
                "rollout_id": "rollout",
                "accepted_jobs": 911,
            },
            policy=policy,
            generated_at=NOW,
        )
        self.assertEqual(plan["current_counts"]["complete_catalog"], 5)
        self.assertEqual(plan["current_counts"]["partial_safe"], 11)
        self.assertEqual(len(plan["current_recurring_source_ids"]), 16)
        self.assertEqual(len(plan["promotion_candidate_source_ids"]), 9)
        self.assertEqual(len(plan["target_source_ids"]), 25)
        self.assertFalse(plan["target_achieved"])
        self.assertFalse(
            plan["controls"]["incomplete_cycles_may_reconcile_missing_jobs"]
        )
        self.assertFalse(
            plan["controls"]["incomplete_cycles_may_deactivate_jobs"]
        )

        with TemporaryDirectory() as directory:
            target = Path(directory) / "plan.json"
            write_target25_json(target, plan, checksum_field="plan_sha256")
            self.assertEqual(read_target25_plan(target)["plan_id"], plan["plan_id"])

    def test_promotion_report_requires_all_nine_candidates_for_25(self) -> None:
        tiers, policy, inventory, production_plan = _fixtures()
        plan = build_target25_plan(
            inventory=inventory,
            production_plan=production_plan,
            tiers=tiers,
            checkpoint_evidence={"checkpoint_count": 7, "rollout_id": "rollout"},
            policy=policy,
            generated_at=NOW,
        )
        records = []
        for index, source_id in enumerate(plan["promotion_candidate_source_ids"]):
            records.append(
                {
                    "source_id": source_id,
                    "status": "success" if index < 4 else "partial",
                    "certification_status": (
                        "source_exhausted" if index < 4 else "catalog_incomplete"
                    ),
                    "discovered_urls": 10,
                    "attempted_urls": 10,
                    "extracted_jobs": 10,
                    "catalog_complete": index < 4,
                    "error_type": None if index < 4 else "catalog_incomplete",
                    "error_message": None,
                }
            )
        report = evaluate_target25_promotion(
            plan=plan,
            certification_summary={"records": records},
            generated_at=NOW,
        )
        self.assertTrue(report["target_achieved"])
        self.assertEqual(report["counts"]["final_recurring_usable"], 25)
        self.assertEqual(report["counts"]["candidate_complete_catalog"], 4)
        self.assertEqual(report["counts"]["candidate_partial_safe"], 5)
        self.assertFalse(report["controls"]["deactivation_enabled"])

    def test_failed_candidate_keeps_target_unreached(self) -> None:
        tiers, policy, inventory, production_plan = _fixtures()
        plan = build_target25_plan(
            inventory=inventory,
            production_plan=production_plan,
            tiers=tiers,
            checkpoint_evidence={"checkpoint_count": 7, "rollout_id": "rollout"},
            policy=policy,
            generated_at=NOW,
        )
        records = [
            {
                "source_id": source_id,
                "status": "partial",
                "extracted_jobs": 5,
                "catalog_complete": False,
                "error_type": "catalog_incomplete",
            }
            for source_id in plan["promotion_candidate_source_ids"][:-1]
        ]
        report = evaluate_target25_promotion(
            plan=plan,
            certification_summary={"records": records},
            generated_at=NOW,
        )
        self.assertFalse(report["target_achieved"])
        self.assertEqual(report["counts"]["final_recurring_usable"], 24)
        self.assertEqual(report["counts"]["shortfall"], 1)

    def test_promotion_candidates_cannot_overlap_current_cohort(self) -> None:
        tiers, policy, inventory, production_plan = _fixtures()
        policy = dict(policy)
        policy["promotion_candidates"] = [
            *policy["promotion_candidates"][:-1],
            {
                "source_id": _source_id(1),
                "display_name": "Overlap",
                "evidence": "invalid",
            },
        ]
        with self.assertRaisesRegex(ProductionTarget25Error, "overlap"):
            build_target25_plan(
                inventory=inventory,
                production_plan=production_plan,
                tiers=tiers,
                checkpoint_evidence={"checkpoint_count": 7, "rollout_id": "rollout"},
                policy=policy,
                generated_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_cohort_cutover import (
    classify_job_documents,
    load_cutover_policy,
)


class Phase7D4CCohortCutoverSmoke(unittest.TestCase):
    def test_policy_and_classification(self) -> None:
        policy = load_cutover_policy(
            ROOT
            / "configs"
            / "portal_cohorts"
            / "phase7d4c_active22_cutover_policy.json"
        )
        rows = [
            {
                "_id": "cohort",
                "job_id": "job_cohort",
                "target_id": policy["source_ids"][0],
                "is_active": True,
            },
            {
                "_id": "legacy_active",
                "job_id": "job_legacy_active",
                "target_id": "legacy_jobs",
                "is_active": True,
            },
            {
                "_id": "legacy_inactive",
                "job_id": "job_legacy_inactive",
                "target_id": "legacy_jobs",
                "is_active": False,
            },
        ]
        result = classify_job_documents(
            rows,
            cohort_source_ids=policy["source_ids"],
        )
        self.assertEqual(len(policy["source_ids"]), 22)
        self.assertEqual(result["cohort_active"], 1)
        self.assertEqual(result["outside_active"], 1)
        self.assertEqual(result["outside_inactive"], 1)
        self.assertEqual(result["expected_active_after_cutover"], 1)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        Phase7D4CCohortCutoverSmoke
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(
        "PHASE_7D4C_COHORT_CUTOVER_SMOKE_OK",
        json.dumps(
            {
                "cohort_sources": 22,
                "physical_job_deletion": False,
                "scheduler": False,
                "backup_required": True,
                "qdrant_rebuild_required": True,
            },
            sort_keys=True,
        ),
        flush=True,
    )

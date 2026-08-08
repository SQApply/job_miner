from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.portals.production_health import (
    ProductionHealthError,
    build_phase8_1_source_health_report,
)


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


def _complete_checkpoint(
    source_id: str,
    *,
    completed_at: datetime | str,
    discovered: int = 10,
) -> dict:
    return {
        "source_id": source_id,
        "cycle_id": f"cycle-{source_id}",
        "status": "complete",
        "completed_at": completed_at,
        "result": {
            "status": "success",
            "catalog_complete": True,
            "discovery_complete": True,
            "reconciliation_safe": True,
            "discovered_count": discovered,
            "accepted_count": discovered,
            "quarantined_count": 1,
            "inserted_count": 2,
            "updated_count": 3,
            "unchanged_count": 5,
            "elapsed_seconds": 42.5,
        },
        "snapshot": {"reconciliation_safe": True},
        "lifecycle": {"missing_marked": 1, "deactivated": 0},
    }


class Phase81SourceHealthTests(unittest.TestCase):
    def test_complete_source_reports_all_required_health_fields(self) -> None:
        report = build_phase8_1_source_health_report(
            source_ids=["source-a"],
            checkpoint_documents=[
                _complete_checkpoint(
                    "source-a",
                    completed_at=NOW - timedelta(hours=1),
                )
            ],
            cycle_documents=[
                {
                    "cycle_id": "cycle-source-a",
                    "next_due_at": NOW + timedelta(hours=71),
                }
            ],
            job_documents=[
                {
                    "target_id": "source-a",
                    "is_active": True,
                    "missing_complete_run_count": 1,
                },
                {"target_id": "source-a", "is_active": False},
            ],
            now=NOW,
        )
        source = report["sources"][0]
        self.assertEqual(source["health_status"], "healthy")
        self.assertEqual(source["latest_run_counts"]["discovered"], 10)
        self.assertEqual(source["latest_run_counts"]["quarantined"], 1)
        self.assertEqual(source["latest_run_counts"]["missing"], 1)
        self.assertEqual(source["current_jobs"]["active"], 1)
        self.assertEqual(source["current_jobs"]["inactive"], 1)
        self.assertEqual(source["current_jobs"]["missing_candidates"], 1)
        self.assertIsNotNone(source["last_success_at"])
        self.assertIsNotNone(source["last_complete_at"])

    def test_naive_mongo_datetimes_are_normalized_to_utc(self) -> None:
        checkpoint = _complete_checkpoint(
            "source-a",
            completed_at=datetime(2026, 8, 7, 11, 0),
        )
        report = build_phase8_1_source_health_report(
            source_ids=["source-a"],
            checkpoint_documents=[checkpoint],
            cycle_documents=[],
            job_documents=[{"target_id": "source-a", "is_active": True}],
            now=NOW,
        )
        source = report["sources"][0]
        self.assertEqual(source["health_status"], "healthy")
        self.assertTrue(source["last_run_at"].endswith("+00:00"))

    def test_consecutive_failures_stop_at_the_last_success(self) -> None:
        report = build_phase8_1_source_health_report(
            source_ids=["source-a"],
            checkpoint_documents=[
                _complete_checkpoint(
                    "source-a",
                    completed_at=NOW - timedelta(days=2),
                ),
                {
                    "source_id": "source-a",
                    "cycle_id": "failed-1",
                    "status": "failed",
                    "completed_at": NOW - timedelta(hours=2),
                    "result": {"status": "failed"},
                },
                {
                    "source_id": "source-a",
                    "cycle_id": "failed-2",
                    "status": "failed_downstream",
                    "completed_at": NOW - timedelta(hours=1),
                    "error_type": "TypeError",
                    "result": {"status": "success"},
                },
            ],
            cycle_documents=[],
            job_documents=[{"target_id": "source-a", "is_active": True}],
            now=NOW,
        )
        source = report["sources"][0]
        self.assertEqual(source["health_status"], "failed")
        self.assertEqual(source["consecutive_failures"], 2)
        self.assertEqual(source["latest_error_type"], "TypeError")

    def test_overdue_and_never_run_sources_are_explicit(self) -> None:
        report = build_phase8_1_source_health_report(
            source_ids=["source-overdue", "source-never"],
            checkpoint_documents=[
                _complete_checkpoint(
                    "source-overdue",
                    completed_at=NOW - timedelta(hours=80),
                )
            ],
            cycle_documents=[],
            job_documents=[
                {"target_id": "source-overdue", "is_active": True}
            ],
            now=NOW,
        )
        by_id = {row["source_id"]: row for row in report["sources"]}
        self.assertEqual(by_id["source-overdue"]["health_status"], "overdue")
        self.assertTrue(by_id["source-overdue"]["is_due"])
        self.assertEqual(by_id["source-never"]["health_status"], "never_run")
        self.assertTrue(by_id["source-never"]["is_due"])

    def test_report_is_read_only_and_duplicate_sources_are_rejected(self) -> None:
        report = build_phase8_1_source_health_report(
            source_ids=["source-a"],
            checkpoint_documents=[],
            cycle_documents=[],
            job_documents=[],
            now=NOW,
        )
        self.assertTrue(report["controls"]["read_only"])
        self.assertFalse(report["controls"]["mongodb_writes"])
        self.assertFalse(report["controls"]["reconciliation"])
        self.assertFalse(report["controls"]["deactivation"])
        with self.assertRaises(ProductionHealthError):
            build_phase8_1_source_health_report(
                source_ids=["source-a", "source-a"],
                checkpoint_documents=[],
                cycle_documents=[],
                job_documents=[],
                now=NOW,
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_health import build_phase8_1_source_health_report


def main() -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    report = build_phase8_1_source_health_report(
        source_ids=["source-healthy", "source-failed"],
        checkpoint_documents=[
            {
                "source_id": "source-healthy",
                "cycle_id": "cycle-healthy",
                "status": "complete",
                "completed_at": now - timedelta(hours=1),
                "result": {
                    "status": "success",
                    "catalog_complete": True,
                    "discovery_complete": True,
                    "reconciliation_safe": True,
                    "discovered_count": 10,
                    "accepted_count": 10,
                    "elapsed_seconds": 30.0,
                },
                "snapshot": {"reconciliation_safe": True},
                "lifecycle": {"missing_marked": 1, "deactivated": 0},
            },
            {
                "source_id": "source-failed",
                "cycle_id": "cycle-failed",
                "status": "failed",
                "completed_at": now - timedelta(hours=2),
                "error_type": "TimeoutError",
                "error_message": "source timed out",
                "result": {"status": "failed"},
            },
        ],
        cycle_documents=[
            {
                "cycle_id": "cycle-healthy",
                "next_due_at": now + timedelta(hours=71),
            },
            {
                "cycle_id": "cycle-failed",
                "next_due_at": now + timedelta(hours=4),
            },
        ],
        job_documents=[
            {
                "target_id": "source-healthy",
                "job_id": "job-1",
                "is_active": True,
                "missing_complete_run_count": 1,
            }
        ],
        now=now,
    )
    assert report["cohort_source_count"] == 2
    assert report["sources_accounted"] == 2
    assert report["status_counts"] == {"failed": 1, "healthy": 1}
    assert report["controls"]["mongodb_writes"] is False
    assert report["controls"]["automatic_scheduler_enabled"] is False
    print(
        "PHASE_8_1_SOURCE_HEALTH_SMOKE_OK",
        json.dumps(
            {
                "sources": report["cohort_source_count"],
                "status_counts": report["status_counts"],
                "attention": report["attention_source_count"],
                "mongodb_writes": report["controls"]["mongodb_writes"],
                "scheduler": report["controls"][
                    "automatic_scheduler_enabled"
                ],
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

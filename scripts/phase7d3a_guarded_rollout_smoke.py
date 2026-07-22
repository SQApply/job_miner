from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_guarded_rollout import (
    build_phase7d3a_rollout_plan,
    read_phase7d3a_rollout_plan,
    write_phase7d3a_rollout_plan,
)
from src.portals.production_ingestion import Phase6AIngestionPlan, Phase6ASource
from src.portals.production_pilot import Phase7D2PilotSelection


def main() -> None:
    source_ids = [f"cert_smoke_{index:02d}" for index in range(1, 27)]
    sources = [
        Phase6ASource(
            source_id=source_id,
            source_row=index,
            display_name=source_id,
            listing_url=f"https://smoke-{index}.example/jobs",
            detected_platform="custom_listing",
            bounded_extracted_jobs=10,
            evidence_run_id="phase7d1-smoke",
        )
        for index, source_id in enumerate(source_ids, start=1)
    ]
    production_plan = Phase6AIngestionPlan(
        plan_id="phase6a-smoke-plan",
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        cohort_sha256="a" * 64,
        cohort_source_count=26,
        selected_source_count=26,
        deferred_source_count=76,
        selected_source_ids=source_ids,
        sources=sources,
        controls={
            "execution_mode": "plan_only",
            "max_source_concurrency": 1,
            "production_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
        },
    )
    seeded_source_ids = [source_ids[4], source_ids[22]]
    selection = Phase7D2PilotSelection(
        plan_id=production_plan.plan_id,
        cohort_sha256=production_plan.cohort_sha256,
        quality_report_sha256="b" * 64,
        full_cohort_source_count=26,
        deferred_source_count=76,
        source_ids=seeded_source_ids,
        expected_source_count=2,
    )
    semantic_closeout = {
        "generated_from_run_id": "phase7d2c-smoke",
        "source_ids": seeded_source_ids,
        "semantic_idempotency": {
            "accepted": 20,
            "inserted": 0,
            "updated": 2,
            "unchanged": 18,
        },
        "evidence": {
            "phase7d2b_report_sha256": "1" * 64,
            "first_write_manifest_sha256": "2" * 64,
            "phase7d2c_strict_report_sha256": "3" * 64,
            "rerun_manifest_sha256": "4" * 64,
            "phase6f_closeout_sha256": "5" * 64,
        },
    }
    payload = build_phase7d3a_rollout_plan(
        production_plan=production_plan,
        selection=selection,
        semantic_closeout=semantic_closeout,
        semantic_closeout_sha256="c" * 64,
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "rollout.json"
        _, stored, created = write_phase7d3a_rollout_plan(target, payload)
        validated = read_phase7d3a_rollout_plan(target)
        _, repeated, created_again = write_phase7d3a_rollout_plan(target, payload)

    initial_backfill = list(source_ids)
    flattened_backfill = [
        source_id
        for batch in validated["initial_backfill_batches"]
        for source_id in batch["source_ids"]
    ]
    flattened_rescrape = [
        source_id
        for batch in validated["steady_state_rescrape_batches"]
        for source_id in batch["source_ids"]
    ]
    assert created is True
    assert created_again is False
    assert repeated == stored == validated
    assert validated["full_source_count"] == 26
    assert validated["seeded_source_count"] == 2
    assert validated["initial_backfill_source_count"] == 26
    assert validated["initial_backfill_batch_count"] == 7
    assert [
        batch["source_count"] for batch in validated["initial_backfill_batches"]
    ] == [4, 4, 4, 4, 4, 4, 2]
    assert flattened_backfill == initial_backfill
    assert set(seeded_source_ids).issubset(set(flattened_backfill))
    assert validated["steady_state_rescrape_source_count"] == 26
    assert validated["steady_state_rescrape_batch_count"] == 7
    assert [
        batch["source_count"] for batch in validated["steady_state_rescrape_batches"]
    ] == [4, 4, 4, 4, 4, 4, 2]
    assert flattened_rescrape == source_ids
    assert set(seeded_source_ids).issubset(set(flattened_rescrape))
    assert validated["rescrape_policy"]["cadence_hours"] == 72
    assert validated["rescrape_policy"]["include_all_production_sources"] is True
    assert validated["catalog_completion_policy"]["mode"] == "complete_catalog"
    assert validated["catalog_completion_policy"]["max_jobs_per_source"] is None
    assert validated["controls"]["mongodb_writes_performed"] is False
    assert validated["controls"]["deactivation_enabled"] is False
    print(
        "PHASE_7D3A_GUARDED_ROLLOUT_SMOKE_OK",
        json.dumps(
            {
                "full_sources": 26,
                "already_seeded_sources": 2,
                "initial_backfill_sources": 26,
                "initial_backfill_batch_sizes": [4, 4, 4, 4, 4, 4, 2],
                "seeded_sources_in_complete_backfill": True,
                "steady_state_rescrape_sources": 26,
                "steady_state_rescrape_batch_sizes": [4, 4, 4, 4, 4, 4, 2],
                "seeded_sources_in_recurring_rescrape": True,
                "rescrape_cadence_hours": 72,
                "catalog_mode": "complete",
                "max_jobs_per_source": None,
                "source_concurrency": 1,
                "network": False,
                "mongodb_writes": False,
                "reconciliation": False,
                "deactivation": False,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

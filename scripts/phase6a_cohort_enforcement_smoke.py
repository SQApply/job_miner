from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_ingestion import (
    Phase6AIngestionConfig,
    ProductionIngestionError,
    build_phase6a_ingestion_plan,
)


def canonical_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cohort_path = Path(raw) / "cohort.json"
        payload = {
            "contract_version": "1.0",
            "phase": "5.5C",
            "cohort_status": "frozen_certified_cohort",
            "frozen_at": "2026-07-17T00:00:00+00:00",
            "inventory": {"path": "portals.xlsx", "sha256": "a" * 64, "source_count": 3},
            "evidence": {},
            "cohort_source_count": 2,
            "deferred_source_count": 1,
            "cohort_source_ids": ["source_a", "source_b"],
            "deferred_source_ids": ["source_c"],
            "sources": [
                {
                    "source_id": "source_a",
                    "source_row": 1,
                    "display_name": "Source A",
                    "listing_url": "https://a.example/jobs",
                    "detected_platform": "custom_listing",
                    "resolved_route_url": "https://a.example/jobs",
                    "bounded_extracted_jobs": 10,
                    "evidence_run_id": "run-1",
                },
                {
                    "source_id": "source_b",
                    "source_row": 2,
                    "display_name": "Source B",
                    "listing_url": "https://b.example/jobs",
                    "detected_platform": "custom_listing",
                    "resolved_route_url": None,
                    "bounded_extracted_jobs": 9,
                    "evidence_run_id": "run-1",
                },
            ],
            "safety": {
                "bounded_certification_only": True,
                "full_inventory_extraction_validated": False,
                "production_ingestion_enabled": False,
                "lifecycle_reconciliation_enabled": False,
                "failed_or_partial_runs_may_deactivate_jobs": False,
                "phase_6_validation_required": True,
            },
        }
        payload["cohort_sha256"] = canonical_sha256(payload)
        cohort_path.write_text(json.dumps(payload), encoding="utf-8")

        plan = build_phase6a_ingestion_plan(
            cohort_path=cohort_path,
            config=Phase6AIngestionConfig(
                expected_cohort_size=2,
                requested_source_ids=["source_b"],
            ),
            generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )
        rejected = False
        try:
            build_phase6a_ingestion_plan(
                cohort_path=cohort_path,
                config=Phase6AIngestionConfig(
                    expected_cohort_size=2,
                    requested_source_ids=["source_c"],
                ),
            )
        except ProductionIngestionError as exc:
            rejected = "SOURCE_NOT_IN_PRODUCTION_COHORT" in str(exc)

    assert plan.selected_source_ids == ["source_b"]
    assert plan.controls["production_writes_enabled"] is False
    assert plan.controls["lifecycle_reconciliation_enabled"] is False
    assert plan.controls["deactivation_enabled"] is False
    assert rejected
    print(
        "PHASE_6A_COHORT_ENFORCEMENT_SMOKE_OK",
        "selected=1",
        "deferred_rejected=true",
        "writes=false",
        "reconciliation=false",
        "deactivation=false",
        flush=True,
    )


if __name__ == "__main__":
    main()

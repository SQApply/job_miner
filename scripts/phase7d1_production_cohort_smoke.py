from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_ingestion import load_phase6a_production_cohort
from src.portals.production_rollout import assess_production_record


def _canonical_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _job(title: str) -> dict:
    return {
        "title": title,
        "job_url": "https://jobs.example.test/openings/platform-engineer-1001",
        "summary": (
            "Job description: own engineering responsibilities, meet technical "
            "requirements, and deliver reliable services with the product team."
        ),
        "location_text": "Remote",
        "job_reference": "1001",
    }


def main() -> None:
    cta = assess_production_record(
        {
            "source_id": "cta_source",
            "status": "success",
            "extracted_jobs": 1,
            "effective_listing_url": "https://jobs.example.test/openings",
            "sample_jobs": [_job("FIND WORK")],
        },
        evidence_origin="repair",
    )
    partial = assess_production_record(
        {
            "source_id": "partial_source",
            "status": "partial",
            "extracted_jobs": 1,
            "effective_listing_url": "https://jobs.example.test/openings",
            "sample_jobs": [_job("Platform Engineer")],
        },
        evidence_origin="repair",
    )
    payload = {
        "contract_version": "1.1",
        "phase": "7D1",
        "cohort_status": "frozen_truthful_production_cohort",
        "frozen_at": "2026-07-21T00:00:00+00:00",
        "inventory": {"path": "portals.xlsx", "sha256": "a" * 64, "source_count": 2},
        "evidence": {"quality_report_sha256": "b" * 64},
        "cohort_source_count": 1,
        "deferred_source_count": 1,
        "cohort_source_ids": ["verified_source"],
        "deferred_source_ids": ["partial_source"],
        "sources": [
            {
                "source_id": "verified_source",
                "source_row": 1,
                "display_name": "Verified Source",
                "listing_url": "https://jobs.example.test/openings",
                "detected_platform": "custom_listing",
                "resolved_route_url": None,
                "bounded_extracted_jobs": 10,
                "evidence_run_id": "repair-run",
            }
        ],
        "safety": {
            "bounded_certification_only": True,
            "full_inventory_extraction_validated": False,
            "production_ingestion_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "failed_or_partial_runs_may_deactivate_jobs": False,
            "phase_6_validation_required": True,
            "sample_job_evidence_revalidated": True,
            "partial_sources_excluded": True,
            "failed_sources_excluded": True,
            "lifecycle_reconciliation_requires_two_clean_runs": True,
        },
    }
    payload["cohort_sha256"] = _canonical_sha256(payload)
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "cohort.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = load_phase6a_production_cohort(path, expected_cohort_size=1)

    if cta["production_ready"] or partial["production_ready"]:
        raise SystemExit("Phase 7D1 truthfulness smoke failed")
    print(
        "PHASE_7D1_PRODUCTION_COHORT_SMOKE_OK",
        json.dumps(
            {
                "cta_rejected": True,
                "partial_excluded": True,
                "loaded_sources": loaded.cohort_source_count,
                "reconciliation_enabled": False,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

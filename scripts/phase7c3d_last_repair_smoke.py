from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.job_evidence import (
    infer_job_title_from_url,
    job_title_source_rejection_reason,
)
from src.portals.repair_campaign import audit_repair_records, load_repair_cohort


def main() -> None:
    cohort = load_repair_cohort(
        ROOT / "configs/portal_cohorts/phase7c3d_last_repair.json"
    )
    inferred = infer_job_title_from_url(
        "https://careers.example.com/jobs/application-developer-ii-121240/"
    )
    rejection = job_title_source_rejection_reason(
        "Example Staffing",
        job_url="https://examplestaffing.com/jobs/cloud-engineer-121240",
        job_reference="https://examplestaffing.com/#website",
        summary="Recruiting and staffing solutions",
    )
    sample = {
        "source_id": cohort.source_ids[0],
        "status": "success",
        "extracted_jobs": 1,
        "effective_listing_url": "https://careers.example.net/jobs",
        "sample_jobs": [
            {
                "title": "Platform Engineer",
                "job_url": "https://careers.example.net/jobs/platform-engineer-42",
                "summary": (
                    "Job description: build reliable systems, own production services, "
                    "review designs, mentor engineers, and improve delivery automation."
                ),
            }
        ],
    }
    audit = audit_repair_records([sample], cohort)
    assert len(cohort.source_ids) == 15
    assert inferred == "application developer ii"
    assert rejection == "website_schema_title"
    assert audit["evaluated"] == 1
    assert audit["stop_rule"] == "incomplete_run_only_missing_sources"
    print(
        "PHASE_7C3D_LAST_REPAIR_SMOKE_OK",
        json.dumps(
            {
                "cohort": len(cohort.source_ids),
                "inferred_title": inferred,
                "site_title_rejection": rejection,
                "selected_for_live_test": 15,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

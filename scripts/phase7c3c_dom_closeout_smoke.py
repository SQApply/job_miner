from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.crawl.browser_evidence import BrowserEvidenceReport, NetworkJsonEvidence
from src.crawl.dom_snapshot import FrameDomSnapshot
from src.portals.job_evidence import job_title_context_rejection_reason
from src.portals.json_discovery import JsonCandidateDiscoverer, JsonDiscoveryOptions
from src.portals.orchestrator import deduplicate_extracted_jobs
from src.portals.route_resolver import resolve_listing_route
from src.schemas import JobPosting


def main() -> None:
    taxonomy_reason = job_title_context_rejection_reason(
        "Accounting / Finance",
        "AP/AR Specialist Related Accounting / Finance Jobs",
    )
    if taxonomy_reason != "related_jobs_taxonomy_title":
        raise SystemExit("taxonomy relationship was not rejected")

    payload = {
        "jobs": [
            {
                "jobTitle": "Platform Engineer",
                "jobId": "REQ-9",
                "location": "Remote",
                "company": "Example Engineering",
                "description": (
                    "Job description: build reliable platform services, automate releases, "
                    "improve observability, and collaborate with application engineering."
                ),
            }
        ]
    }
    now = datetime.now(timezone.utc).isoformat()
    report = BrowserEvidenceReport(
        requested_url="https://careers.example.com/jobs",
        final_url="https://careers.example.com/jobs",
        success=True,
        status_code=200,
        started_at=now,
        completed_at=now,
        frames=[
            FrameDomSnapshot(
                frame_id="f0",
                frame_url="https://careers.example.com/jobs",
            )
        ],
        network_json=[
            NetworkJsonEvidence(
                response_id="r0001",
                url="https://careers.example.com/api/opening/REQ-9",
                status=200,
                resource_type="fetch",
                content_type="application/json",
                captured_body_bytes=len(json.dumps(payload).encode("utf-8")),
                body_sha256="a" * 64,
                payload=payload,
            )
        ],
    )
    listing_candidates = JsonCandidateDiscoverer().discover(report).candidates
    detail_candidates = JsonCandidateDiscoverer(
        JsonDiscoveryOptions(allow_url_less_records=True)
    ).discover(report).candidates
    if listing_candidates or len(detail_candidates) != 1:
        raise SystemExit("URL-less detail JSON escaped its bounded extraction mode")

    route = resolve_listing_route(
        source_url="https://careers-example.icims.com/jobs/search?in_iframe=1",
        html=(
            '<a href="https://careers-example.icims.com/">Join our team</a>'
            '<a href="https://careers-example.icims.com/example/categories">Categories</a>'
        ),
    )
    if route.selected is not None:
        raise SystemExit("strong ATS search route was replaced by weaker navigation")

    jobs, duplicates = deduplicate_extracted_jobs(
        [
            JobPosting(
                title="Platform Engineer",
                job_url="https://careers.example.com/jobs/42?utm_source=list",
            ),
            JobPosting(
                title="Senior Platform Engineer",
                job_url="https://careers.example.com/jobs/42",
                location_text="Remote",
                summary="Build reliable cloud platform services.",
            ),
        ]
    )
    if len(jobs) != 1 or len(duplicates) != 1:
        raise SystemExit("canonical final-detail deduplication failed")

    print(
        "PHASE_7C3C_DOM_CLOSEOUT_SMOKE_OK",
        json.dumps(
            {
                "taxonomy_rejection": taxonomy_reason,
                "url_less_detail_records": len(detail_candidates),
                "strong_route_retained": route.selected is None,
                "duplicates_collapsed": len(duplicates),
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

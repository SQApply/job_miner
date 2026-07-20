from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.crawl.browser_evidence import BrowserEvidenceReport, NetworkJsonEvidence
from src.portals.contracts import DiscoveryCandidate
from src.portals.dom_discovery import preserve_evidence_backed_urls
from src.portals.json_discovery import JsonCandidateDiscoverer
from src.portals.live_interaction import LiveLinklessResolver


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    report = BrowserEvidenceReport(
        requested_url="https://careers.example.com/careers",
        final_url="https://careers.example.com/careers",
        success=True,
        status_code=200,
        title="Careers",
        started_at=now,
        completed_at=now,
        network_json=[
            NetworkJsonEvidence(
                response_id="r0001",
                url="https://careers.example.com/api/graphql",
                status=200,
                resource_type="xhr",
                content_type="application/json",
                payload={
                    "data": {
                        "jobs": [
                            {
                                "positionTitle": "Reliability Engineer",
                                "detailUri": "/opening?opaque=phase7c2",
                                "requisitionNumber": "REQ-C2",
                                "location": "Remote",
                            }
                        ]
                    }
                },
            )
        ],
    )
    structured = JsonCandidateDiscoverer().discover(report)
    assert len(structured.candidates) == 1
    preserved, preservation_metrics = preserve_evidence_backed_urls(
        [],
        structured.candidates,
    )
    assert preserved == ["https://careers.example.com/opening?opaque=phase7c2"]

    navigation = DiscoveryCandidate.from_url(
        "https://careers.example.com/employers/salary-guide",
        confidence=0.99,
        evidence={
            "origin": "adaptive_dom_individual_link",
            "evidence_preserving": True,
        },
    )
    rejected, _ = preserve_evidence_backed_urls([], [navigation])
    assert rejected == []
    assert LiveLinklessResolver._is_openable_detail_route(
        "https://careers.example.com/jobs",
        "https://careers.example.com/jobs#role/REQ-C2",
        source_job_id="REQ-C2",
    )
    assert not LiveLinklessResolver._is_openable_detail_route(
        "https://careers.example.com/jobs",
        "https://careers.example.com/jobs?page=2",
        source_job_id=None,
    )

    print(
        "PHASE_7C2_ADAPTIVE_EVIDENCE_SMOKE_OK",
        json.dumps(
            {
                "json_candidates": len(structured.candidates),
                "preextracted_jobs": structured.metrics["preextracted_jobs"],
                "preserved_structured_urls": preservation_metrics[
                    "adaptive_urls_preserved"
                ],
                "individual_navigation_preserved": len(rejected),
                "linkless_route_guard": "ok",
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

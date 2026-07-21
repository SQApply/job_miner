from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.acquisition import AcquisitionOutcome
from src.portals.certification import (
    _empty_acquisition_listing_handoff,
    _owned_sibling_hosts,
)
from src.portals.contracts import DiscoveryCandidate
from src.portals.dom_discovery import preserve_evidence_backed_urls
from src.portals.job_evidence import job_title_rejection_reason
from src.portals.url_intelligence import assess_job_candidate_url


def main() -> None:
    listing = "http://careerportal.compri.com/#/jobs"
    detail = "http://careerportal.compri.com/#/jobs/26431"
    assessment = assess_job_candidate_url(detail, listing_url=listing)
    assert assessment.score >= 8
    assert job_title_rejection_reason("Added - 07/17/26") == "date_label_title"
    assert job_title_rejection_reason("Must-Haves") == "section_heading_title"

    marketing_url = "https://www.randstadusa.com/employers/operational"
    marketing = DiscoveryCandidate.from_url(
        marketing_url,
        confidence=0.96,
        evidence={
            "origin": "adaptive_dom_repeated_cluster",
            "evidence_preserving": True,
            "structural_job_grounding": True,
        },
    )
    preserved, preservation = preserve_evidence_backed_urls(
        [],
        [marketing],
        ranking_metrics={
            "rejected_candidates": [
                {
                    "url": marketing_url,
                    "hard_reject": False,
                    "reasons": ["navigation_tokens:employers"],
                }
            ]
        },
    )
    assert preserved == []

    outcome = AcquisitionOutcome(
        selected=None,
        attempts=[
            {
                "platform": "public_html",
                "status": "empty",
                "trusted_hosts": ["www.belcan.com"],
                "metadata": {"listing_url": "https://www.belcan.com/employment/"},
            }
        ],
    )
    with patch("src.portals.safety._validate_host_is_public", return_value=None):
        handoff = _empty_acquisition_listing_handoff(
            outcome,
            source_url="http://www.belcan.com/",
        )
    assert handoff is not None
    assert handoff[0] == "https://www.belcan.com/employment/"

    print(
        "PHASE_7C3B_TRUTHFUL_ROUTE_SMOKE_OK",
        json.dumps(
            {
                "spa_detail_score": assessment.score,
                "navigation_rejections": preservation[
                    "adaptive_urls_rejected_navigation"
                ],
                "empty_route_handoff": handoff[0],
                "owned_sibling_scope": _owned_sibling_hosts(
                    "jobs.insightglobal.com"
                ),
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

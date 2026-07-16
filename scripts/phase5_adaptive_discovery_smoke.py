from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.acquisition import AcquisitionContext, ICIMSProvider
from src.portals.url_intelligence import assess_llm_eligibility, rank_job_candidate_urls


def main() -> None:
    listing_url = "https://www.judge.com/jobs/"
    ranked, metrics = rank_job_candidate_urls(
        [
            "https://www.judge.com/resources",
            "https://www.judge.com/jobs/?_ga=tracking",
            "https://www.judge.com/jobs/details/1142387",
        ],
        listing_url=listing_url,
        platform_hint="custom_listing",
    )
    llm_allowed, llm_reason = assess_llm_eligibility(
        SimpleNamespace(html="<h1>Resources</h1><p>Company news and events</p>"),
        "https://www.judge.com/resources",
    )
    icims_match = ICIMSProvider().match(
        AcquisitionContext(listing_url="https://careers-example.icims.com/jobs")
    )

    assert ranked == ["https://www.judge.com/jobs/details/1142387"]
    assert metrics["rejected_urls"] == 2
    assert not llm_allowed
    assert icims_match is not None
    assert icims_match.platform == "icims"
    print(
        "PHASE_5_ADAPTIVE_DISCOVERY_SMOKE_OK",
        f"ranked_urls={len(ranked)}",
        f"rejected_urls={metrics['rejected_urls']}",
        f"llm_skipped_reason={json.dumps(llm_reason)}",
        f"provider={icims_match.platform}",
    )


if __name__ == "__main__":
    main()

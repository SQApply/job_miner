from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.extract.deterministic_lane import extract_job_from_html
from src.portals.acquisition import (
    AcquisitionContext,
    AcquisitionRegistry,
    PublicHtmlProvider,
)
from src.portals.page_quality import assess_page_quality
from src.portals.url_intelligence import (
    promote_trusted_detail_url,
    rank_job_candidate_urls,
)


class _HtmlClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.responses = [
            '<html><a href="/search-results-usa">Search jobs</a></html>',
            (
                '<html><div>2 results</div>'
                '<a href="/job/100_usa/data-engineer">Data Engineer</a>'
                '<a href="/job/101_usa/security-engineer">Security Engineer</a>'
                "</html>"
            ),
        ]

    async def request_text(self, url: str, *, timeout_seconds: float = 20.0) -> str:
        self.calls.append(url)
        return self.responses.pop(0)

    async def request_json(self, *args, **kwargs):
        raise AssertionError("The selector-free HTML smoke test must not use a JSON endpoint")


async def _run() -> None:
    challenge = (
        "<html><title>Careers</title>"
        "<script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'></script>"
        "</html>"
    )
    assert assess_page_quality(html=challenge).blocked

    job_url = "https://www.judge.example/jobs/details/1141335"
    job_payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Security Engineer",
        "url": job_url,
        "description": "Own production security controls.",
    }
    job_html = (
        "<html><script type='application/ld+json'>"
        f"{json.dumps(job_payload)}"
        "</script><footer>Protected by reCAPTCHA</footer></html>"
    )
    assert not assess_page_quality(html=job_html).blocked
    assert extract_job_from_html(job_html, job_url) is not None

    ranked, _ = rank_job_candidate_urls(
        [
            "https://www.googletagmanager.com/ns.html?id=GTM-1",
            "tel:8554858853",
            "https://careers.example.com/job/100_usa/data-engineer",
        ],
        listing_url="https://careers.example.com/search-results-usa",
    )
    assert ranked == ["https://careers.example.com/job/100_usa/data-engineer"]

    outer = "https://careers.example.icims.com/company/jobs/7420?lang=en-us&in_iframe=1"
    canonical_listing = "https://tenant.i.icims.com/company/jobs"
    promoted = promote_trusted_detail_url(
        outer,
        platform_hint="icims",
        acquisition_hints={"listing_url": canonical_listing},
    )
    assert promoted == "https://tenant.i.icims.com/company/jobs/7420?lang=en-us"

    client = _HtmlClient()
    outcome = await AcquisitionRegistry(
        client=client,
        providers=(PublicHtmlProvider(),),
    ).acquire(
        AcquisitionContext(
            listing_url="https://careers.example.com/consultant-careers",
            source_platform_hint="custom_listing",
            max_pages=2,
            require_complete=False,
        )
    )
    assert outcome.selected is not None
    assert len(outcome.selected.discovered_urls) == 2
    print(
        "PHASE_5_4_RESILIENT_SURFACES_SMOKE_OK "
        "challenge_blocked=true judge_schema_usable=true "
        f"public_html_jobs={len(outcome.selected.discovered_urls)} "
        f"http_pages={len(client.calls)} icims_promoted=true telemetry_rejected=true"
    )


if __name__ == "__main__":
    asyncio.run(_run())

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.acquisition import (
    AcquisitionContext,
    AcquisitionRegistry,
    PublicHtmlProvider,
    PublicTextResponse,
)
from src.portals.detector import detect_portal
from src.portals.route_resolver import resolve_listing_route


class _RedirectClient:
    async def request_public_text(
        self,
        url: str,
        *,
        timeout_seconds: float = 20.0,
    ) -> PublicTextResponse:
        assert url == "http://legacy.company.example/careers"
        return PublicTextResponse(
            document=(
                "<h1>Job Openings</h1>"
                '<a href="/job/10001/platform-engineer">Platform Engineer</a>'
                '<a href="/job/10002/data-engineer">Data Engineer</a>'
            ),
            final_url="https://jobs.company.example/openings",
            redirect_chain=("https://jobs.company.example/openings",),
        )


async def _main() -> None:
    detail_resolution = resolve_listing_route(
        source_url="https://careers.example.com/jobs",
        html=(
            '<a href="/jobs/ac-dc-power-solutions-engineer-247271/">Engineer</a>'
            '<a href="/jobs/72187-field-service-engineer/">Field Engineer</a>'
            '<a href="/#/jobs/26240">SPA Job</a>'
            '<a href="/search/details/?job_id=13803">Search Result</a>'
        ),
    )
    assert detail_resolution.selected is None
    assert not detail_resolution.candidates

    locale_resolution = resolve_listing_route(
        source_url="https://jobs.example.com/us/en/search-results",
        html=(
            '<a href="/ca/search-results">Search Jobs Canada</a>'
            '<a href="/ca/fr">Careers Canada French</a>'
        ),
    )
    assert locale_resolution.selected is None

    with patch(
        "src.portals.acquisition.validate_public_http_url",
        side_effect=lambda url: SimpleNamespace(
            normalized_url=url,
            hostname=str(url).split("/", 3)[2],
        ),
    ):
        outcome = await AcquisitionRegistry(
            client=_RedirectClient(),
            providers=(PublicHtmlProvider(),),
        ).acquire(
            AcquisitionContext(
                listing_url="http://legacy.company.example/careers",
                source_platform_hint="custom_listing",
                max_pages=1,
                require_complete=False,
            )
        )
    selected = outcome.selected
    assert selected is not None
    assert len(selected.discovered_urls) == 2
    assert selected.metadata["redirect_evidence"][0]["final_url"] == (
        "https://jobs.company.example/openings"
    )

    workday = "https://chghealthcare.wd1.myworkdayjobs.com/External?q=Weatherby"
    detected = detect_portal(
        listing_url="https://weatherby.example/careers",
        html=f'<a href="{workday}">Search Jobs</a>',
    )
    assert detected.source_platform == "workday"
    assert detected.acquisition_hints["site_token"] == "External"

    print(
        "PHASE_5_5C_REPAIR_BATCH_1_SMOKE_OK",
        f"redirect_jobs={len(selected.discovered_urls)}",
        f"redirect_hosts={len(selected.trusted_hosts)}",
        "detail_routes_rejected=4",
        "locale_loop_rejected=true",
        f"workday_site={detected.acquisition_hints['site_token']}",
    )


if __name__ == "__main__":
    asyncio.run(_main())

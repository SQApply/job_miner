from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.acquisition import AcquisitionContext, AcquisitionRegistry, PublicHtmlProvider
from src.portals.route_resolver import resolve_listing_route


class _StaticClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request_text(self, url: str, *, timeout_seconds: float = 20.0) -> str:
        self.calls.append(url)
        if url == "https://www.company.example/careers":
            return '<a href="https://jobs.company.example/search-results">Search Jobs</a>'
        if url == "https://jobs.company.example/search-results":
            return (
                '<a href="/job/10001/platform-engineer">Platform Engineer</a>'
                '<a href="/job/10002/data-engineer">Data Engineer</a>'
            )
        raise AssertionError(f"Unexpected request: {url}")

    async def request_json(self, *args, **kwargs):
        raise AssertionError("JSON acquisition must not run in this smoke test")


async def _run_handoff() -> tuple[int, int, int]:
    client = _StaticClient()
    with patch(
        "src.portals.acquisition.validate_public_http_url",
        return_value=SimpleNamespace(),
    ):
        outcome = await AcquisitionRegistry(
            client=client,
            providers=(PublicHtmlProvider(),),
        ).acquire(
            AcquisitionContext(
                listing_url="https://www.company.example/careers",
                source_platform_hint="custom_listing",
                max_pages=2,
                require_complete=False,
            )
        )
    selected = outcome.selected
    assert selected is not None
    assert len(selected.discovered_urls) == 2
    assert selected.metadata["listing_url"] == "https://jobs.company.example/search-results"
    assert selected.trusted_hosts == ("jobs.company.example", "www.company.example")
    return len(client.calls), len(selected.discovered_urls), len(selected.trusted_hosts)


def main() -> None:
    apex = resolve_listing_route(
        source_url="https://www.apex.example/consultant-careers",
        html='<a href="/search-results-usa">Search Jobs</a>',
    )
    assert apex.selected is not None
    assert apex.selected.url == "https://www.apex.example/search-results-usa"

    workday_url = "https://acme.wd5.myworkdayjobs.com/en-US/External"
    workday = resolve_listing_route(
        source_url="https://www.acme.example/careers",
        html=f'<script>window.careersUrl = "{workday_url}";</script>',
    )
    assert workday.selected is not None
    assert workday.selected.platform == "workday"

    rejected = resolve_listing_route(
        source_url="https://www.acme.example/careers",
        html=(
            '<a href="https://www.linkedin.com/company/acme">LinkedIn</a>'
            '<a href="/careers/benefits">Benefits</a>'
        ),
    )
    assert rejected.selected is None

    requests, jobs, trusted_hosts = asyncio.run(_run_handoff())
    print(
        "PHASE_5_5B_ROUTE_RESOLUTION_SMOKE_OK",
        f"same_host_route={apex.selected.url}",
        f"ats_platform={workday.selected.platform}",
        f"handoff_requests={requests}",
        f"jobs={jobs}",
        f"trusted_hosts={trusted_hosts}",
        "manual_selectors=0",
        "weak_routes_rejected=true",
    )


if __name__ == "__main__":
    main()

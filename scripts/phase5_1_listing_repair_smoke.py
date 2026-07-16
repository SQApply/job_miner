from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.acquisition import AcquisitionContext, AcquisitionRegistry
from src.portals.detector import detect_portal
from src.portals.url_intelligence import rank_job_candidate_urls


class _Client:
    def __init__(self, document: str) -> None:
        self.document = document
        self.calls: list[str] = []

    async def request_json(self, url, *, method="GET", payload=None, timeout_seconds=20.0):
        raise AssertionError("unexpected JSON request")

    async def request_text(self, url, *, timeout_seconds=20.0):
        self.calls.append(url)
        return self.document


async def _main() -> None:
    outer = "https://careers-insightglobal.icims.com/jobs"
    canonical = (
        "https://c-13769-20240415-careers-insightglobal-com.i.icims.com/"
        "insightglobal-careers/jobs"
    )
    detection = detect_portal(
        listing_url=outer,
        html=f'<a href="{canonical}">Search Jobs</a>',
    )
    if detection.acquisition_hints.get("listing_url") != canonical:
        raise SystemExit("canonical iCIMS route was not detected")

    client = _Client(
        '<h2>88 results</h2><a href="/insightglobal-careers/jobs/7420?lang=en-us">Job</a>'
    )
    outcome = await AcquisitionRegistry(client=client).acquire(
        AcquisitionContext(listing_url=canonical, max_pages=1, require_complete=False)
    )
    if outcome.selected is None or len(outcome.selected.discovered_urls) != 1:
        raise SystemExit("tenant-routed iCIMS acquisition failed")

    apex = detect_portal(
        listing_url="https://www.apexsystems.com/consultant-careers",
        html='<a href="/search-results-usa">Search Jobs</a>',
    )
    ranked, metrics = rank_job_candidate_urls(
        ["https://jobs.insightglobal.com/?utm_source=tracking"],
        listing_url=outer,
        platform_hint="icims",
    )
    if apex.acquisition_hints.get("listing_url") != "https://www.apexsystems.com/search-results-usa":
        raise SystemExit("same-site results route was not detected")
    if ranked or not metrics.get("strict_platform_filter"):
        raise SystemExit("known-ATS fallback guard failed")

    print(
        "PHASE_5_1_LISTING_REPAIR_SMOKE_OK "
        f"icims_jobs={len(outcome.selected.discovered_urls)} "
        f"icims_complete={str(outcome.selected.complete).lower()} "
        f"apex_route={apex.acquisition_hints['listing_url']} "
        "gpu_llm_calls=0"
    )


if __name__ == "__main__":
    asyncio.run(_main())

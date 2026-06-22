from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..blueprint_hub import BlueprintHub
from ..crawl.browser_lane import build_browser_config, close_session, detail_run_config, listing_run_config
from ..extract.model_lane import build_llm_strategy, parse_extracted_jobs
from ..extract.validator import is_valid_job
from ..router import get_adapter
from ..schemas import JobPosting
from .blueprint import build_portal_blueprint
from .detector import PortalDetection, detect_portal
from .safety import PortalUrlSafetyError, host_is_allowed, validate_public_http_url


@dataclass(frozen=True)
class PortalProbeResult:
    final_url: str
    detected: PortalDetection
    discovered_urls: int
    sample_job_urls: list[str]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["detected"] = self.detected.to_dict()
        return data


@dataclass(frozen=True)
class PortalScrapeResult:
    discovered_urls: int
    attempted_urls: int
    extracted_jobs: list[JobPosting]
    rejected_urls: int
    elapsed_seconds: float

    def metrics(self) -> dict[str, Any]:
        return {
            "discovered_urls": self.discovered_urls,
            "attempted_urls": self.attempted_urls,
            "extracted_jobs": len(self.extracted_jobs),
            "rejected_urls": self.rejected_urls,
            "elapsed_seconds": self.elapsed_seconds,
        }


def _result_html(result: Any) -> str:
    for field in ("cleaned_html", "html", "markdown"):
        value = getattr(result, field, None)
        if value:
            return str(value)
    return ""


def _result_text(result: Any) -> str:
    for field in ("markdown", "fit_markdown", "text"):
        value = getattr(result, field, None)
        if value:
            return str(value)
    return ""


def _result_final_url(result: Any, fallback: str) -> str:
    for field in ("url", "final_url", "redirected_url"):
        value = getattr(result, field, None)
        if value:
            return str(value)
    return fallback


def _safe_discovered_urls(urls: list[str], allowed_hosts: list[str]) -> tuple[list[str], int]:
    valid: list[str] = []
    rejected = 0
    seen: set[str] = set()
    for url in urls:
        try:
            checked = validate_public_http_url(url, allowed_hosts=allowed_hosts)
        except PortalUrlSafetyError:
            rejected += 1
            continue
        if checked.normalized_url not in seen:
            seen.add(checked.normalized_url)
            valid.append(checked.normalized_url)
    return valid, rejected


async def probe_portal(*, root: Path, portal: dict[str, Any], run_session_id: str) -> PortalProbeResult:
    """Open one listing page, verify redirect safety, detect a profile, and try discovery."""
    from crawl4ai import AsyncWebCrawler

    hub = BlueprintHub(root)
    settings = hub.system.browser
    listing_url = str(portal.get("listing_url") or "")
    checked_url = validate_public_http_url(listing_url, allowed_hosts=portal.get("allowed_hosts") or None)

    browser_config = build_browser_config(settings)
    probe_session_id = f"portal_probe_{str(portal['id']).replace('-', '')[:16]}"

    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(
            url=checked_url.normalized_url,
            config=listing_run_config(settings, probe_session_id, None),
        )
        if not getattr(result, "success", False):
            raise RuntimeError(f"Portal listing probe failed: {getattr(result, 'error_message', 'unknown error')}")

        final_url = _result_final_url(result, checked_url.normalized_url)
        final_checked = validate_public_http_url(final_url, allowed_hosts=checked_url.allowed_hosts)
        html = _result_html(result)
        detection = detect_portal(listing_url=final_checked.normalized_url, html=html, text_content=_result_text(result))

        discovered_urls = 0
        samples: list[str] = []
        if not detection.blocked:
            portal_for_profile = dict(portal)
            portal_for_profile["canonical_listing_url"] = final_checked.normalized_url
            portal_for_profile["profile_name"] = detection.profile_name
            blueprint = build_portal_blueprint(root=root, portal=portal_for_profile, run_session_id=run_session_id)
            adapter = get_adapter(blueprint)
            urls = await adapter.discover_job_urls(crawler, blueprint, hub.system)
            safe_urls, _ = _safe_discovered_urls(urls, list(blueprint.allowed_hosts))
            discovered_urls = len(safe_urls)
            samples = safe_urls[:5]
        else:
            await close_session(crawler, probe_session_id)

    return PortalProbeResult(
        final_url=final_checked.normalized_url,
        detected=detection,
        discovered_urls=discovered_urls,
        sample_job_urls=samples,
    )


async def scrape_portal(
    *,
    root: Path,
    portal: dict[str, Any],
    run_session_id: str,
    max_jobs: int,
) -> PortalScrapeResult:
    """Run a bounded browser + LLM scrape for an already-probed portal.

    The caller decides whether this is a non-persistent test scrape or a full
    catalog ingestion. This function never marks old jobs inactive; lifecycle
    reconciliation requires a separate complete-crawl safety gate.
    """
    from crawl4ai import AsyncWebCrawler

    started = time.perf_counter()
    hub = BlueprintHub(root)
    settings = hub.system.browser
    blueprint = build_portal_blueprint(root=root, portal=portal, run_session_id=run_session_id)
    adapter = get_adapter(blueprint)
    instruction = blueprint.detail.instruction
    llm_strategy = build_llm_strategy(hub.system.llm, instruction)

    browser_config = build_browser_config(settings)
    rejected_urls = 0

    async with AsyncWebCrawler(config=browser_config) as crawler:
        urls = await adapter.discover_job_urls(crawler, blueprint, hub.system)
        safe_urls, rejected = _safe_discovered_urls(urls, list(blueprint.allowed_hosts))
        rejected_urls += rejected
        attempted_urls = safe_urls[:max(1, max_jobs)]
        concurrency = max(1, min(int(settings.detail_extraction_concurrency), 5, len(attempted_urls) or 1))
        semaphore = asyncio.Semaphore(concurrency)
        # A portal-specific rate gate applies across concurrent detail requests.
        # It intentionally lives inside this single portal run so one noisy source
        # cannot burst through the rate configured by the administrator.
        request_interval_seconds = 60.0 / max(1, int(portal.get("request_rate_limit_per_minute") or 1))
        rate_lock = asyncio.Lock()
        next_request_monotonic = 0.0

        async def throttle_detail_request() -> None:
            nonlocal next_request_monotonic
            async with rate_lock:
                now = time.monotonic()
                wait_seconds = max(0.0, next_request_monotonic - now)
                next_request_monotonic = max(now, next_request_monotonic) + request_interval_seconds
            if wait_seconds:
                await asyncio.sleep(wait_seconds)

        async def extract_one(index: int, job_url: str) -> JobPosting | None:
            nonlocal rejected_urls
            async with semaphore:
                try:
                    validate_public_http_url(job_url, allowed_hosts=blueprint.allowed_hosts)
                    detail_session_id = f"portal_detail_{str(portal['id']).replace('-', '')[:12]}_{index}"
                    await throttle_detail_request()
                    result = await crawler.arun(
                        url=job_url,
                        config=detail_run_config(
                            settings,
                            blueprint.detail.wait_for,
                            llm_strategy,
                            session_id=detail_session_id,
                        ),
                    )
                    if not getattr(result, "success", False):
                        return None
                    final_url = _result_final_url(result, job_url)
                    validate_public_http_url(final_url, allowed_hosts=blueprint.allowed_hosts)
                    job = parse_extracted_jobs(getattr(result, "extracted_content", None), final_url)
                    return job if job and is_valid_job(job) else None
                except PortalUrlSafetyError:
                    rejected_urls += 1
                    return None
                finally:
                    try:
                        await close_session(crawler, f"portal_detail_{str(portal['id']).replace('-', '')[:12]}_{index}")
                    except Exception:
                        pass

        results = await asyncio.gather(
            *(extract_one(index, job_url) for index, job_url in enumerate(attempted_urls, start=1)),
            return_exceptions=True,
        )

    jobs = [row for row in results if isinstance(row, JobPosting)]
    return PortalScrapeResult(
        discovered_urls=len(safe_urls),
        attempted_urls=len(attempted_urls),
        extracted_jobs=jobs,
        rejected_urls=rejected_urls,
        elapsed_seconds=round(time.perf_counter() - started, 3),
    )

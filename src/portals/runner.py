from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..blueprint_hub import BlueprintHub
from ..crawl.browser_lane import build_browser_config, close_session, detail_run_config, listing_run_config
from ..extract.model_lane import build_llm_strategy, parse_extracted_jobs
from ..extract.validator import is_valid_job
from ..infrastructure.mongo import get_mongo_database
from ..router import get_adapter
from ..schemas import JobPosting
from ..warehouse.repositories import WarehouseRepository
from .artifacts import result_artifacts, save_portal_artifact
from .blueprint import build_portal_blueprint
from .detector import PortalDetection, detect_portal
from .safety import PortalUrlSafetyError, validate_public_http_url


@dataclass(frozen=True)
class PortalProbeResult:
    final_url: str
    detected: PortalDetection
    discovered_urls: int
    sample_job_urls: list[str]
    artifacts: list[dict[str, Any]] | None = None

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
    artifacts: list[dict[str, Any]] | None = None
    detail_failures: list[dict[str, Any]] | None = None
    skipped_existing: int = 0
    rescrape_plan: dict[str, Any] | None = None
    lifecycle_reconcile: dict[str, Any] | None = None
    discovered_job_urls: list[str] | None = None

    def metrics(self) -> dict[str, Any]:
        return {
            "discovered_urls": self.discovered_urls,
            "attempted_urls": self.attempted_urls,
            "skipped_existing": self.skipped_existing,
            "extracted_jobs": len(self.extracted_jobs),
            "rejected_urls": self.rejected_urls,
            "elapsed_seconds": self.elapsed_seconds,
            "artifact_count": len(self.artifacts or []),
            "detail_failure_count": len(self.detail_failures or []),
            "detail_failures": (self.detail_failures or [])[:25],
            "rescrape_plan": self.rescrape_plan or {},
            "lifecycle_reconcile": self.lifecycle_reconcile or {},
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


def _plan_portal_detail_rescrape(
    *,
    target_id: str,
    run_session_id: str,
    safe_urls: list[str],
    incremental_rescrape: bool,
    force_detail_refresh: bool,
    deep_refresh_days: int,
) -> tuple[list[str], dict[str, Any]]:
    if not incremental_rescrape:
        return safe_urls, {
            "status": "disabled",
            "discovered_urls": len(safe_urls),
            "urls_to_extract_count": len(safe_urls),
            "known_skipped": 0,
        }
    try:
        repo = WarehouseRepository(get_mongo_database())
        plan = repo.plan_detail_rescrape(
            target_id=target_id,
            run_session_id=run_session_id,
            discovered_urls=safe_urls,
            force_detail_refresh=force_detail_refresh,
            deep_refresh_days=deep_refresh_days,
        )
        return list(plan.get("urls_to_extract") or []), {**plan, "status": "planned"}
    except Exception as exc:
        return safe_urls, {
            "status": "fallback_full_scrape",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "discovered_urls": len(safe_urls),
            "urls_to_extract_count": len(safe_urls),
        }


async def probe_portal(*, root: Path, portal: dict[str, Any], run_session_id: str) -> PortalProbeResult:
    """Open one listing page, verify redirect safety, detect a profile, and try discovery."""
    from crawl4ai import AsyncWebCrawler

    hub = BlueprintHub(root)
    settings = hub.system.browser
    listing_url = str(portal.get("listing_url") or "")
    checked_url = validate_public_http_url(listing_url, allowed_hosts=portal.get("allowed_hosts") or None)

    browser_config = build_browser_config(settings)
    probe_session_id = f"portal_probe_{str(portal['id']).replace('-', '')[:16]}"
    artifacts: list[dict[str, Any]] = []

    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(
            url=checked_url.normalized_url,
            config=listing_run_config(settings, probe_session_id, None),
        )
        artifacts.extend(result_artifacts(
            root=root,
            portal_id=str(portal["id"]),
            run_session_id=run_session_id,
            prefix="probe_listing",
            result=result,
            metadata={"url": checked_url.normalized_url},
        ))
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
            artifact = save_portal_artifact(
                root=root,
                portal_id=str(portal["id"]),
                run_session_id=run_session_id,
                category="portal_crawl",
                artifact_type="discovered_urls",
                name="probe_discovered_urls",
                content={"urls": safe_urls[:100], "total": len(safe_urls)},
                mime_type="application/json",
                extension=".json",
                metadata={"stage": "probe"},
            )
            if artifact:
                artifacts.append(artifact)
        else:
            await close_session(crawler, probe_session_id)

    return PortalProbeResult(
        final_url=final_checked.normalized_url,
        detected=detection,
        discovered_urls=discovered_urls,
        sample_job_urls=samples,
        artifacts=artifacts,
    )


async def scrape_portal(
    *,
    root: Path,
    portal: dict[str, Any],
    run_session_id: str,
    max_jobs: int,
    incremental_rescrape: bool = True,
    force_detail_refresh: bool = False,
    deep_refresh_days: int = 14,
    reconcile_lifecycle: bool = False,
) -> PortalScrapeResult:
    """Run a bounded browser + LLM scrape for an already-probed portal.

    This function discovers listing URLs and extracts details. It intentionally
    does not deactivate missing jobs. Lifecycle reconciliation is a post-ingestion
    decision made by the task after the scrape is known to be successful.
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
    artifacts: list[dict[str, Any]] = []
    detail_failures: list[dict[str, Any]] = []
    safe_urls: list[str] = []
    rescrape_plan: dict[str, Any] = {}
    attempted_urls: list[str] = []

    async with AsyncWebCrawler(config=browser_config) as crawler:
        urls = await adapter.discover_job_urls(crawler, blueprint, hub.system)
        safe_urls, rejected = _safe_discovered_urls(urls, list(blueprint.allowed_hosts))
        rejected_urls += rejected
        discovered_artifact = save_portal_artifact(
            root=root,
            portal_id=str(portal["id"]),
            run_session_id=run_session_id,
            category="portal_crawl",
            artifact_type="discovered_urls",
            name="scrape_discovered_urls",
            content={"urls": safe_urls[:500], "total": len(safe_urls)},
            mime_type="application/json",
            extension=".json",
            metadata={"stage": "scrape"},
        )
        if discovered_artifact:
            artifacts.append(discovered_artifact)

        urls_to_extract, rescrape_plan = _plan_portal_detail_rescrape(
            target_id=str(portal["target_id"]),
            run_session_id=run_session_id,
            safe_urls=safe_urls,
            incremental_rescrape=incremental_rescrape,
            force_detail_refresh=force_detail_refresh,
            deep_refresh_days=deep_refresh_days,
        )
        attempted_urls = urls_to_extract[:max(1, max_jobs)]
        concurrency = max(1, min(int(settings.detail_extraction_concurrency), 5, len(attempted_urls) or 1))
        semaphore = asyncio.Semaphore(concurrency)
        request_interval_seconds = 60.0 / max(1, int(portal.get("request_rate_limit_per_minute") or 1))
        detail_retry_attempts = max(0, min(int(portal.get("detail_retry_attempts") or 0), 5))
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
                last_error: str | None = None
                for attempt in range(1, detail_retry_attempts + 2):
                    detail_session_id = f"portal_detail_{str(portal['id']).replace('-', '')[:12]}_{index}_{attempt}"
                    try:
                        validate_public_http_url(job_url, allowed_hosts=blueprint.allowed_hosts)
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
                            last_error = str(getattr(result, "error_message", "detail scrape failed"))
                            if attempt == detail_retry_attempts + 1:
                                artifacts.extend(result_artifacts(
                                    root=root,
                                    portal_id=str(portal["id"]),
                                    run_session_id=run_session_id,
                                    prefix=f"detail_{index}_failed",
                                    result=result,
                                    metadata={"job_url": job_url, "attempt": attempt},
                                ))
                            continue
                        final_url = _result_final_url(result, job_url)
                        validate_public_http_url(final_url, allowed_hosts=blueprint.allowed_hosts)
                        job = parse_extracted_jobs(getattr(result, "extracted_content", None), final_url)
                        if job and is_valid_job(job):
                            return job
                        last_error = "LLM extraction returned no valid job"
                    except PortalUrlSafetyError as exc:
                        rejected_urls += 1
                        last_error = str(exc)
                        break
                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                    finally:
                        try:
                            await close_session(crawler, detail_session_id)
                        except Exception:
                            pass
                    if attempt <= detail_retry_attempts:
                        await asyncio.sleep(min(2 ** (attempt - 1), 8))
                detail_failures.append({"job_url": job_url, "attempts": detail_retry_attempts + 1, "error": last_error or "unknown"})
                return None

        results = await asyncio.gather(
            *(extract_one(index, job_url) for index, job_url in enumerate(attempted_urls, start=1)),
            return_exceptions=True,
        )

    jobs = [row for row in results if isinstance(row, JobPosting)]
    exception_failures = [row for row in results if isinstance(row, BaseException)]
    for exc in exception_failures[:25]:
        detail_failures.append({"job_url": None, "attempts": 1, "error": f"{type(exc).__name__}: {exc}"})

    lifecycle_reconcile = {"status": "pending_post_ingestion" if reconcile_lifecycle else "disabled"}
    return PortalScrapeResult(
        discovered_urls=len(safe_urls),
        attempted_urls=len(attempted_urls),
        extracted_jobs=jobs,
        rejected_urls=rejected_urls,
        elapsed_seconds=round(time.perf_counter() - started, 3),
        artifacts=artifacts,
        detail_failures=detail_failures,
        skipped_existing=int((rescrape_plan or {}).get("known_skipped") or 0),
        rescrape_plan=rescrape_plan,
        lifecycle_reconcile=lifecycle_reconcile,
        discovered_job_urls=safe_urls,
    )

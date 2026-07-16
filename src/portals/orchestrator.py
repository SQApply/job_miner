from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..crawl.browser_lane import (
    build_browser_config,
    close_session,
    detail_llm_run_config,
    detail_run_config,
    resilient_detail_wait,
)
from ..extract.deterministic_lane import extract_job_from_result
from ..extract.model_lane import build_llm_strategy, parse_extracted_jobs
from ..extract.validator import is_valid_job
from ..router import get_adapter
from ..schemas import JobPosting, ResolvedBlueprint, SystemConfig
from .acquisition import (
    AcquisitionContext,
    AcquisitionOutcome,
    AcquisitionRegistry,
    default_acquisition_registry,
)


EventCallback = Callable[[str, dict[str, Any]], None]
DiscoveryNormalizer = Callable[[list[str]], tuple[list[str], int]]
AcquiredDiscoveryNormalizer = Callable[[list[str], tuple[str, ...]], tuple[list[str], int]]
DetailPlanner = Callable[[list[str]], tuple[list[str], dict[str, Any]]]
UrlValidator = Callable[[str], str]
RejectedErrorPredicate = Callable[[BaseException], bool]
DiscoveryArtifactCallback = Callable[[list[str]], list[dict[str, Any]] | None]
FailureArtifactCallback = Callable[[int, str, int, Any], list[dict[str, Any]] | None]
FailedPayloadCallback = Callable[[str, Any], None]
GpuSnapshotCallback = Callable[[], dict[str, Any]]


def _normalize_discovered_urls(urls: list[str]) -> tuple[list[str], int]:
    normalized = list(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))
    return normalized, max(0, len(urls) - len(normalized))


def _plan_all_urls(urls: list[str]) -> tuple[list[str], dict[str, Any]]:
    return list(urls), {
        "status": "disabled",
        "discovered_urls": len(urls),
        "urls_to_extract_count": len(urls),
        "known_skipped": 0,
    }


def _identity_url(url: str) -> str:
    return url


def _never_rejected(_: BaseException) -> bool:
    return False


@dataclass(frozen=True)
class ScrapeExecutionOptions:
    """Runtime limits shared by static fleet and database-backed portal runs."""

    detail_concurrency: int = 1
    detail_retry_attempts: int = 0
    requests_per_minute: int | None = None
    max_jobs: int | None = None
    fail_on_zero_discovery: bool = True
    session_prefix: str = "detail"
    prefer_platform_api: bool = True
    max_acquisition_pages: int = 50
    acquisition_timeout_seconds: float = 20.0
    require_complete_acquisition: bool = True

    def __post_init__(self) -> None:
        if self.detail_concurrency < 1:
            raise ValueError("detail_concurrency must be at least 1")
        if not 0 <= self.detail_retry_attempts <= 5:
            raise ValueError("detail_retry_attempts must be between 0 and 5")
        if self.requests_per_minute is not None and self.requests_per_minute < 1:
            raise ValueError("requests_per_minute must be at least 1 when provided")
        if self.max_jobs is not None and self.max_jobs < 1:
            raise ValueError("max_jobs must be at least 1 when provided")
        if not self.session_prefix.strip():
            raise ValueError("session_prefix cannot be empty")
        if self.max_acquisition_pages < 1:
            raise ValueError("max_acquisition_pages must be at least 1")
        if self.acquisition_timeout_seconds <= 0:
            raise ValueError("acquisition_timeout_seconds must be positive")


@dataclass
class ScrapeOrchestratorHooks:
    """Caller-owned policy and side effects around the shared scrape engine."""

    normalize_discovered_urls: DiscoveryNormalizer = _normalize_discovered_urls
    normalize_acquired_urls: AcquiredDiscoveryNormalizer | None = None
    plan_detail_urls: DetailPlanner = _plan_all_urls
    validate_detail_url: UrlValidator = _identity_url
    is_rejected_error: RejectedErrorPredicate = _never_rejected
    on_event: EventCallback | None = None
    on_discovery_artifacts: DiscoveryArtifactCallback | None = None
    on_failure_artifacts: FailureArtifactCallback | None = None
    on_failed_payload: FailedPayloadCallback | None = None
    adapter_session_logger: Any = None
    gpu_snapshot: GpuSnapshotCallback | None = None


@dataclass(frozen=True)
class OrchestratedScrapeResult:
    discovered_job_urls: list[str]
    attempted_job_urls: list[str]
    jobs: list[JobPosting]
    rejected_urls: int
    detail_failures: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    rescrape_plan: dict[str, Any]
    elapsed_seconds: float
    raw_discovered_urls: int = 0
    acquisition: dict[str, Any] = field(default_factory=dict)

    @property
    def skipped_existing(self) -> int:
        return int(self.rescrape_plan.get("known_skipped") or 0)


@dataclass
class _RateLimiter:
    requests_per_minute: int | None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _next_request_monotonic: float = 0.0

    async def wait(self) -> None:
        if self.requests_per_minute is None:
            return
        interval_seconds = 60.0 / self.requests_per_minute
        async with self._lock:
            now = time.monotonic()
            wait_seconds = max(0.0, self._next_request_monotonic - now)
            self._next_request_monotonic = max(now, self._next_request_monotonic) + interval_seconds
        if wait_seconds:
            await asyncio.sleep(wait_seconds)


class ScrapeOrchestrator:
    """Shared discovery/detail engine; storage, safety and artifacts stay caller-owned."""

    def __init__(
        self,
        *,
        blueprint: ResolvedBlueprint,
        system_config: SystemConfig,
        run_session_id: str,
        instruction: str | None = None,
        adapter: Any = None,
        source_platform_hint: str | None = None,
        acquisition_hints: dict[str, str] | None = None,
        acquisition_registry: AcquisitionRegistry | None = None,
    ) -> None:
        self.blueprint = blueprint
        self.system_config = system_config
        self.run_session_id = run_session_id
        self.instruction = instruction or blueprint.detail.instruction
        self.adapter = adapter or get_adapter(blueprint)
        self.source_platform_hint = str(source_platform_hint or "").strip() or None
        self.acquisition_hints = dict(acquisition_hints or {})
        self.acquisition_registry = acquisition_registry or default_acquisition_registry()

    async def run(
        self,
        *,
        options: ScrapeExecutionOptions,
        hooks: ScrapeOrchestratorHooks | None = None,
        acquisition_outcome: AcquisitionOutcome | None = None,
    ) -> OrchestratedScrapeResult:
        """Execute one source scrape and avoid launching a browser for full API feeds."""
        if acquisition_outcome is None and options.prefer_platform_api:
            try:
                acquisition_outcome = await self.acquisition_registry.acquire(
                    self._acquisition_context(options)
                )
            except Exception as exc:
                acquisition_outcome = AcquisitionOutcome(
                    selected=None,
                    attempts=[
                        {
                            "status": "failed",
                            "error_type": type(exc).__name__,
                            "error_message": str(exc)[:500],
                        }
                    ],
                )

        selected = acquisition_outcome.selected if acquisition_outcome is not None else None
        if selected is not None and selected.discovered_urls and all(
            url in selected.preextracted_jobs and is_valid_job(selected.preextracted_jobs[url])
            for url in selected.discovered_urls
        ):
            return await self.run_with_crawler(
                _UnavailableCrawler(),
                options=options,
                hooks=hooks,
                acquisition_outcome=acquisition_outcome,
            )

        from crawl4ai import AsyncWebCrawler

        browser_config = build_browser_config(self.system_config.browser)
        async with AsyncWebCrawler(config=browser_config) as crawler:
            return await self.run_with_crawler(
                crawler,
                options=options,
                hooks=hooks,
                acquisition_outcome=acquisition_outcome,
            )

    async def run_with_crawler(
        self,
        crawler: Any,
        *,
        options: ScrapeExecutionOptions,
        hooks: ScrapeOrchestratorHooks | None = None,
        acquisition_outcome: AcquisitionOutcome | None = None,
    ) -> OrchestratedScrapeResult:
        """Execute with an existing crawler; useful for probes and deterministic tests."""
        started = time.perf_counter()
        hooks = hooks or ScrapeOrchestratorHooks()
        artifacts: list[dict[str, Any]] = []
        detail_failures: list[dict[str, Any]] = []
        rejected_urls = 0
        preextracted_jobs: dict[str, JobPosting] = {}
        trusted_acquisition_hosts: tuple[str, ...] = ()
        acquisition: dict[str, Any] = {
            "selected": False,
            "strategy": "browser_fallback",
            "reason": "platform_api_disabled" if not options.prefer_platform_api else "no_provider_selected",
            "attempts": [],
        }

        selected_acquisition = None
        if acquisition_outcome is not None:
            selected_acquisition = acquisition_outcome.selected
            acquisition = acquisition_outcome.metrics()
        elif options.prefer_platform_api:
            try:
                outcome = await self.acquisition_registry.acquire(
                    self._acquisition_context(options)
                )
                selected_acquisition = outcome.selected
                acquisition = outcome.metrics()
            except Exception as exc:
                acquisition = {
                    "selected": False,
                    "strategy": "browser_fallback",
                    "reason": "registry_exception",
                    "attempts": [
                        {
                            "status": "failed",
                            "error_type": type(exc).__name__,
                            "error_message": str(exc)[:500],
                        }
                    ],
                }

        self._emit(hooks, "acquisition_complete", **acquisition)
        if selected_acquisition is not None:
            raw_urls = list(selected_acquisition.discovered_urls)
            preextracted_jobs = dict(selected_acquisition.preextracted_jobs)
            trusted_acquisition_hosts = tuple(selected_acquisition.trusted_hosts)
            normalizer = hooks.normalize_acquired_urls
            if normalizer is not None:
                discovered_urls, rejected = normalizer(list(raw_urls), trusted_acquisition_hosts)
            else:
                discovered_urls, rejected = hooks.normalize_discovered_urls(list(raw_urls))
            self._emit(
                hooks,
                "acquisition_selected",
                platform=selected_acquisition.platform,
                strategy=selected_acquisition.strategy,
                complete=selected_acquisition.complete,
                discovered_urls=len(discovered_urls),
                preextracted_jobs=len(preextracted_jobs),
            )
        else:
            self._emit(
                hooks,
                "acquisition_browser_fallback",
                page_url=self.blueprint.listing.page_url,
                attempts=acquisition.get("attempts") or [],
            )
            raw_urls = await self.adapter.discover_job_urls(
                crawler,
                self.blueprint,
                self.system_config,
                session_logger=hooks.adapter_session_logger,
            )
            discovered_urls, rejected = hooks.normalize_discovered_urls(list(raw_urls or []))
        rejected_urls += rejected
        self._emit(
            hooks,
            "discovery_complete",
            raw_discovered_urls=len(raw_urls or []),
            discovered_urls=len(discovered_urls),
            rejected_urls=rejected,
        )

        if not discovered_urls and options.fail_on_zero_discovery:
            self._emit(
                hooks,
                "discovery_failed_zero_urls",
                page_url=self.blueprint.listing.page_url,
                message="A job portal discovery run returned zero valid URLs and is not complete.",
            )
            raise RuntimeError(f"Target {self.blueprint.id} discovered zero valid job URLs")

        if hooks.on_discovery_artifacts is not None:
            artifacts.extend(hooks.on_discovery_artifacts(discovered_urls) or [])

        planned_urls, rescrape_plan = hooks.plan_detail_urls(discovered_urls)
        discovered_set = set(discovered_urls)
        attempted_urls = list(dict.fromkeys(url for url in planned_urls if url in discovered_set))
        ignored_planned_urls = len(list(planned_urls)) - len(attempted_urls)
        if ignored_planned_urls:
            self._emit(
                hooks,
                "rescrape_plan_invalid_urls_ignored",
                ignored_urls=ignored_planned_urls,
            )

        rescrape_plan = dict(rescrape_plan or {})
        rescrape_plan.setdefault("discovered_urls", len(discovered_urls))
        rescrape_plan["urls_to_extract_count"] = len(attempted_urls)
        self._emit(hooks, "rescrape_plan_complete", **rescrape_plan)

        if options.max_jobs is not None:
            attempted_urls = attempted_urls[: options.max_jobs]
            rescrape_plan.update(
                {
                    "bounded_test": True,
                    "bounded_max_jobs": options.max_jobs,
                    "bounded_urls_to_extract_count": len(attempted_urls),
                }
            )

        concurrency = max(1, min(options.detail_concurrency, len(attempted_urls) or 1))
        semaphore = asyncio.Semaphore(concurrency)
        rate_limiter = _RateLimiter(options.requests_per_minute)
        llm_strategy: Any = None
        llm_strategy_lock = asyncio.Lock()

        async def get_llm_strategy() -> Any:
            nonlocal llm_strategy
            if llm_strategy is None:
                async with llm_strategy_lock:
                    if llm_strategy is None:
                        llm_strategy = build_llm_strategy(self.system_config.llm, self.instruction)
            return llm_strategy

        self._emit(
            hooks,
            "parallel_extraction_start",
            total_job_urls=len(attempted_urls),
            discovered_urls=len(discovered_urls),
            skipped_existing=int(rescrape_plan.get("known_skipped") or 0),
            concurrency=concurrency,
            retry_attempts=options.detail_retry_attempts,
            requests_per_minute=options.requests_per_minute,
        )

        async def extract_one(item_index: int, original_job_url: str) -> JobPosting | None:
            nonlocal rejected_urls
            async with semaphore:
                self._emit(
                    hooks,
                    "extract_start",
                    job_url=original_job_url,
                    item_index=item_index,
                )
                acquired_job = preextracted_jobs.get(original_job_url)
                if acquired_job is not None and is_valid_job(acquired_job):
                    self._emit(
                        hooks,
                        "extract_saved",
                        job_url=original_job_url,
                        item_index=item_index,
                        attempt=0,
                        elapsed_seconds=0.0,
                        title=acquired_job.title,
                        extraction_method="platform_api",
                    )
                    return acquired_job
                last_error = "unknown extraction failure"
                attempts_used = 0

                for attempt in range(1, options.detail_retry_attempts + 2):
                    attempts_used = attempt
                    detail_session_id = (
                        f"{options.session_prefix}_{self._safe_session_component(self.blueprint.id)}_"
                        f"{item_index}_{attempt}"
                    )
                    result: Any = None
                    retryable = True
                    attempt_started = time.perf_counter()

                    try:
                        job_url = hooks.validate_detail_url(original_job_url)
                        await rate_limiter.wait()
                        if hooks.gpu_snapshot is not None:
                            self._emit(
                                hooks,
                                "gpu_before_extract",
                                job_url=job_url,
                                item_index=item_index,
                                attempt=attempt,
                                stage="browser_acquisition",
                                gpu=hooks.gpu_snapshot(),
                            )

                        result = await crawler.arun(
                            url=job_url,
                            config=detail_run_config(
                                self.system_config.browser,
                                resilient_detail_wait(self.blueprint.detail.wait_for),
                                None,
                                session_id=detail_session_id,
                            ),
                        )
                        elapsed = round(time.perf_counter() - attempt_started, 3)

                        if hooks.gpu_snapshot is not None:
                            self._emit(
                                hooks,
                                "gpu_after_extract",
                                job_url=job_url,
                                item_index=item_index,
                                attempt=attempt,
                                stage="browser_acquisition",
                                elapsed_seconds=elapsed,
                                gpu=hooks.gpu_snapshot(),
                            )

                        if not getattr(result, "success", False):
                            last_error = str(getattr(result, "error_message", "detail scrape failed"))
                            self._save_failed_payload(
                                hooks,
                                job_url,
                                f"CRAWL FAILED: {last_error}",
                            )
                            self._emit(
                                hooks,
                                "extract_attempt_failed",
                                job_url=job_url,
                                item_index=item_index,
                                attempt=attempt,
                                elapsed_seconds=elapsed,
                                error_message=last_error,
                                extraction_method="browser_acquisition",
                            )
                        else:
                            final_url = self._result_final_url(result, job_url)
                            final_url = hooks.validate_detail_url(final_url)
                            job = extract_job_from_result(result, final_url)
                            if job is not None and is_valid_job(job):
                                self._emit(
                                    hooks,
                                    "extract_saved",
                                    job_url=job_url,
                                    item_index=item_index,
                                    attempt=attempt,
                                    elapsed_seconds=elapsed,
                                    title=job.title,
                                    extraction_method="deterministic",
                                )
                                return job

                            self._emit(
                                hooks,
                                "llm_fallback_start",
                                job_url=job_url,
                                item_index=item_index,
                                attempt=attempt,
                            )
                            llm_result = await crawler.arun(
                                url=job_url,
                                config=detail_llm_run_config(
                                    self.system_config.browser,
                                    await get_llm_strategy(),
                                    detail_session_id,
                                ),
                            )
                            if not getattr(llm_result, "success", False):
                                last_error = str(getattr(llm_result, "error_message", "LLM extraction failed"))
                                self._save_failed_payload(
                                    hooks,
                                    job_url,
                                    f"LLM EXTRACTION FAILED: {last_error}",
                                )
                                self._emit(
                                    hooks,
                                    "extract_attempt_failed",
                                    job_url=job_url,
                                    item_index=item_index,
                                    attempt=attempt,
                                    elapsed_seconds=elapsed,
                                    error_message=last_error,
                                    extraction_method="llm_fallback",
                                )
                            else:
                                raw_content = getattr(llm_result, "extracted_content", None)
                                job = parse_extracted_jobs(raw_content, final_url)
                                if job is not None and is_valid_job(job):
                                    self._emit(
                                        hooks,
                                        "extract_saved",
                                        job_url=job_url,
                                        item_index=item_index,
                                        attempt=attempt,
                                        elapsed_seconds=elapsed,
                                        title=job.title,
                                        extraction_method="llm_fallback",
                                    )
                                    return job

                                last_error = "Deterministic and LLM extraction returned no valid job"
                                self._save_failed_payload(hooks, job_url, raw_content)
                                self._emit(
                                    hooks,
                                    "validation_failed" if job is not None else "parse_failed",
                                    job_url=job_url,
                                    item_index=item_index,
                                    attempt=attempt,
                                    elapsed_seconds=elapsed,
                                    parsed_title=getattr(job, "title", None),
                                )
                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                        if hooks.is_rejected_error(exc):
                            rejected_urls += 1
                            retryable = False
                            self._emit(
                                hooks,
                                "detail_url_rejected",
                                job_url=original_job_url,
                                item_index=item_index,
                                attempt=attempt,
                                error_message=str(exc),
                            )
                        else:
                            self._save_failed_payload(
                                hooks,
                                original_job_url,
                                f"CRAWL EXCEPTION: {last_error}",
                            )
                            self._emit(
                                hooks,
                                "extract_exception",
                                job_url=original_job_url,
                                item_index=item_index,
                                attempt=attempt,
                                elapsed_seconds=round(time.perf_counter() - attempt_started, 3),
                                error_message=last_error,
                            )
                    finally:
                        try:
                            await close_session(crawler, detail_session_id)
                        except Exception as cleanup_exc:
                            self._emit(
                                hooks,
                                "detail_session_cleanup_failed",
                                job_url=original_job_url,
                                item_index=item_index,
                                detail_session_id=detail_session_id,
                                error_message=str(cleanup_exc),
                            )

                    if not retryable:
                        break
                    if attempt <= options.detail_retry_attempts:
                        retry_delay_seconds = min(2 ** (attempt - 1), 8)
                        self._emit(
                            hooks,
                            "extract_retry_scheduled",
                            job_url=original_job_url,
                            item_index=item_index,
                            attempt=attempt,
                            retry_delay_seconds=retry_delay_seconds,
                        )
                        await asyncio.sleep(retry_delay_seconds)

                if result is not None and not getattr(result, "success", False):
                    if hooks.on_failure_artifacts is not None:
                        artifacts.extend(
                            hooks.on_failure_artifacts(
                                item_index,
                                original_job_url,
                                attempts_used,
                                result,
                            )
                            or []
                        )

                failure = {
                    "job_url": original_job_url,
                    "attempts": attempts_used,
                    "error": last_error,
                }
                detail_failures.append(failure)
                self._emit(hooks, "extract_failed", **failure, item_index=item_index)
                return None

        extraction_results = await asyncio.gather(
            *(extract_one(index, url) for index, url in enumerate(attempted_urls, start=1)),
            return_exceptions=True,
        )
        jobs: list[JobPosting] = []
        for item in extraction_results:
            if isinstance(item, BaseException):
                failure = {
                    "job_url": None,
                    "attempts": 1,
                    "error": f"{type(item).__name__}: {item}",
                }
                detail_failures.append(failure)
                self._emit(hooks, "extract_task_exception", **failure)
            elif isinstance(item, JobPosting):
                jobs.append(item)

        elapsed_seconds = round(time.perf_counter() - started, 3)
        self._emit(
            hooks,
            "parallel_extraction_complete",
            attempted_urls=len(attempted_urls),
            discovered_urls=len(discovered_urls),
            extracted_jobs=len(jobs),
            detail_failures=len(detail_failures),
            elapsed_seconds=elapsed_seconds,
        )
        return OrchestratedScrapeResult(
            discovered_job_urls=discovered_urls,
            attempted_job_urls=attempted_urls,
            jobs=jobs,
            rejected_urls=rejected_urls,
            detail_failures=detail_failures,
            artifacts=artifacts,
            rescrape_plan=rescrape_plan,
            elapsed_seconds=elapsed_seconds,
            raw_discovered_urls=len(raw_urls or []),
            acquisition=acquisition,
        )

    @staticmethod
    def _emit(hooks: ScrapeOrchestratorHooks, event: str, **payload: Any) -> None:
        if hooks.on_event is not None:
            hooks.on_event(event, payload)

    @staticmethod
    def _save_failed_payload(hooks: ScrapeOrchestratorHooks, job_url: str, payload: Any) -> None:
        if hooks.on_failed_payload is not None:
            hooks.on_failed_payload(job_url, payload)

    @staticmethod
    def _result_final_url(result: Any, fallback: str) -> str:
        for field_name in ("url", "final_url", "redirected_url"):
            value = getattr(result, field_name, None)
            if value:
                return str(value)
        return fallback

    @staticmethod
    def _safe_session_component(value: str) -> str:
        normalized = "".join(character if character.isalnum() else "_" for character in value)
        return normalized.strip("_")[:40] or "source"

    def _acquisition_context(self, options: ScrapeExecutionOptions) -> AcquisitionContext:
        return AcquisitionContext(
            listing_url=self.blueprint.listing.page_url,
            source_platform_hint=self.source_platform_hint,
            acquisition_hints=self.acquisition_hints,
            max_pages=options.max_acquisition_pages,
            timeout_seconds=options.acquisition_timeout_seconds,
            require_complete=options.require_complete_acquisition,
        )


class _UnavailableCrawler:
    async def arun(self, **_: Any) -> Any:
        raise RuntimeError("A browser was not launched because acquisition supplied complete job records")

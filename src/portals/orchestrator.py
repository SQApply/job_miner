from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlsplit

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
from .contracts import (
    CompletenessState,
    DiscoveryBatch,
    DiscoveryCandidate,
    DiscoveryCandidateKind,
    ScrapeStrategy,
)
from .page_quality import assess_page_quality
from .rendered_detail import RenderedDetailExtractionService
from .url_intelligence import canonicalize_candidate_url, promote_trusted_detail_url


EventCallback = Callable[[str, dict[str, Any]], None]
DiscoveryNormalizer = Callable[[list[str]], tuple[list[str], int]]
AcquiredDiscoveryNormalizer = Callable[[list[str], tuple[str, ...]], tuple[list[str], int]]
DiscoveryRanker = Callable[[list[str]], tuple[list[str], dict[str, Any]]]
DetailPlanner = Callable[[list[str]], tuple[list[str], dict[str, Any]]]
UrlValidator = Callable[[str], str]
RejectedErrorPredicate = Callable[[BaseException], bool]
ExtractedJobValidator = Callable[[JobPosting, str], tuple[bool, str]]
LlmEligibilityCallback = Callable[[Any, str], tuple[bool, str]]
LlmGroundingCallback = Callable[[JobPosting, str, Any], tuple[bool, str]]
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


def _preserve_discovery_order(urls: list[str]) -> tuple[list[str], dict[str, Any]]:
    return list(urls), {
        "strategy": "disabled",
        "input_urls": len(urls),
        "selected_urls": len(urls),
        "rejected_urls": 0,
    }


def _identity_url(url: str) -> str:
    return url


def _never_rejected(_: BaseException) -> bool:
    return False


def _default_job_validator(job: JobPosting, _: str) -> tuple[bool, str]:
    if is_valid_job(job):
        return True, "title_and_url_present"
    return False, "missing title or job URL"


def _always_allow_llm(_: Any, __: str) -> tuple[bool, str]:
    return True, "default"


def _accept_llm_payload(_: JobPosting, __: str, ___: Any) -> tuple[bool, str]:
    return True, "grounding_not_configured"


def _job_evidence_weight(job: JobPosting) -> tuple[int, int, int]:
    scalar_values = (
        job.title,
        job.apply_url,
        job.company,
        job.location_text,
        job.employment_type,
        job.duration,
        job.compensation_text,
        job.posted_date,
        job.job_reference,
    )
    return (
        sum(bool(value) for value in scalar_values)
        + len(job.responsibilities)
        + len(job.required_skills)
        + len(job.preferred_skills),
        len(str(job.summary or "")),
        sum(
            len(str(value or ""))
            for value in (
                *job.responsibilities,
                *job.required_skills,
                *job.preferred_skills,
            )
        ),
    )


def deduplicate_extracted_jobs(
    jobs: list[JobPosting],
) -> tuple[list[JobPosting], list[dict[str, Any]]]:
    """Collapse details that redirect to the same canonical job identity.

    Discovery URLs can differ while the browser resolves both to one posting.
    The richer grounded extraction wins and the duplicate remains observable in
    run metrics instead of becoming a second database job.
    """

    selected: list[JobPosting] = []
    positions: dict[str, int] = {}
    duplicates: list[dict[str, Any]] = []
    for job in jobs:
        identity = canonicalize_candidate_url(str(job.job_url or ""))
        if not identity:
            selected.append(job)
            continue
        position = positions.get(identity)
        if position is None:
            positions[identity] = len(selected)
            selected.append(job)
            continue
        existing = selected[position]
        replace_existing = _job_evidence_weight(job) > _job_evidence_weight(existing)
        if replace_existing:
            selected[position] = job
        duplicates.append(
            {
                "canonical_job_url": identity,
                "kept_title": (job.title if replace_existing else existing.title),
                "discarded_title": (existing.title if replace_existing else job.title),
                "replaced_existing": replace_existing,
            }
        )
    return selected, duplicates


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
    prefer_static_detail_html: bool = False
    enable_adaptive_dom_fallback: bool = False
    enable_rendered_detail_fallback: bool = False
    adaptive_dom_max_candidates: int = 1_000

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
        if not 1 <= self.adaptive_dom_max_candidates <= 10_000:
            raise ValueError("adaptive_dom_max_candidates must be between 1 and 10000")


@dataclass
class ScrapeOrchestratorHooks:
    """Caller-owned policy and side effects around the shared scrape engine."""

    normalize_discovered_urls: DiscoveryNormalizer = _normalize_discovered_urls
    normalize_acquired_urls: AcquiredDiscoveryNormalizer | None = None
    rank_discovered_urls: DiscoveryRanker = _preserve_discovery_order
    plan_detail_urls: DetailPlanner = _plan_all_urls
    validate_detail_url: UrlValidator = _identity_url
    is_rejected_error: RejectedErrorPredicate = _never_rejected
    validate_extracted_job: ExtractedJobValidator = _default_job_validator
    should_attempt_llm: LlmEligibilityCallback = _always_allow_llm
    validate_llm_extracted_job: LlmGroundingCallback = _accept_llm_payload
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
    discovered_candidates: list[DiscoveryCandidate] = field(default_factory=list)
    attempted_candidate_ids: list[str] = field(default_factory=list)
    discovery_batch: dict[str, Any] = field(default_factory=dict)

    @property
    def skipped_existing(self) -> int:
        return int(self.rescrape_plan.get("known_skipped") or 0)

    @property
    def linkless_candidate_count(self) -> int:
        return sum(candidate.detail_url is None for candidate in self.discovered_candidates)


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
        adaptive_dom_service: Any = None,
        rendered_detail_service: Any = None,
    ) -> None:
        self.blueprint = blueprint
        self.system_config = system_config
        self.run_session_id = run_session_id
        self.instruction = instruction or blueprint.detail.instruction
        self.adapter = adapter or get_adapter(blueprint)
        self.source_platform_hint = str(source_platform_hint or "").strip() or None
        self.acquisition_hints = dict(acquisition_hints or {})
        self.acquisition_registry = acquisition_registry or default_acquisition_registry()
        self.adaptive_dom_service = adaptive_dom_service
        self.rendered_detail_service = rendered_detail_service

    async def run(
        self,
        *,
        options: ScrapeExecutionOptions,
        hooks: ScrapeOrchestratorHooks | None = None,
        acquisition_outcome: AcquisitionOutcome | None = None,
    ) -> OrchestratedScrapeResult:
        """Execute one source scrape and avoid launching a browser for full API feeds."""
        effective_hooks = hooks or ScrapeOrchestratorHooks()
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
            url in selected.preextracted_jobs
            and effective_hooks.validate_extracted_job(selected.preextracted_jobs[url], url)[0]
            for url in selected.discovered_urls
        ):
            return await self.run_with_crawler(
                _UnavailableCrawler(),
                options=options,
                hooks=effective_hooks,
                acquisition_outcome=acquisition_outcome,
            )

        from crawl4ai import AsyncWebCrawler

        browser_config = build_browser_config(self.system_config.browser)
        async with AsyncWebCrawler(config=browser_config) as crawler:
            return await self.run_with_crawler(
                crawler,
                options=options,
                hooks=effective_hooks,
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
        preextracted_methods: dict[str, str] = {}
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
        discovery_batch: DiscoveryBatch
        raw_candidate_count = 0
        raw_url_count = 0
        if selected_acquisition is not None:
            raw_urls = list(selected_acquisition.discovered_urls)
            raw_candidate_count = len(raw_urls)
            raw_url_count = len(raw_urls)
            raw_preextracted_jobs = dict(selected_acquisition.preextracted_jobs)
            trusted_acquisition_hosts = tuple(selected_acquisition.trusted_hosts)
            normalizer = hooks.normalize_acquired_urls
            if normalizer is not None:
                discovered_urls, rejected = normalizer(list(raw_urls), trusted_acquisition_hosts)
            else:
                discovered_urls, rejected = hooks.normalize_discovered_urls(list(raw_urls))
            discovered_set = set(discovered_urls)
            for original_url, job in raw_preextracted_jobs.items():
                if normalizer is not None:
                    normalized_job_urls, _ = normalizer([original_url], trusted_acquisition_hosts)
                else:
                    normalized_job_urls, _ = hooks.normalize_discovered_urls([original_url])
                for normalized_job_url in normalized_job_urls:
                    if normalized_job_url not in discovered_set:
                        continue
                    if job.job_url == original_url and normalized_job_url != original_url:
                        job = job.model_copy(update={"job_url": normalized_job_url})
                    preextracted_jobs[normalized_job_url] = job
                    preextracted_methods[normalized_job_url] = "platform_api"
            acquisition_candidates = [
                DiscoveryCandidate.from_url(
                    url,
                    evidence={
                        "origin": "platform_acquisition",
                        "platform": selected_acquisition.platform,
                        "strategy": selected_acquisition.strategy,
                    },
                    preextracted_job=preextracted_jobs.get(url),
                )
                for url in discovered_urls
            ]
            discovery_batch = DiscoveryBatch(
                strategy=ScrapeStrategy.PLATFORM_API,
                completeness=(
                    CompletenessState.COMPLETE
                    if selected_acquisition.complete and acquisition_candidates
                    else CompletenessState.PARTIAL
                ),
                candidates=acquisition_candidates,
                pages_visited=selected_acquisition.pages_visited,
                pagination_complete=bool(
                    selected_acquisition.complete and acquisition_candidates
                ),
                metrics={
                    "origin": "platform_acquisition",
                    "endpoint_requests": selected_acquisition.endpoint_requests,
                },
            )
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
            try:
                raw_discovery_batch = await self._discover_adapter_candidates(
                    crawler,
                    hooks,
                )
            except Exception as exc:
                if not options.enable_adaptive_dom_fallback:
                    raise
                raw_discovery_batch = DiscoveryBatch(
                    strategy=ScrapeStrategy.BLUEPRINT_DOM,
                    completeness=CompletenessState.FAILED,
                    reasons=[
                        f"Existing adapter discovery failed before adaptive DOM fallback: "
                        f"{type(exc).__name__}: {exc}"[:1_000]
                    ],
                    metrics={
                        "adapter_mode": "failed_before_adaptive_dom",
                        "adapter_error_type": type(exc).__name__,
                        "adapter_error_message": str(exc)[:500],
                    },
                )
                self._emit(
                    hooks,
                    "adapter_discovery_failed_adaptive_fallback",
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                )
            raw_urls = raw_discovery_batch.discovered_urls
            raw_url_count = int(
                raw_discovery_batch.metrics.get("raw_url_candidates") or len(raw_urls)
            )
            raw_candidate_count = max(
                len(raw_discovery_batch.candidates),
                int(raw_discovery_batch.metrics.get("raw_url_candidates") or 0),
            )
            discovery_batch, rejected = self._normalize_candidate_batch(
                raw_discovery_batch,
                hooks.normalize_discovered_urls,
            )
            discovered_urls = discovery_batch.discovered_urls
            for candidate in discovery_batch.candidates:
                if candidate.detail_url and candidate.preextracted_job is not None:
                    preextracted_jobs[candidate.detail_url] = candidate.preextracted_job
                    preextracted_methods[candidate.detail_url] = str(
                        candidate.evidence.get("origin") or "adapter_preextracted"
                    )
        rejected_urls += rejected
        discovered_urls, discovery_ranking = hooks.rank_discovered_urls(discovered_urls)
        discovery_batch = self._rank_candidate_batch(discovery_batch, discovered_urls)
        if options.enable_adaptive_dom_fallback and not discovered_urls:
            self._emit(
                hooks,
                "adaptive_dom_discovery_start",
                page_url=self.blueprint.listing.page_url,
                retained_linkless_candidates=len(discovery_batch.linkless_candidates),
            )
            try:
                raw_adaptive_batch = await self._discover_adaptive_dom_candidates(options)
            except Exception as exc:
                adaptive_metrics: dict[str, Any] = {
                    "attempted": True,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                }
                acquisition = dict(acquisition)
                acquisition["adaptive_dom"] = adaptive_metrics
                self._emit(hooks, "adaptive_dom_discovery_failed", **adaptive_metrics)
            else:
                raw_candidate_count += len(raw_adaptive_batch.candidates)
                raw_url_count += len(raw_adaptive_batch.discovered_urls)
                adaptive_batch, adaptive_rejected = self._normalize_candidate_batch(
                    raw_adaptive_batch,
                    hooks.normalize_discovered_urls,
                )
                rejected_urls += adaptive_rejected
                adaptive_ranked_urls, adaptive_ranking = hooks.rank_discovered_urls(
                    adaptive_batch.discovered_urls
                )
                from .dom_discovery import preserve_evidence_backed_urls

                adaptive_ranked_urls, preservation = preserve_evidence_backed_urls(
                    adaptive_ranked_urls,
                    adaptive_batch.candidates,
                    ranking_metrics=adaptive_ranking,
                )
                adaptive_batch = self._rank_candidate_batch(
                    adaptive_batch,
                    adaptive_ranked_urls,
                )
                for candidate in adaptive_batch.candidates:
                    if candidate.detail_url and candidate.preextracted_job is not None:
                        preextracted_jobs[candidate.detail_url] = candidate.preextracted_job
                        preextracted_methods[candidate.detail_url] = str(
                            candidate.evidence.get("origin") or "adaptive_structured_evidence"
                        )
                if adaptive_batch.candidates:
                    discovery_batch = self._merge_adaptive_candidate_batch(
                        discovery_batch,
                        adaptive_batch,
                    )
                    discovered_urls = discovery_batch.discovered_urls
                adaptive_metrics = {
                    "attempted": True,
                    "status": raw_adaptive_batch.completeness.value,
                    "raw_candidates": len(raw_adaptive_batch.candidates),
                    "selected_candidates": len(adaptive_batch.candidates),
                    "selected_urls": len(adaptive_ranked_urls),
                    "linkless_candidates": len(adaptive_batch.linkless_candidates),
                    "normalization_rejected": adaptive_rejected,
                    "discovery": dict(raw_adaptive_batch.metrics),
                    "ranking": dict(adaptive_ranking),
                    **preservation,
                }
                acquisition = dict(acquisition)
                acquisition["adaptive_dom"] = adaptive_metrics
                discovery_ranking = {
                    "strategy": "adaptive_dom_fallback",
                    "initial": dict(discovery_ranking),
                    "adaptive": dict(adaptive_ranking),
                    **preservation,
                }
                self._emit(hooks, "adaptive_dom_discovery_complete", **adaptive_metrics)
        discovered_candidates = list(discovery_batch.candidates)
        linkless_candidates = list(discovery_batch.linkless_candidates)
        self._emit(
            hooks,
            "discovery_complete",
            raw_discovered_urls=raw_url_count,
            raw_discovered_candidates=raw_candidate_count,
            discovered_urls=len(discovered_urls),
            discovered_candidates=len(discovered_candidates),
            linkless_candidates=len(linkless_candidates),
            rejected_urls=rejected_urls,
        )
        self._emit(hooks, "discovery_ranked", **discovery_ranking)

        if not discovered_candidates and options.fail_on_zero_discovery:
            self._emit(
                hooks,
                "discovery_failed_zero_urls",
                page_url=self.blueprint.listing.page_url,
                message=(
                    "A job portal discovery run returned zero valid job URLs or "
                    "linkless candidates and is not complete."
                ),
            )
            raise RuntimeError(
                f"Target {self.blueprint.id} discovered zero valid job URLs or candidates"
            )

        if linkless_candidates:
            self._emit(
                hooks,
                "linkless_candidates_deferred",
                candidates=len(linkless_candidates),
                candidate_ids=[candidate.candidate_id for candidate in linkless_candidates],
                reason=(
                    "Structural discovery retained linkless candidates; bounded click/modal "
                    "interaction is handled by Phase 7C2."
                ),
            )

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
        rescrape_plan["discovered_candidates"] = len(discovered_candidates)
        rescrape_plan["linkless_candidates_deferred"] = len(linkless_candidates)
        rescrape_plan["urls_to_extract_count"] = len(attempted_urls)
        rescrape_plan["url_ranking"] = dict(discovery_ranking)
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

        candidate_by_url = {
            candidate.detail_url: candidate
            for candidate in discovered_candidates
            if candidate.detail_url is not None
        }
        attempted_candidate_ids = [
            candidate_by_url[url].candidate_id
            for url in attempted_urls
            if url in candidate_by_url
        ]

        concurrency = max(1, min(options.detail_concurrency, len(attempted_urls) or 1))
        semaphore = asyncio.Semaphore(concurrency)
        rate_limiter = _RateLimiter(options.requests_per_minute)
        rendered_detail_service = self.rendered_detail_service
        if options.enable_rendered_detail_fallback and rendered_detail_service is None:
            rendered_detail_service = RenderedDetailExtractionService(
                self.system_config.browser
            )
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
                if acquired_job is not None:
                    acquired_valid, acquired_reason = hooks.validate_extracted_job(
                        acquired_job,
                        original_job_url,
                    )
                    if acquired_valid:
                        self._emit(
                            hooks,
                            "extract_saved",
                            job_url=original_job_url,
                            item_index=item_index,
                            attempt=0,
                            elapsed_seconds=0.0,
                            title=acquired_job.title,
                            extraction_method=preextracted_methods.get(
                                original_job_url,
                                "preextracted_evidence",
                            ),
                            validation_reason=acquired_reason,
                        )
                        return acquired_job
                    self._emit(
                        hooks,
                        "validation_failed",
                        job_url=original_job_url,
                        item_index=item_index,
                        attempt=0,
                        extraction_method=preextracted_methods.get(
                            original_job_url,
                            "preextracted_evidence",
                        ),
                        validation_reason=acquired_reason,
                    )
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
                        promoted_job_url = promote_trusted_detail_url(
                            original_job_url,
                            platform_hint=self.source_platform_hint,
                            acquisition_hints=self.acquisition_hints,
                        )
                        try:
                            job_url = hooks.validate_detail_url(promoted_job_url)
                        except Exception as promotion_exc:
                            if promoted_job_url == original_job_url:
                                raise
                            self._emit(
                                hooks,
                                "detail_url_promotion_rejected",
                                job_url=original_job_url,
                                promoted_url=promoted_job_url,
                                item_index=item_index,
                                attempt=attempt,
                                error_message=str(promotion_exc),
                            )
                            job_url = hooks.validate_detail_url(original_job_url)
                        if job_url != original_job_url:
                            self._emit(
                                hooks,
                                "detail_url_promoted",
                                job_url=original_job_url,
                                promoted_url=job_url,
                                item_index=item_index,
                                attempt=attempt,
                                evidence_source="acquisition_hints",
                            )

                        if attempt == 1 and options.prefer_static_detail_html:
                            request_text = getattr(self.acquisition_registry.client, "request_text", None)
                            if callable(request_text):
                                static_started = time.perf_counter()
                                self._emit(
                                    hooks,
                                    "static_detail_start",
                                    job_url=job_url,
                                    item_index=item_index,
                                )
                                try:
                                    await rate_limiter.wait()
                                    static_html = await request_text(
                                        job_url,
                                        timeout_seconds=options.acquisition_timeout_seconds,
                                    )
                                except Exception as static_exc:
                                    self._emit(
                                        hooks,
                                        "static_detail_failed",
                                        job_url=job_url,
                                        item_index=item_index,
                                        elapsed_seconds=round(
                                            time.perf_counter() - static_started,
                                            3,
                                        ),
                                        error_type=type(static_exc).__name__,
                                        error_message=str(static_exc)[:500],
                                    )
                                else:
                                    static_quality = assess_page_quality(html=static_html)
                                    if static_quality.blocked:
                                        self._emit(
                                            hooks,
                                            "static_detail_rejected",
                                            job_url=job_url,
                                            item_index=item_index,
                                            reason=static_quality.reason,
                                            page_quality=static_quality.to_dict(),
                                        )
                                    else:
                                        static_result = SimpleNamespace(
                                            success=True,
                                            url=job_url,
                                            html=str(static_html or ""),
                                            cleaned_html="",
                                            error_message=None,
                                        )
                                        static_job = extract_job_from_result(static_result, job_url)
                                        if static_job is not None:
                                            static_valid, static_reason = hooks.validate_extracted_job(
                                                static_job,
                                                job_url,
                                            )
                                            if static_valid:
                                                self._emit(
                                                    hooks,
                                                    "extract_saved",
                                                    job_url=job_url,
                                                    item_index=item_index,
                                                    attempt=0,
                                                    elapsed_seconds=round(
                                                        time.perf_counter() - static_started,
                                                        3,
                                                    ),
                                                    title=static_job.title,
                                                    extraction_method="static_deterministic",
                                                    validation_reason=static_reason,
                                                )
                                                return static_job
                                            self._emit(
                                                hooks,
                                                "validation_failed",
                                                job_url=job_url,
                                                item_index=item_index,
                                                attempt=0,
                                                extraction_method="static_deterministic",
                                                validation_reason=static_reason,
                                            )
                                        else:
                                            self._emit(
                                                hooks,
                                                "static_detail_deterministic_miss",
                                                job_url=job_url,
                                                item_index=item_index,
                                                elapsed_seconds=round(
                                                    time.perf_counter() - static_started,
                                                    3,
                                                ),
                                                page_quality=static_quality.to_dict(),
                                            )

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
                            if attempt == 1 and rendered_detail_service is not None:
                                rendered_job = await self._try_rendered_detail(
                                    rendered_detail_service,
                                    hooks=hooks,
                                    rate_limiter=rate_limiter,
                                    job_url=job_url,
                                    original_job_url=original_job_url,
                                    candidate=candidate_by_url.get(original_job_url),
                                    item_index=item_index,
                                    attempt=attempt,
                                )
                                if rendered_job is not None:
                                    return rendered_job
                        else:
                            final_url = self._result_final_url(result, job_url)
                            final_url = hooks.validate_detail_url(final_url)
                            job = extract_job_from_result(result, final_url)
                            if job is not None:
                                deterministic_valid, deterministic_reason = hooks.validate_extracted_job(
                                    job,
                                    final_url,
                                )
                                if deterministic_valid:
                                    self._emit(
                                        hooks,
                                        "extract_saved",
                                        job_url=job_url,
                                        item_index=item_index,
                                        attempt=attempt,
                                        elapsed_seconds=elapsed,
                                        title=job.title,
                                        extraction_method="deterministic",
                                        validation_reason=deterministic_reason,
                                    )
                                    return job
                                self._emit(
                                    hooks,
                                    "validation_failed",
                                    job_url=job_url,
                                    item_index=item_index,
                                    attempt=attempt,
                                    extraction_method="deterministic",
                                    validation_reason=deterministic_reason,
                                )

                            rendered_content = "\n".join(
                                str(getattr(result, field, "") or "")
                                for field in ("html", "cleaned_html", "markdown")
                            )
                            rendered_promotion = promote_trusted_detail_url(
                                original_job_url,
                                platform_hint=self.source_platform_hint,
                                acquisition_hints=self.acquisition_hints,
                                page_content=rendered_content,
                            )
                            if rendered_promotion != job_url:
                                try:
                                    checked_promotion = hooks.validate_detail_url(
                                        rendered_promotion
                                    )
                                except Exception as promotion_exc:
                                    self._emit(
                                        hooks,
                                        "detail_url_promotion_rejected",
                                        job_url=job_url,
                                        promoted_url=rendered_promotion,
                                        item_index=item_index,
                                        attempt=attempt,
                                        error_message=str(promotion_exc),
                                    )
                                else:
                                    self._emit(
                                        hooks,
                                        "detail_url_promoted",
                                        job_url=job_url,
                                        promoted_url=checked_promotion,
                                        item_index=item_index,
                                        attempt=attempt,
                                        evidence_source="rendered_document",
                                    )
                                    await rate_limiter.wait()
                                    promoted_result = await crawler.arun(
                                        url=checked_promotion,
                                        config=detail_run_config(
                                            self.system_config.browser,
                                            resilient_detail_wait(
                                                self.blueprint.detail.wait_for
                                            ),
                                            None,
                                            session_id=detail_session_id,
                                        ),
                                    )
                                    if not getattr(promoted_result, "success", False):
                                        raise RuntimeError(
                                            "Embedded detail document acquisition failed: "
                                            f"{getattr(promoted_result, 'error_message', 'unknown error')}"
                                        )
                                    result = promoted_result
                                    job_url = checked_promotion
                                    final_url = self._result_final_url(result, job_url)
                                    final_url = hooks.validate_detail_url(final_url)
                                    job = extract_job_from_result(result, final_url)
                                    if job is not None:
                                        promoted_valid, promoted_reason = (
                                            hooks.validate_extracted_job(job, final_url)
                                        )
                                        if promoted_valid:
                                            self._emit(
                                                hooks,
                                                "extract_saved",
                                                job_url=job_url,
                                                item_index=item_index,
                                                attempt=attempt,
                                                elapsed_seconds=round(
                                                    time.perf_counter() - attempt_started,
                                                    3,
                                                ),
                                                title=job.title,
                                                extraction_method="deterministic_embedded_document",
                                                validation_reason=promoted_reason,
                                            )
                                            return job
                                        self._emit(
                                            hooks,
                                            "validation_failed",
                                            job_url=job_url,
                                            item_index=item_index,
                                            attempt=attempt,
                                            extraction_method="deterministic_embedded_document",
                                            validation_reason=promoted_reason,
                                        )

                            if attempt == 1 and rendered_detail_service is not None:
                                rendered_job = await self._try_rendered_detail(
                                    rendered_detail_service,
                                    hooks=hooks,
                                    rate_limiter=rate_limiter,
                                    job_url=final_url,
                                    original_job_url=original_job_url,
                                    candidate=candidate_by_url.get(original_job_url),
                                    item_index=item_index,
                                    attempt=attempt,
                                )
                                if rendered_job is not None:
                                    return rendered_job

                            llm_allowed, llm_reason = hooks.should_attempt_llm(result, final_url)
                            if not llm_allowed:
                                last_error = f"LLM skipped: {llm_reason}"
                                retryable = False
                                self._emit(
                                    hooks,
                                    "llm_fallback_skipped_non_job",
                                    job_url=job_url,
                                    item_index=item_index,
                                    attempt=attempt,
                                    reason=llm_reason,
                                )
                                break

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
                                if job is not None:
                                    llm_grounded, llm_grounding_reason = (
                                        hooks.validate_llm_extracted_job(job, final_url, result)
                                    )
                                    if not llm_grounded:
                                        llm_valid = False
                                        llm_validation_reason = llm_grounding_reason
                                        self._emit(
                                            hooks,
                                            "llm_grounding_failed",
                                            job_url=job_url,
                                            item_index=item_index,
                                            attempt=attempt,
                                            parsed_title=job.title,
                                            validation_reason=llm_grounding_reason,
                                        )
                                    else:
                                        llm_valid, schema_reason = hooks.validate_extracted_job(
                                            job,
                                            final_url,
                                        )
                                        llm_validation_reason = (
                                            f"{llm_grounding_reason};{schema_reason}"
                                        )
                                    if llm_valid:
                                        self._emit(
                                            hooks,
                                            "extract_saved",
                                            job_url=job_url,
                                            item_index=item_index,
                                            attempt=attempt,
                                            elapsed_seconds=elapsed,
                                            title=job.title,
                                            extraction_method="llm_fallback",
                                            validation_reason=llm_validation_reason,
                                        )
                                        return job
                                else:
                                    llm_validation_reason = "LLM payload could not be parsed"

                                last_error = (
                                    "Deterministic and LLM extraction returned no certifiable job: "
                                    f"{llm_validation_reason}"
                                )
                                self._save_failed_payload(hooks, job_url, raw_content)
                                self._emit(
                                    hooks,
                                    "validation_failed" if job is not None else "parse_failed",
                                    job_url=job_url,
                                    item_index=item_index,
                                    attempt=attempt,
                                    elapsed_seconds=elapsed,
                                    parsed_title=getattr(job, "title", None),
                                    validation_reason=llm_validation_reason,
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
        raw_jobs: list[JobPosting] = []
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
                raw_jobs.append(item)

        jobs, extracted_duplicates = deduplicate_extracted_jobs(raw_jobs)
        for duplicate in extracted_duplicates:
            self._emit(hooks, "extracted_duplicate_collapsed", **duplicate)
        rescrape_plan["raw_extracted_jobs"] = len(raw_jobs)
        rescrape_plan["extracted_duplicate_urls_collapsed"] = len(extracted_duplicates)

        elapsed_seconds = round(time.perf_counter() - started, 3)
        self._emit(
            hooks,
            "parallel_extraction_complete",
            attempted_urls=len(attempted_urls),
            discovered_urls=len(discovered_urls),
            extracted_jobs=len(jobs),
            raw_extracted_jobs=len(raw_jobs),
            extracted_duplicate_urls_collapsed=len(extracted_duplicates),
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
            raw_discovered_urls=raw_url_count,
            acquisition=acquisition,
            discovered_candidates=discovered_candidates,
            attempted_candidate_ids=attempted_candidate_ids,
            discovery_batch=discovery_batch.model_dump(mode="json"),
        )

    async def _discover_adapter_candidates(
        self,
        crawler: Any,
        hooks: ScrapeOrchestratorHooks,
    ) -> DiscoveryBatch:
        discover_candidates = getattr(self.adapter, "discover_candidates", None)
        if callable(discover_candidates):
            batch = await discover_candidates(
                crawler,
                self.blueprint,
                self.system_config,
                session_logger=hooks.adapter_session_logger,
            )
            if not isinstance(batch, DiscoveryBatch):
                raise TypeError("discover_candidates() must return DiscoveryBatch")
            return batch

        raw_urls = await self.adapter.discover_job_urls(
            crawler,
            self.blueprint,
            self.system_config,
            session_logger=hooks.adapter_session_logger,
        )
        return DiscoveryBatch.from_urls(
            list(raw_urls or []),
            strategy=ScrapeStrategy.BLUEPRINT_DOM,
            metrics={"adapter_mode": "legacy_url_projection"},
        )

    async def _discover_adaptive_dom_candidates(
        self,
        options: ScrapeExecutionOptions,
    ) -> DiscoveryBatch:
        service = self.adaptive_dom_service
        if service is None:
            from .adaptive_dom import AdaptiveDomDiscoveryService

            service = AdaptiveDomDiscoveryService(self.system_config.browser)
        batch = await service.discover(
            str(self.blueprint.listing.page_url),
            allowed_hosts=tuple(self.blueprint.allowed_hosts),
            max_candidates=options.adaptive_dom_max_candidates,
        )
        if not isinstance(batch, DiscoveryBatch):
            raise TypeError("adaptive DOM discover() must return DiscoveryBatch")
        return batch

    @staticmethod
    def _normalize_candidate_batch(
        batch: DiscoveryBatch,
        normalizer: DiscoveryNormalizer,
    ) -> tuple[DiscoveryBatch, int]:
        normalized_candidates: list[DiscoveryCandidate] = []
        seen_urls: set[str] = set()
        rejected = int(batch.metrics.get("deduplicated_url_candidates") or 0) + int(
            batch.metrics.get("invalid_url_candidates") or 0
        )

        for candidate in batch.candidates:
            if candidate.detail_url is None:
                normalized_candidates.append(candidate)
                continue

            normalized_urls, local_rejected = normalizer([candidate.detail_url])
            normalized_urls = [str(url).strip() for url in normalized_urls if str(url).strip()]
            if not normalized_urls:
                rejected += max(1, int(local_rejected or 0))
                continue

            for normalized_url in normalized_urls:
                if normalized_url in seen_urls:
                    rejected += 1
                    continue
                seen_urls.add(normalized_url)
                update: dict[str, Any] = {"detail_url": normalized_url}
                if (
                    candidate.kind == DiscoveryCandidateKind.URL
                    and normalized_url != candidate.detail_url
                ):
                    update["candidate_id"] = DiscoveryCandidate.from_url(
                        normalized_url,
                        source_job_id=candidate.source_job_id,
                    ).candidate_id
                if (
                    candidate.preextracted_job is not None
                    and candidate.preextracted_job.job_url == candidate.detail_url
                    and normalized_url != candidate.detail_url
                ):
                    update["preextracted_job"] = candidate.preextracted_job.model_copy(
                        update={"job_url": normalized_url}
                    )
                normalized_candidates.append(candidate.model_copy(update=update))

        metrics = dict(batch.metrics)
        metrics.update(
            {
                "normalized_candidates": len(normalized_candidates),
                "normalization_rejected": rejected,
            }
        )
        has_candidates = bool(normalized_candidates)
        return (
            DiscoveryBatch(
                batch_version=batch.batch_version,
                strategy=batch.strategy,
                completeness=(
                    batch.completeness if has_candidates else CompletenessState.PARTIAL
                ),
                candidates=normalized_candidates,
                pages_visited=batch.pages_visited,
                pagination_complete=batch.pagination_complete if has_candidates else False,
                reasons=list(batch.reasons),
                metrics=metrics,
            ),
            rejected,
        )

    @staticmethod
    def _rank_candidate_batch(
        batch: DiscoveryBatch,
        ranked_urls: list[str],
    ) -> DiscoveryBatch:
        candidate_by_url = {
            candidate.detail_url: candidate
            for candidate in batch.candidates
            if candidate.detail_url is not None
        }
        ranked_candidates = [
            candidate_by_url.get(url) or DiscoveryCandidate.from_url(url)
            for url in ranked_urls
        ]
        ranked_candidates.extend(batch.linkless_candidates)
        has_candidates = bool(ranked_candidates)
        completeness = batch.completeness if has_candidates else CompletenessState.PARTIAL
        metrics = dict(batch.metrics)
        metrics.update(
            {
                "ranked_url_candidates": len(ranked_urls),
                "linkless_candidates": len(batch.linkless_candidates),
            }
        )
        return DiscoveryBatch(
            batch_version=batch.batch_version,
            strategy=batch.strategy,
            completeness=completeness,
            candidates=ranked_candidates,
            pages_visited=batch.pages_visited,
            pagination_complete=batch.pagination_complete if has_candidates else False,
            reasons=list(batch.reasons),
            metrics=metrics,
        )

    @staticmethod
    def _merge_adaptive_candidate_batch(
        retained_batch: DiscoveryBatch,
        adaptive_batch: DiscoveryBatch,
    ) -> DiscoveryBatch:
        candidates: list[DiscoveryCandidate] = []
        identities: set[str] = set()
        for candidate in [*adaptive_batch.candidates, *retained_batch.linkless_candidates]:
            identity = candidate.identity_key
            if identity in identities:
                continue
            identities.add(identity)
            candidates.append(candidate)
        metrics = dict(adaptive_batch.metrics)
        metrics["retained_prior_linkless_candidates"] = len(
            retained_batch.linkless_candidates
        )
        return DiscoveryBatch(
            batch_version=adaptive_batch.batch_version,
            strategy=adaptive_batch.strategy,
            completeness=CompletenessState.PARTIAL,
            candidates=candidates,
            pages_visited=adaptive_batch.pages_visited,
            pagination_complete=False,
            reasons=list(dict.fromkeys([*retained_batch.reasons, *adaptive_batch.reasons])),
            metrics=metrics,
        )

    async def _try_rendered_detail(
        self,
        service: Any,
        *,
        hooks: ScrapeOrchestratorHooks,
        rate_limiter: _RateLimiter,
        job_url: str,
        original_job_url: str,
        candidate: DiscoveryCandidate | None,
        item_index: int,
        attempt: int,
    ) -> JobPosting | None:
        started = time.perf_counter()
        self._emit(
            hooks,
            "rendered_detail_start",
            job_url=job_url,
            original_job_url=original_job_url,
            item_index=item_index,
            attempt=attempt,
        )
        try:
            await rate_limiter.wait()
            outcome = await service.extract(
                job_url,
                allowed_hosts=self._detail_allowed_hosts(job_url),
                title_hint=candidate.title_hint if candidate is not None else None,
                location_hint=candidate.location_hint if candidate is not None else None,
            )
        except Exception as exc:
            self._emit(
                hooks,
                "rendered_detail_failed",
                job_url=job_url,
                original_job_url=original_job_url,
                item_index=item_index,
                attempt=attempt,
                elapsed_seconds=round(time.perf_counter() - started, 3),
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
            )
            return None

        job = getattr(outcome, "job", None)
        metrics = dict(getattr(outcome, "metrics", {}) or {})
        reason = str(getattr(outcome, "reason", "rendered detail miss"))
        if job is None:
            self._emit(
                hooks,
                "rendered_detail_miss",
                job_url=job_url,
                original_job_url=original_job_url,
                item_index=item_index,
                attempt=attempt,
                elapsed_seconds=round(time.perf_counter() - started, 3),
                reason=reason,
                metrics=metrics,
            )
            return None

        validation_url = str(job.job_url or job_url)
        valid, validation_reason = hooks.validate_extracted_job(job, validation_url)
        if not valid:
            self._emit(
                hooks,
                "validation_failed",
                job_url=validation_url,
                original_job_url=original_job_url,
                item_index=item_index,
                attempt=attempt,
                extraction_method="rendered_semantic_dom",
                validation_reason=validation_reason,
                rendered_reason=reason,
            )
            return None

        self._emit(
            hooks,
            "extract_saved",
            job_url=validation_url,
            original_job_url=original_job_url,
            item_index=item_index,
            attempt=attempt,
            elapsed_seconds=round(time.perf_counter() - started, 3),
            title=job.title,
            extraction_method="rendered_semantic_dom",
            validation_reason=validation_reason,
            rendered_reason=reason,
            rendered_metrics=metrics,
        )
        return job

    def _detail_allowed_hosts(self, job_url: str) -> tuple[str, ...]:
        hostname = str(urlsplit(job_url).hostname or "").lower().rstrip(".")
        return tuple(
            dict.fromkeys(
                str(value).strip().lower().rstrip(".")
                for value in [*self.blueprint.allowed_hosts, hostname]
                if str(value).strip()
            )
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
            max_records=options.max_jobs,
        )


class _UnavailableCrawler:
    async def arun(self, **_: Any) -> Any:
        raise RuntimeError("A browser was not launched because acquisition supplied complete job records")

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..schemas import JobPosting, ResolvedBlueprint


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ContractModel(BaseModel):
    """Strict base model for versioned scraper orchestration contracts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RunMode(str, Enum):
    PROBE = "probe"
    CERTIFICATION = "certification"
    PRODUCTION = "production"
    REPAIR = "repair"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


class CompletenessState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"
    EMPTY_CONFIRMED = "empty_confirmed"


class SourceAccessPolicy(str, Enum):
    PUBLIC = "public"
    AUTHORIZED_SESSION = "authorized_session"
    LICENSED_FEED = "licensed_feed"
    REVIEW_REQUIRED = "review_required"
    PROHIBITED = "prohibited"


class ScrapeStrategy(str, Enum):
    PLATFORM_API = "platform_api"
    NETWORK_JSON = "network_json"
    JSON_LD = "json_ld"
    INLINE_JSON = "inline_json"
    STATIC_HTML = "static_html"
    BLUEPRINT_DOM = "blueprint_dom"
    CRAWL4AI = "crawl4ai"
    LLM_FALLBACK = "llm_fallback"


class CertificationStatus(str, Enum):
    PASSED = "passed"
    SOURCE_EXHAUSTED = "source_exhausted"
    ACCESS_BLOCKED = "access_blocked"
    LICENSE_REQUIRED = "license_required"
    NEEDS_REPAIR = "needs_repair"
    FAILED = "failed"


class DiscoveryCandidateKind(str, Enum):
    """How a discovered job can be opened or extracted.

    URL preserves the existing scraper behavior.  The remaining kinds are the
    stable contract needed by the Phase 7 browser-evidence lanes; importantly,
    none of them requires an XPath or even a detail URL.
    """

    URL = "url"
    API_RECORD = "api_record"
    DOM_CLICK = "dom_click"
    MODAL = "modal"
    INLINE = "inline"
    FRAME = "frame"


DEFAULT_STRATEGY_ORDER = [
    ScrapeStrategy.PLATFORM_API,
    ScrapeStrategy.NETWORK_JSON,
    ScrapeStrategy.JSON_LD,
    ScrapeStrategy.INLINE_JSON,
    ScrapeStrategy.STATIC_HTML,
    ScrapeStrategy.BLUEPRINT_DOM,
    ScrapeStrategy.CRAWL4AI,
    ScrapeStrategy.LLM_FALLBACK,
]


def _validate_http_url(value: str, field_name: str) -> str:
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
    return value


def _validate_aware_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value


class SourceLimits(ContractModel):
    requests_per_minute: int = Field(default=10, ge=1, le=600)
    domain_concurrency: int = Field(default=1, ge=1, le=20)
    max_pages_per_run: int = Field(default=50, ge=1, le=5000)
    max_jobs_per_run: int = Field(default=500, ge=1, le=100000)
    detail_retry_attempts: int = Field(default=2, ge=0, le=5)
    crawl_timeout_seconds: int = Field(default=1800, ge=30, le=86400)


class SourceLifecyclePolicy(ContractModel):
    refresh_interval_minutes: int = Field(default=4320, ge=60, le=43200)
    deactivate_after_complete_misses: int = Field(default=2, ge=2, le=10)
    min_discovery_coverage_ratio: float = Field(default=0.25, ge=0.0, le=1.0)
    deep_refresh_days: int = Field(default=14, ge=1, le=365)
    hard_delete_enabled: bool = False

    @model_validator(mode="after")
    def reject_hard_delete(self) -> "SourceLifecyclePolicy":
        if self.hard_delete_enabled:
            raise ValueError("Production source contracts must retain job history; hard deletion is not supported")
        return self


class SourceIdentityPolicy(ContractModel):
    primary_fields: list[str] = Field(
        default_factory=lambda: ["source_platform", "ats_tenant", "source_job_id"]
    )
    preserve_url_fragment: bool = False
    retain_url_aliases: bool = True

    @field_validator("primary_fields")
    @classmethod
    def validate_primary_fields(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item and item.strip()]
        if not normalized:
            raise ValueError("identity.primary_fields cannot be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("identity.primary_fields cannot contain duplicates")
        return normalized


class SourceContract(ContractModel):
    source_id: str = Field(min_length=2, max_length=128)
    contract_version: int = Field(default=1, ge=1)
    display_name: str | None = Field(default=None, max_length=300)
    provided_url: str
    listing_url: str
    allowed_hosts: list[str] = Field(min_length=1)
    source_platform: str = Field(default="unknown", min_length=1, max_length=100)
    ats_tenant: str | None = Field(default=None, max_length=200)
    profile_name: str | None = Field(default=None, max_length=100)
    access_policy: SourceAccessPolicy = SourceAccessPolicy.PUBLIC
    strategy_order: list[ScrapeStrategy] = Field(default_factory=lambda: list(DEFAULT_STRATEGY_ORDER))
    identity: SourceIdentityPolicy = Field(default_factory=SourceIdentityPolicy)
    limits: SourceLimits = Field(default_factory=SourceLimits)
    lifecycle: SourceLifecyclePolicy = Field(default_factory=SourceLifecyclePolicy)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,127}", normalized):
            raise ValueError("source_id must contain only lowercase letters, numbers, underscores, and hyphens")
        return normalized

    @field_validator("provided_url")
    @classmethod
    def validate_provided_url(cls, value: str) -> str:
        return _validate_http_url(value, "provided_url")

    @field_validator("listing_url")
    @classmethod
    def validate_listing_url(cls, value: str) -> str:
        return _validate_http_url(value, "listing_url")

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_allowed_hosts(cls, value: list[str]) -> list[str]:
        hosts: list[str] = []
        for raw in value:
            host = str(raw or "").strip().lower().rstrip(".")
            if host.startswith("http://") or host.startswith("https://"):
                host = (urlsplit(host).hostname or "").lower()
            if host and host not in hosts:
                hosts.append(host)
        if not hosts:
            raise ValueError("allowed_hosts must contain at least one host")
        return hosts

    @field_validator("strategy_order")
    @classmethod
    def validate_strategy_order(cls, value: list[ScrapeStrategy]) -> list[ScrapeStrategy]:
        if not value:
            raise ValueError("strategy_order cannot be empty")
        if len(value) != len(set(value)):
            raise ValueError("strategy_order cannot contain duplicate strategies")
        return value

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def source_contract_from_blueprint(blueprint: ResolvedBlueprint) -> SourceContract:
    """Convert an existing resolved YAML blueprint without changing its behavior."""

    listing_url = str(blueprint.listing.page_url)
    return SourceContract(
        source_id=blueprint.id,
        display_name=blueprint.label,
        provided_url=listing_url,
        listing_url=listing_url,
        allowed_hosts=list(blueprint.allowed_hosts),
        profile_name=blueprint.adapter,
        identity=SourceIdentityPolicy(preserve_url_fragment="#" in listing_url),
        metadata={
            "legacy_adapter": blueprint.adapter,
            "legacy_output_file": blueprint.output_file,
        },
    )


def _candidate_id(kind: DiscoveryCandidateKind, identity: str) -> str:
    digest = hashlib.sha256(f"{kind.value}:{identity}".encode("utf-8")).hexdigest()[:24]
    return f"{kind.value}_{digest}"


class DiscoveryCandidate(ContractModel):
    """A job-like unit discovered from a URL, API response, or live DOM.

    ``node_token`` is an ephemeral browser-session handle, not a persisted CSS
    selector or XPath.  ``candidate_id`` is supplied by native discovery lanes
    from stable evidence (for example an external id or structural signature).
    """

    candidate_id: str = Field(min_length=3, max_length=200)
    kind: DiscoveryCandidateKind
    detail_url: str | None = Field(default=None, max_length=4096)
    apply_url: str | None = Field(default=None, max_length=4096)
    source_job_id: str | None = Field(default=None, max_length=500)
    title_hint: str | None = Field(default=None, max_length=1000)
    location_hint: str | None = Field(default=None, max_length=1000)
    node_token: str | None = Field(default=None, max_length=500)
    frame_url: str | None = Field(default=None, max_length=4096)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    preextracted_job: JobPosting | None = None

    @field_validator(
        "detail_url",
        "apply_url",
        "source_job_id",
        "title_hint",
        "location_hint",
        "node_token",
        "frame_url",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("detail_url")
    @classmethod
    def validate_detail_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_http_url(value, "detail_url")

    @field_validator("apply_url")
    @classmethod
    def validate_apply_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_http_url(value, "apply_url")

    @field_validator("frame_url")
    @classmethod
    def validate_frame_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_http_url(value, "frame_url")

    @model_validator(mode="after")
    def validate_candidate(self) -> "DiscoveryCandidate":
        if self.kind == DiscoveryCandidateKind.URL and not self.detail_url:
            raise ValueError("URL discovery candidates must include detail_url")
        if self.kind == DiscoveryCandidateKind.FRAME and not self.frame_url:
            raise ValueError("FRAME discovery candidates must include frame_url")
        if self.kind in {
            DiscoveryCandidateKind.DOM_CLICK,
            DiscoveryCandidateKind.MODAL,
            DiscoveryCandidateKind.INLINE,
        } and not (self.node_token or self.preextracted_job):
            raise ValueError(
                "interactive DOM candidates must include node_token or preextracted_job"
            )
        if not any(
            (
                self.detail_url,
                self.source_job_id,
                self.node_token,
                self.frame_url,
                self.title_hint,
                self.preextracted_job,
            )
        ):
            raise ValueError("discovery candidates must contain at least one identity signal")
        return self

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        source_job_id: str | None = None,
        confidence: float = 1.0,
        evidence: dict[str, Any] | None = None,
        preextracted_job: JobPosting | None = None,
    ) -> "DiscoveryCandidate":
        normalized_url = _validate_http_url(str(url).strip(), "detail_url")
        identity = str(source_job_id or normalized_url)
        return cls(
            candidate_id=_candidate_id(DiscoveryCandidateKind.URL, identity),
            kind=DiscoveryCandidateKind.URL,
            detail_url=normalized_url,
            source_job_id=source_job_id,
            confidence=confidence,
            evidence=dict(evidence or {}),
            preextracted_job=preextracted_job,
        )

    @property
    def identity_key(self) -> str:
        if self.source_job_id:
            return f"source_job_id:{self.source_job_id}"
        if self.detail_url:
            return f"detail_url:{self.detail_url}"
        return f"candidate_id:{self.candidate_id}"


class DiscoveryBatch(ContractModel):
    """Versioned result of one discovery lane.

    A batch may contain both URL-backed and linkless jobs.  Completeness remains
    explicit so a bounded browser pass can never become reconciliation evidence.
    """

    batch_version: int = Field(default=1, ge=1)
    strategy: ScrapeStrategy
    completeness: CompletenessState = CompletenessState.PARTIAL
    candidates: list[DiscoveryCandidate] = Field(default_factory=list)
    pages_visited: int = Field(default=0, ge=0)
    pagination_complete: bool = False
    reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_batch(self) -> "DiscoveryBatch":
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("DiscoveryBatch candidate_id values must be unique")
        if self.completeness in {CompletenessState.COMPLETE, CompletenessState.EMPTY_CONFIRMED}:
            if not self.pagination_complete:
                raise ValueError("complete discovery batches must confirm pagination completion")
        if self.completeness == CompletenessState.COMPLETE and not self.candidates:
            raise ValueError("zero-candidate discovery must use EMPTY_CONFIRMED")
        if self.completeness == CompletenessState.EMPTY_CONFIRMED:
            if self.candidates:
                raise ValueError("EMPTY_CONFIRMED discovery batches cannot contain candidates")
            if not self.reasons:
                raise ValueError("EMPTY_CONFIRMED discovery batches require independent evidence")
        if self.completeness in {CompletenessState.BLOCKED, CompletenessState.FAILED} and not self.reasons:
            raise ValueError("blocked and failed discovery batches must include a reason")
        return self

    @classmethod
    def from_urls(
        cls,
        urls: list[str],
        *,
        strategy: ScrapeStrategy = ScrapeStrategy.BLUEPRINT_DOM,
        completeness: CompletenessState = CompletenessState.PARTIAL,
        pages_visited: int = 0,
        pagination_complete: bool = False,
        reasons: list[str] | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> "DiscoveryBatch":
        raw_urls = [str(url).strip() for url in urls if str(url).strip()]
        unique_urls = list(dict.fromkeys(raw_urls))
        candidates: list[DiscoveryCandidate] = []
        invalid_urls = 0
        for url in unique_urls:
            try:
                candidates.append(cls._url_candidate(url))
            except (TypeError, ValueError):
                invalid_urls += 1
        batch_metrics = dict(metrics or {})
        batch_metrics.setdefault("raw_url_candidates", len(raw_urls))
        batch_metrics.setdefault("deduplicated_url_candidates", len(raw_urls) - len(unique_urls))
        batch_metrics.setdefault("invalid_url_candidates", invalid_urls)
        return cls(
            strategy=strategy,
            completeness=completeness,
            candidates=candidates,
            pages_visited=pages_visited,
            pagination_complete=pagination_complete,
            reasons=list(reasons or []),
            metrics=batch_metrics,
        )

    @staticmethod
    def _url_candidate(url: str) -> DiscoveryCandidate:
        return DiscoveryCandidate.from_url(url, evidence={"origin": "legacy_url_adapter"})

    @property
    def discovered_urls(self) -> list[str]:
        return list(
            dict.fromkeys(
                candidate.detail_url
                for candidate in self.candidates
                if candidate.detail_url is not None
            )
        )

    @property
    def linkless_candidates(self) -> list[DiscoveryCandidate]:
        return [candidate for candidate in self.candidates if candidate.detail_url is None]


class DiscoveryManifest(ContractModel):
    run_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=2, max_length=128)
    contract_version: int = Field(ge=1)
    strategy: ScrapeStrategy
    completeness: CompletenessState
    discovered_count: int = Field(default=0, ge=0)
    discovered_candidate_ids: list[str] = Field(default_factory=list)
    discovered_source_job_ids: list[str] = Field(default_factory=list)
    discovered_urls: list[str] = Field(default_factory=list)
    pages_visited: int = Field(default=0, ge=0)
    pagination_complete: bool = False
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_id")
    @classmethod
    def normalize_source_id(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("discovered_source_job_ids")
    @classmethod
    def validate_unique_job_ids(cls, value: list[str]) -> list[str]:
        normalized = [str(item).strip() for item in value if str(item).strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("discovered_source_job_ids must be unique")
        return normalized

    @field_validator("discovered_candidate_ids")
    @classmethod
    def validate_unique_candidate_ids(cls, value: list[str]) -> list[str]:
        normalized = [str(item).strip() for item in value if str(item).strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("discovered_candidate_ids must be unique")
        return normalized

    @field_validator("discovered_urls")
    @classmethod
    def validate_unique_urls(cls, value: list[str]) -> list[str]:
        normalized = [_validate_http_url(str(item).strip(), "discovered_urls") for item in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("discovered_urls must be unique")
        return normalized

    @field_validator("started_at")
    @classmethod
    def validate_started_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value, "started_at")

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _validate_aware_datetime(value, "completed_at")

    @model_validator(mode="after")
    def validate_manifest(self) -> "DiscoveryManifest":
        observed_count = max(
            len(self.discovered_candidate_ids),
            len(self.discovered_source_job_ids),
            len(self.discovered_urls),
        )
        if self.discovered_count < observed_count:
            raise ValueError("discovered_count cannot be smaller than the recorded IDs or URLs")
        if self.completeness in {CompletenessState.COMPLETE, CompletenessState.EMPTY_CONFIRMED}:
            if not self.pagination_complete:
                raise ValueError("complete discovery manifests must confirm pagination completion")
            if self.completed_at is None:
                raise ValueError("complete discovery manifests must include completed_at")
        if self.completeness == CompletenessState.COMPLETE and self.discovered_count == 0:
            raise ValueError("zero-job discovery must use EMPTY_CONFIRMED rather than COMPLETE")
        if self.completeness == CompletenessState.EMPTY_CONFIRMED and self.discovered_count != 0:
            raise ValueError("EMPTY_CONFIRMED manifests cannot contain discovered jobs")
        if self.completeness == CompletenessState.EMPTY_CONFIRMED and not self.reasons:
            raise ValueError("EMPTY_CONFIRMED manifests must include independent empty-source evidence")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be earlier than started_at")
        if self.completeness in {CompletenessState.BLOCKED, CompletenessState.FAILED} and not self.reasons:
            raise ValueError("blocked and failed manifests must include at least one reason")
        return self

    @property
    def reconciliation_allowed(self) -> bool:
        return (
            self.completeness in {CompletenessState.COMPLETE, CompletenessState.EMPTY_CONFIRMED}
            and self.pagination_complete
            and self.completed_at is not None
        )


class AcquisitionResult(ContractModel):
    strategy: ScrapeStrategy
    success: bool
    final_url: str | None = None
    payload_ref: str | None = None
    content_type: str | None = None
    content_hash: str | None = None
    template_fingerprint: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    browser_used: bool = False
    elapsed_seconds: float = Field(default=0.0, ge=0.0)
    error_type: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("final_url")
    @classmethod
    def validate_final_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_http_url(value, "final_url")

    @model_validator(mode="after")
    def validate_result(self) -> "AcquisitionResult":
        if self.strategy == ScrapeStrategy.CRAWL4AI and not self.browser_used:
            raise ValueError("Crawl4AI acquisition results must set browser_used=True")
        if not self.success and not (self.error_type or self.error_message):
            raise ValueError("failed acquisition results must include an error")
        return self


class ExtractionResult(ContractModel):
    strategy: ScrapeStrategy
    valid: bool
    job: JobPosting | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    input_content_hash: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    gpu_used: bool = False
    cache_hit: bool = False
    model_name: str | None = None
    prompt_version: str | None = None
    schema_version: str = "1"
    elapsed_seconds: float = Field(default=0.0, ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_result(self) -> "ExtractionResult":
        if self.valid and self.job is None:
            raise ValueError("valid extraction results must contain a job")
        if self.cache_hit and self.gpu_used:
            raise ValueError("a cache hit must not claim GPU usage for the current extraction")
        if self.strategy == ScrapeStrategy.LLM_FALLBACK:
            if not (self.gpu_used or self.cache_hit):
                raise ValueError("LLM fallback must either use the GPU or return a cached result")
            if not self.model_name:
                raise ValueError("LLM fallback results must record model_name")
        elif self.gpu_used or self.cache_hit or self.model_name:
            raise ValueError("non-LLM extraction results cannot claim GPU/cache/model usage")
        return self


class GpuExtractionRequest(ContractModel):
    request_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=2, max_length=128)
    source_job_id: str = Field(min_length=1, max_length=500)
    content_ref: str = Field(min_length=1, max_length=2000)
    content_hash: str = Field(min_length=16, max_length=128)
    model_name: str = Field(default="qwen2.5:3b", min_length=1, max_length=200)
    prompt_version: str = Field(default="1", min_length=1, max_length=100)
    schema_version: str = Field(default="1", min_length=1, max_length=100)
    source_contract_version: int = Field(default=1, ge=1)
    known_fields: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=5, ge=0, le=9)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value, "created_at")

    def cache_key(self) -> str:
        payload = {
            "content_hash": self.content_hash,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "source_contract_version": self.source_contract_version,
            "known_fields": self.known_fields,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class CertificationSideEffects(ContractModel):
    write_catalog: bool = False
    reconcile_lifecycle: bool = False
    index_vectors: bool = False
    refresh_recommendations: bool = False

    @model_validator(mode="after")
    def enforce_isolation(self) -> "CertificationSideEffects":
        if any((self.write_catalog, self.reconcile_lifecycle, self.index_vectors, self.refresh_recommendations)):
            raise ValueError("certification mode cannot enable production side effects")
        return self


class CertificationResult(ContractModel):
    source_id: str = Field(min_length=2, max_length=128)
    status: CertificationStatus
    requested_limit: int = Field(default=10, ge=1, le=10)
    discovered_count: int = Field(default=0, ge=0)
    attempted_count: int = Field(default=0, ge=0)
    jobs: list[JobPosting] = Field(default_factory=list)
    side_effects: CertificationSideEffects = Field(default_factory=CertificationSideEffects)
    reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_certification(self) -> "CertificationResult":
        if self.attempted_count > self.requested_limit:
            raise ValueError("certification attempted_count cannot exceed requested_limit")
        if len(self.jobs) > self.requested_limit:
            raise ValueError("certification cannot return more jobs than requested_limit")
        if len(self.jobs) > self.attempted_count:
            raise ValueError("certification cannot return more jobs than were attempted")
        if self.status == CertificationStatus.PASSED:
            if self.discovered_count < self.requested_limit or len(self.jobs) != self.requested_limit:
                raise ValueError("PASSED certification requires exactly requested_limit valid jobs")
        if self.status == CertificationStatus.SOURCE_EXHAUSTED:
            if self.discovered_count >= self.requested_limit:
                raise ValueError("SOURCE_EXHAUSTED requires fewer discovered jobs than requested_limit")
            if len(self.jobs) != self.discovered_count:
                raise ValueError("SOURCE_EXHAUSTED must return every discovered job as valid")
        if self.status in {CertificationStatus.ACCESS_BLOCKED, CertificationStatus.LICENSE_REQUIRED} and not self.reasons:
            raise ValueError("blocked and licensed certification results must include a reason")
        return self

    @property
    def valid_job_count(self) -> int:
        return len(self.jobs)


class SourceRunResult(ContractModel):
    run_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=2, max_length=128)
    contract_version: int = Field(ge=1)
    mode: RunMode
    status: RunStatus
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    manifest: DiscoveryManifest | None = None
    acquisitions: list[AcquisitionResult] = Field(default_factory=list)
    extractions: list[ExtractionResult] = Field(default_factory=list)
    certification: CertificationResult | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

    @field_validator("source_id")
    @classmethod
    def normalize_source_id(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("started_at")
    @classmethod
    def validate_started_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value, "started_at")

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _validate_aware_datetime(value, "completed_at")

    @model_validator(mode="after")
    def validate_run(self) -> "SourceRunResult":
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be earlier than started_at")
        if self.manifest is not None:
            if self.manifest.run_id != self.run_id or self.manifest.source_id != self.source_id:
                raise ValueError("manifest identity must match the source run")
            if self.manifest.contract_version != self.contract_version:
                raise ValueError("manifest contract_version must match the source run")
        if self.mode == RunMode.CERTIFICATION:
            if self.certification is None:
                raise ValueError("certification runs must include a CertificationResult")
            if self.certification.source_id != self.source_id:
                raise ValueError("certification source_id must match the source run")
        elif self.certification is not None:
            raise ValueError("only certification runs may include a CertificationResult")
        if self.status in {RunStatus.SUCCEEDED, RunStatus.PARTIAL, RunStatus.BLOCKED, RunStatus.FAILED}:
            if self.completed_at is None:
                raise ValueError("terminal source runs must include completed_at")
        return self

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pymongo.database import Database

from .certification import (
    CertificationOptions,
    PortalCertificationRecord,
    PortalFleetCertifier,
    PortalInventoryEntry,
)
from .contracts import ContractModel
from .production_ingestion import Phase6AIngestionPlan, Phase6ASource
from .production_jobs import build_phase6c_content_hash, build_phase6c_identity
from .production_persistence import (
    Phase6BRawEvidenceInput,
    Phase6BSourceCounters,
    ProductionIngestionPersistenceRepository,
    ProductionPersistenceError,
)
from .production_quality import (
    ProductionJobQualityRepository,
    validate_phase6d_candidate,
)
from ..warehouse.serializers import to_plain_data


PHASE_6E_CONTRACT_VERSION = "1.0"
PHASE_6E = "6E"
PRODUCTION_WRITE_CONFIRMATION = "ENABLE_PHASE_6E_WRITES"

TerminalSourceStatus = Literal["success", "failed", "blocked", "cancelled"]
ExecutionMode = Literal["dry_run", "write"]

_RETRYABLE_ERROR_TYPES = {
    "source_timeout",
    "network_error",
    "TimeoutError",
    "ConnectionError",
    "OSError",
}


class ProductionRunnerError(RuntimeError):
    """Raised when Phase 6E execution violates cohort or safety controls."""


class Phase6ERunnerConfig(ContractModel):
    contract_version: Literal["1.0"] = PHASE_6E_CONTRACT_VERSION
    phase: Literal["6E"] = PHASE_6E
    execution_mode: ExecutionMode = "dry_run"
    max_source_concurrency: int = Field(default=2, ge=1, le=4)
    max_attempts: int = Field(default=2, ge=1, le=3)
    retry_backoff_seconds: float = Field(default=1.0, ge=0, le=300)
    catalog_mode: Literal["bounded_certification", "complete_catalog"] = (
        "bounded_certification"
    )
    max_jobs: int | None = Field(default=10, ge=1, le=10)
    max_pages: int = Field(default=3, ge=1, le=500)
    detail_concurrency: int = Field(default=1, ge=1, le=2)
    detail_retry_attempts: int = Field(default=1, ge=0, le=2)
    requests_per_minute: int = Field(default=30, ge=1, le=600)
    source_timeout_seconds: int = Field(default=600, ge=30, le=86400)
    acquisition_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    allow_llm_fallback: bool = False
    normalized_job_writes_enabled: bool = False
    lifecycle_reconciliation_enabled: bool = False
    deactivation_enabled: bool = False

    @model_validator(mode="after")
    def validate_controls(self) -> "Phase6ERunnerConfig":
        if self.execution_mode == "write" and not self.normalized_job_writes_enabled:
            raise ValueError("Phase 6E write mode requires normalized_job_writes_enabled=true")
        if self.execution_mode == "dry_run" and self.normalized_job_writes_enabled:
            raise ValueError("Phase 6E dry-run cannot enable normalized job writes")
        if self.lifecycle_reconciliation_enabled:
            raise ValueError("Phase 6E cannot enable lifecycle reconciliation")
        if self.deactivation_enabled:
            raise ValueError("Phase 6E cannot enable job deactivation")
        if self.catalog_mode == "bounded_certification":
            if self.max_jobs is None:
                raise ValueError("Bounded certification requires max_jobs")
        elif self.max_jobs is not None:
            raise ValueError("Complete-catalog execution cannot set max_jobs")
        return self

    def certification_options(self) -> CertificationOptions:
        return CertificationOptions(
            catalog_mode=self.catalog_mode,
            max_jobs=self.max_jobs,
            max_pages=self.max_pages,
            detail_concurrency=self.detail_concurrency,
            detail_retry_attempts=self.detail_retry_attempts,
            requests_per_minute=self.requests_per_minute,
            source_timeout_seconds=self.source_timeout_seconds,
            acquisition_timeout_seconds=self.acquisition_timeout_seconds,
            allow_unknown_cross_domain_redirects=False,
            allow_llm_fallback=self.allow_llm_fallback,
        )


class Phase6EAttemptResult(ContractModel):
    attempt_number: int = Field(ge=1)
    status: str
    error_type: str | None = None
    error_message: str | None = None
    elapsed_seconds: float = Field(default=0, ge=0)
    retryable: bool = False


class Phase6ESourceResult(ContractModel):
    source_id: str
    display_name: str
    status: TerminalSourceStatus
    certification_status: str | None = None
    attempts: list[Phase6EAttemptResult] = Field(default_factory=list)
    discovered_count: int = Field(default=0, ge=0)
    attempted_count: int = Field(default=0, ge=0)
    extracted_count: int = Field(default=0, ge=0)
    accepted_count: int = Field(default=0, ge=0)
    quarantined_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)
    inserted_count: int = Field(default=0, ge=0)
    updated_count: int = Field(default=0, ge=0)
    unchanged_count: int = Field(default=0, ge=0)
    reactivated_count: int = Field(default=0, ge=0)
    source_run_id: str | None = None
    catalog_mode: str = "bounded_certification"
    discovery_complete: bool = False
    catalog_complete: bool = False
    error_type: str | None = None
    error_message: str | None = None
    elapsed_seconds: float = Field(default=0, ge=0)


class Phase6ERunManifest(ContractModel):
    contract_version: Literal["1.0"] = PHASE_6E_CONTRACT_VERSION
    phase: Literal["6E"] = PHASE_6E
    run_id: str
    generated_at: datetime
    plan_id: str
    cohort_sha256: str
    execution_mode: ExecutionMode
    selected_source_ids: list[str]
    requested_source_count: int = Field(ge=1)
    completed_source_count: int = Field(ge=0)
    successful_source_count: int = Field(ge=0)
    failed_source_count: int = Field(ge=0)
    blocked_source_count: int = Field(ge=0)
    cancelled_source_count: int = Field(ge=0)
    extracted_job_count: int = Field(ge=0)
    accepted_job_count: int = Field(ge=0)
    quarantined_job_count: int = Field(ge=0)
    inserted_job_count: int = Field(ge=0)
    updated_job_count: int = Field(ge=0)
    unchanged_job_count: int = Field(ge=0)
    reactivated_job_count: int = Field(ge=0)
    source_results: list[Phase6ESourceResult]
    controls: dict[str, Any]

    @field_validator("generated_at")
    @classmethod
    def require_aware_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_manifest(self) -> "Phase6ERunManifest":
        if self.requested_source_count != len(self.selected_source_ids):
            raise ValueError("requested_source_count does not match selected_source_ids")
        if self.completed_source_count != len(self.source_results):
            raise ValueError("completed_source_count does not match source_results")
        if self.controls.get("lifecycle_reconciliation_enabled") is not False:
            raise ValueError("Phase 6E manifest must keep reconciliation disabled")
        if self.controls.get("deactivation_enabled") is not False:
            raise ValueError("Phase 6E manifest must keep deactivation disabled")
        return self


class Phase6ESourceExecutor(Protocol):
    async def execute(
        self,
        source: Phase6ASource,
        *,
        attempt_number: int,
        run_id: str,
    ) -> PortalCertificationRecord: ...


class CertificationPhase6ESourceExecutor:
    """Run one frozen source through the existing evidence-bound scraper."""

    def __init__(
        self,
        *,
        root: Path,
        output_dir: Path,
        options: CertificationOptions,
    ) -> None:
        self.root = Path(root).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.options = options

    async def execute(
        self,
        source: Phase6ASource,
        *,
        attempt_number: int,
        run_id: str,
    ) -> PortalCertificationRecord:
        certifier = PortalFleetCertifier(
            root=self.root,
            output_dir=self.output_dir,
            options=self.options,
        )
        return await certifier.certify(
            PortalInventoryEntry(
                source_id=source.source_id,
                display_name=source.display_name,
                listing_url=source.listing_url,
                source_row=source.source_row,
            ),
            run_id=run_id,
            attempt_number=attempt_number,
        )


def select_phase6e_sources(
    plan: Phase6AIngestionPlan,
    requested_source_ids: Sequence[str] | None = None,
    *,
    limit: int | None = None,
) -> list[Phase6ASource]:
    requested = [str(value or "").strip() for value in requested_source_ids or []]
    if any(not value for value in requested):
        raise ProductionRunnerError("requested_source_ids cannot contain empty values")
    if len(requested) != len(set(requested)):
        raise ProductionRunnerError("requested_source_ids cannot contain duplicates")
    allowed = set(plan.selected_source_ids)
    rejected = [value for value in requested if value not in allowed]
    if rejected:
        raise ProductionRunnerError(
            "SOURCE_NOT_IN_PRODUCTION_COHORT: " + ", ".join(rejected)
        )
    selected_ids = requested or list(plan.selected_source_ids)
    by_id = {source.source_id: source for source in plan.sources}
    selected = [by_id[source_id] for source_id in selected_ids]
    if limit is not None:
        if limit < 1:
            raise ProductionRunnerError("limit must be at least 1")
        selected = selected[:limit]
    if not selected:
        raise ProductionRunnerError("Phase 6E requires at least one selected source")
    return selected


def _host(value: str | None) -> str | None:
    if not value:
        return None
    return (urlsplit(value).hostname or "").casefold() or None


def _trusted_hosts(source: Phase6ASource, record: PortalCertificationRecord) -> list[str]:
    hosts: list[str] = []
    for value in (
        source.listing_url,
        source.resolved_route_url,
        record.effective_listing_url,
        record.resolved_route_url,
    ):
        host = _host(value)
        if host and host not in hosts:
            hosts.append(host)
    acquisition_hosts = record.acquisition.get("trusted_hosts") if isinstance(record.acquisition, dict) else None
    if isinstance(acquisition_hosts, list):
        for value in acquisition_hosts:
            host = _host(str(value)) or str(value or "").casefold().strip(".")
            if host and host not in hosts:
                hosts.append(host)
    route_hosts = record.route_resolution.get("trusted_hosts") if isinstance(record.route_resolution, dict) else None
    if isinstance(route_hosts, list):
        for value in route_hosts:
            host = _host(str(value)) or str(value or "").casefold().strip(".")
            if host and host not in hosts:
                hosts.append(host)
    return hosts


def _external_job_id(payload: Mapping[str, Any]) -> str | None:
    for key in (
        "external_job_id",
        "externalJobId",
        "job_id",
        "jobId",
        "job_reference",
        "jobReference",
        "requisition_id",
        "requisitionId",
        "id",
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return None


def _job_url(payload: Mapping[str, Any]) -> str | None:
    for key in ("canonical_url", "canonicalUrl", "job_url", "jobUrl", "url", "apply_url", "applyUrl"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return None


def _retryable(*, error_type: str | None, error_message: str | None = None) -> bool:
    if str(error_type or "") in _RETRYABLE_ERROR_TYPES:
        return True
    message = str(error_message or "").casefold()
    return any(marker in message for marker in ("timeout", "timed out", "connection reset", "temporary failure", "dns"))


def _record_terminal_status(record: PortalCertificationRecord, *, accepted: int) -> TerminalSourceStatus:
    if record.status == "blocked":
        return "blocked"
    if record.status == "success" and accepted > 0:
        return "success"
    return "failed"


def _preview_outcome(db: Database, *, source_id: str, normalized_job: Any) -> str:
    identity = build_phase6c_identity(source_id=source_id, job=normalized_job)
    content_hash = build_phase6c_content_hash(normalized_job)
    collection = db["jobs_current"]
    existing = collection.find_one({"source_id": source_id, "identity_hash": identity.identity_hash})
    if existing is None:
        existing = collection.find_one({"job_id": identity.job_id})
    if existing is None:
        return "inserted"
    if existing.get("is_active") is False:
        return "reactivated"
    if str(existing.get("content_hash") or "") != content_hash:
        return "updated"
    return "unchanged"


def write_phase6e_manifest(path: Path, manifest: Phase6ERunManifest) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(target)
    return target


ProgressCallback = Callable[[Phase6ESourceResult, int, int], None]


class Phase6EProductionRunner:
    """Execute a frozen cohort with retries, source isolation, and safe persistence."""

    def __init__(
        self,
        *,
        plan: Phase6AIngestionPlan,
        db: Database,
        executor: Phase6ESourceExecutor,
        config: Phase6ERunnerConfig,
    ) -> None:
        self.plan = plan
        self.db = db
        self.executor = executor
        self.config = config
        self.audit = ProductionIngestionPersistenceRepository(db)
        self.quality = ProductionJobQualityRepository(db)

    async def run(
        self,
        *,
        requested_source_ids: Sequence[str] | None = None,
        limit: int | None = None,
        run_id: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> Phase6ERunManifest:
        selected = select_phase6e_sources(
            self.plan,
            requested_source_ids,
            limit=limit,
        )
        current_run_id = str(run_id or f"phase6e_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:10]}")
        write_mode = self.config.execution_mode == "write"
        fleet = None
        if write_mode:
            fleet = self.audit.create_fleet_run(
                plan=self.plan,
                selected_source_ids=[source.source_id for source in selected],
                fleet_run_id=current_run_id,
                normalized_job_writes_enabled=True,
            )
        semaphore = asyncio.Semaphore(self.config.max_source_concurrency)
        results: list[Phase6ESourceResult | None] = [None] * len(selected)

        async def run_one(index: int, source: Phase6ASource) -> None:
            async with semaphore:
                result = await self._run_source(
                    source=source,
                    run_id=current_run_id,
                    fleet_run_id=fleet.fleet_run_id if fleet else None,
                )
                results[index] = result
                if on_progress is not None:
                    on_progress(result, sum(item is not None for item in results), len(selected))

        await asyncio.gather(*(run_one(index, source) for index, source in enumerate(selected)))
        completed = [result for result in results if result is not None]
        if len(completed) != len(selected):
            raise ProductionRunnerError("Phase 6E did not produce a result for every selected source")

        if fleet is not None:
            finalized_fleet = self.audit.finalize_fleet_run(fleet_run_id=fleet.fleet_run_id)
            if finalized_fleet.controls.get("lifecycle_reconciliation_enabled") is not False:
                raise ProductionRunnerError("Unsafe fleet reconciliation control detected")
            if finalized_fleet.controls.get("deactivation_enabled") is not False:
                raise ProductionRunnerError("Unsafe fleet deactivation control detected")

        status_counts = Counter(result.status for result in completed)
        manifest = Phase6ERunManifest(
            run_id=current_run_id,
            generated_at=datetime.now(timezone.utc),
            plan_id=self.plan.plan_id,
            cohort_sha256=self.plan.cohort_sha256,
            execution_mode=self.config.execution_mode,
            selected_source_ids=[source.source_id for source in selected],
            requested_source_count=len(selected),
            completed_source_count=len(completed),
            successful_source_count=status_counts.get("success", 0),
            failed_source_count=status_counts.get("failed", 0),
            blocked_source_count=status_counts.get("blocked", 0),
            cancelled_source_count=status_counts.get("cancelled", 0),
            extracted_job_count=sum(result.extracted_count for result in completed),
            accepted_job_count=sum(result.accepted_count for result in completed),
            quarantined_job_count=sum(result.quarantined_count for result in completed),
            inserted_job_count=sum(result.inserted_count for result in completed),
            updated_job_count=sum(result.updated_count for result in completed),
            unchanged_job_count=sum(result.unchanged_count for result in completed),
            reactivated_job_count=sum(result.reactivated_count for result in completed),
            source_results=completed,
            controls={
                "normalized_job_writes_enabled": write_mode,
                "lifecycle_reconciliation_enabled": False,
                "deactivation_enabled": False,
                "max_source_concurrency": self.config.max_source_concurrency,
                "max_attempts": self.config.max_attempts,
                "max_jobs_per_source": self.config.max_jobs,
                "max_pages_per_source": self.config.max_pages,
                "catalog_mode": self.config.catalog_mode,
                "catalog_completion_required": (
                    self.config.catalog_mode == "complete_catalog"
                ),
                "bounded_pilot_execution": (
                    self.config.catalog_mode == "bounded_certification"
                ),
            },
        )
        return manifest

    async def _run_source(
        self,
        *,
        source: Phase6ASource,
        run_id: str,
        fleet_run_id: str | None,
    ) -> Phase6ESourceResult:
        started = time.perf_counter()
        attempts: list[Phase6EAttemptResult] = []
        source_run = None
        if fleet_run_id is not None:
            source_run = self.audit.start_source_run(
                fleet_run_id=fleet_run_id,
                source=source,
                extractor_version="phase6e-production-runner-1.1",
                metadata={
                    "execution_mode": self.config.execution_mode,
                    "catalog_mode": self.config.catalog_mode,
                },
            )

        record: PortalCertificationRecord | None = None
        terminal_exception: BaseException | None = None
        for attempt_number in range(1, self.config.max_attempts + 1):
            attempt_started = time.perf_counter()
            try:
                candidate = await self.executor.execute(
                    source,
                    attempt_number=attempt_number,
                    run_id=run_id,
                )
                retryable = (
                    candidate.status == "failed"
                    and _retryable(error_type=candidate.error_type, error_message=candidate.error_message)
                    and attempt_number < self.config.max_attempts
                )
                attempts.append(
                    Phase6EAttemptResult(
                        attempt_number=attempt_number,
                        status=candidate.status,
                        error_type=candidate.error_type,
                        error_message=candidate.error_message,
                        elapsed_seconds=round(time.perf_counter() - attempt_started, 3),
                        retryable=retryable,
                    )
                )
                record = candidate
                if not retryable:
                    break
            except asyncio.CancelledError:
                terminal_exception = asyncio.CancelledError()
                attempts.append(
                    Phase6EAttemptResult(
                        attempt_number=attempt_number,
                        status="cancelled",
                        error_type="CancelledError",
                        error_message="Source execution was cancelled",
                        elapsed_seconds=round(time.perf_counter() - attempt_started, 3),
                        retryable=False,
                    )
                )
                break
            except Exception as exc:
                terminal_exception = exc
                retryable = _retryable(error_type=type(exc).__name__, error_message=str(exc)) and attempt_number < self.config.max_attempts
                attempts.append(
                    Phase6EAttemptResult(
                        attempt_number=attempt_number,
                        status="failed",
                        error_type=type(exc).__name__,
                        error_message=str(exc)[:2000],
                        elapsed_seconds=round(time.perf_counter() - attempt_started, 3),
                        retryable=retryable,
                    )
                )
                if not retryable:
                    break
            if self.config.retry_backoff_seconds:
                await asyncio.sleep(self.config.retry_backoff_seconds * attempt_number)

        if record is None:
            terminal_status: TerminalSourceStatus = "cancelled" if isinstance(terminal_exception, asyncio.CancelledError) else "failed"
            error_type = type(terminal_exception).__name__ if terminal_exception else "source_execution_failed"
            error_message = str(terminal_exception or "Source execution produced no record")[:2000]
            if source_run is not None:
                self.audit.finalize_source_run(
                    source_run_id=source_run.source_run_id,
                    status=terminal_status,
                    counters=Phase6BSourceCounters(),
                    error_type=error_type,
                    error_message=error_message,
                    metadata={"attempts": [item.model_dump(mode="json") for item in attempts]},
                )
            return Phase6ESourceResult(
                source_id=source.source_id,
                display_name=source.display_name,
                status=terminal_status,
                attempts=attempts,
                source_run_id=source_run.source_run_id if source_run else None,
                catalog_mode=self.config.catalog_mode,
                error_type=error_type,
                error_message=error_message,
                elapsed_seconds=round(time.perf_counter() - started, 3),
            )

        accepted = quarantined = rejected = 0
        outcome_counts: Counter[str] = Counter()
        trusted_hosts = _trusted_hosts(source, record)
        for payload_value in record.sample_jobs:
            if not isinstance(payload_value, Mapping):
                rejected += 1
                continue
            payload = to_plain_data(dict(payload_value))
            job_url = _job_url(payload)
            external_id = _external_job_id(payload)
            if source_run is not None and fleet_run_id is not None:
                try:
                    evidence, _ = self.audit.store_raw_evidence(
                        fleet_run_id=fleet_run_id,
                        source_run_id=source_run.source_run_id,
                        source_id=source.source_id,
                        evidence=Phase6BRawEvidenceInput(
                            source_url=job_url or record.effective_listing_url,
                            canonical_url=job_url,
                            external_job_id=external_id,
                            payload=payload,
                            extractor_name="phase6e_certified_scraper",
                            extractor_version="1.0",
                            metadata={
                                "certification_status": record.certification_status,
                                "certification_attempt": record.attempt_number,
                                "detected_platform": record.detected_platform,
                            },
                        ),
                    )
                    processed = self.quality.process_raw_evidence(
                        fleet_run_id=fleet_run_id,
                        source_run_id=source_run.source_run_id,
                        source_id=source.source_id,
                        raw_evidence_id=evidence.evidence_id,
                        candidate=payload,
                        trusted_hosts=trusted_hosts,
                    )
                    if processed.status == "accepted" and processed.upsert is not None:
                        accepted += 1
                        outcome_counts[processed.upsert.outcome] += 1
                    else:
                        quarantined += 1
                except Exception:
                    rejected += 1
            else:
                quality = validate_phase6d_candidate(
                    source_id=source.source_id,
                    payload=payload,
                    source_listing_url=source.listing_url,
                    trusted_hosts=trusted_hosts,
                )
                if quality.status == "accepted" and quality.normalized_job is not None:
                    accepted += 1
                    outcome_counts[_preview_outcome(self.db, source_id=source.source_id, normalized_job=quality.normalized_job)] += 1
                else:
                    quarantined += 1

        terminal_status = _record_terminal_status(record, accepted=accepted)
        error_type = record.error_type
        error_message = record.error_message
        if self.config.catalog_mode == "complete_catalog" and (
            record.catalog_mode != "complete_catalog" or not record.catalog_complete
        ):
            terminal_status = "failed"
            error_type = error_type or "catalog_incomplete"
            error_message = error_message or (
                "Complete-catalog execution did not produce complete discovery and detail evidence"
            )
        if terminal_status == "failed" and record.status == "success" and accepted == 0:
            error_type = "quality_gate_rejected_all"
            error_message = "Scraping succeeded but no extracted jobs passed the Phase 6D quality gate"
        if source_run is not None:
            self.audit.finalize_source_run(
                source_run_id=source_run.source_run_id,
                status=terminal_status,
                counters=Phase6BSourceCounters(
                    discovered_count=record.discovered_urls,
                    attempted_count=record.attempted_urls,
                    extracted_count=record.extracted_jobs,
                    valid_count=accepted,
                    quarantined_count=quarantined,
                    rejected_count=rejected,
                ),
                acquisition_strategy=str(record.acquisition.get("strategy") or record.detected_platform or "unknown"),
                error_type=error_type if terminal_status != "success" else None,
                error_message=error_message if terminal_status != "success" else None,
                metadata={
                    "attempts": [item.model_dump(mode="json") for item in attempts],
                    "certification_status": record.certification_status,
                    "catalog_mode": record.catalog_mode,
                    "discovery_complete": record.discovery_complete,
                    "catalog_complete": record.catalog_complete,
                    "bounded_pilot_execution": (
                        self.config.catalog_mode == "bounded_certification"
                    ),
                },
            )

        return Phase6ESourceResult(
            source_id=source.source_id,
            display_name=source.display_name,
            status=terminal_status,
            certification_status=record.certification_status,
            attempts=attempts,
            discovered_count=record.discovered_urls,
            attempted_count=record.attempted_urls,
            extracted_count=record.extracted_jobs,
            accepted_count=accepted,
            quarantined_count=quarantined,
            rejected_count=rejected,
            inserted_count=outcome_counts.get("inserted", 0),
            updated_count=outcome_counts.get("updated", 0),
            unchanged_count=outcome_counts.get("unchanged", 0),
            reactivated_count=outcome_counts.get("reactivated", 0),
            source_run_id=source_run.source_run_id if source_run else None,
            catalog_mode=record.catalog_mode,
            discovery_complete=record.discovery_complete,
            catalog_complete=record.catalog_complete,
            error_type=error_type if terminal_status != "success" else None,
            error_message=error_message if terminal_status != "success" else None,
            elapsed_seconds=round(time.perf_counter() - started, 3),
        )

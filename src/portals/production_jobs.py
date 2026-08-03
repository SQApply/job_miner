from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from .contracts import ContractModel
from .production_persistence import (
    ProductionIngestionPersistenceRepository,
    ProductionPersistenceError,
)
from ..warehouse.base import utc_now
from ..warehouse.documents import JobCurrentDocument, JobHistoryDocument
from ..warehouse.hashing import compact_text, stable_hash
from ..warehouse.job_dates import parse_job_posted_at
from ..warehouse.serializers import as_str_list, to_plain_data
from ..warehouse.url_utils import canonical_job_url

PHASE_6C_CONTRACT_VERSION = "1.0"
JobIdentityStrategy = Literal["external_job_id", "canonical_url", "fingerprint"]
JobUpsertOutcome = Literal["inserted", "updated", "unchanged", "reactivated"]


class ProductionJobUpsertError(RuntimeError):
    """Raised when a Phase 6C identity or upsert invariant is violated."""


def _text(value: Any) -> str | None:
    normalized = compact_text(None if value is None else str(value))
    return normalized or None


def _normalized_list(value: Any, *, sort_values: bool = False) -> list[str]:
    items = [compact_text(item) for item in as_str_list(value)]
    unique = list(dict.fromkeys(item for item in items if item))
    return sorted(unique, key=str.casefold) if sort_values else unique


def _canonical(value: Any) -> str | None:
    normalized = canonical_job_url(None if value is None else str(value))
    return normalized or None


def _identity_parts(
    *,
    source_id: str,
    external_job_id: str | None,
    canonical_url_value: str | None,
    title: str | None,
    company: str | None,
    location_text: str | None,
    job_reference: str | None,
    identity_hint: str | None,
) -> tuple[JobIdentityStrategy, dict[str, Any]]:
    if external_job_id:
        return "external_job_id", {
            "source_id": source_id,
            "external_job_id": external_job_id.casefold(),
        }
    if canonical_url_value:
        return "canonical_url", {
            "source_id": source_id,
            "canonical_url": canonical_url_value,
        }
    if not title or not any((company, location_text, job_reference, identity_hint)):
        raise ProductionJobUpsertError(
            "Fallback identity requires title plus company, location, job_reference, or identity_hint"
        )
    return "fingerprint", {
        "source_id": source_id,
        "title": title.casefold(),
        "company": (company or "").casefold(),
        "location_text": (location_text or "").casefold(),
        "job_reference": (job_reference or "").casefold(),
        "identity_hint": (identity_hint or "").casefold(),
    }


class Phase6CNormalizedJobInput(ContractModel):
    external_job_id: str | None = Field(default=None, max_length=300)
    canonical_url: str | None = None
    source_url: str | None = None
    job_url: str | None = None
    apply_url: str | None = None
    title: str | None = Field(default=None, max_length=1000)
    company: str | None = Field(default=None, max_length=1000)
    location_text: str | None = Field(default=None, max_length=1000)
    description: str | None = None
    employment_type: str | None = Field(default=None, max_length=300)
    duration: str | None = Field(default=None, max_length=300)
    compensation_text: str | None = Field(default=None, max_length=1000)
    posted_date: str | None = Field(default=None, max_length=300)
    responsibilities: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    job_reference: str | None = Field(default=None, max_length=300)
    identity_hint: str | None = Field(default=None, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "external_job_id",
        "title",
        "company",
        "location_text",
        "description",
        "employment_type",
        "duration",
        "compensation_text",
        "posted_date",
        "job_reference",
        "identity_hint",
        mode="before",
    )
    @classmethod
    def normalize_text_fields(cls, value: Any) -> str | None:
        return _text(value)

    @field_validator("canonical_url", "source_url", "job_url", "apply_url", mode="before")
    @classmethod
    def normalize_url_fields(cls, value: Any) -> str | None:
        return _canonical(value)

    @field_validator("responsibilities", mode="before")
    @classmethod
    def normalize_responsibilities(cls, value: Any) -> list[str]:
        return _normalized_list(value)

    @field_validator("required_skills", "preferred_skills", mode="before")
    @classmethod
    def normalize_skills(cls, value: Any) -> list[str]:
        return _normalized_list(value, sort_values=True)

    @model_validator(mode="after")
    def validate_identity_evidence(self) -> "Phase6CNormalizedJobInput":
        canonical_value = self.best_canonical_url()
        try:
            _identity_parts(
                source_id="validation-source",
                external_job_id=self.external_job_id,
                canonical_url_value=canonical_value,
                title=self.title,
                company=self.company,
                location_text=self.location_text,
                job_reference=self.job_reference,
                identity_hint=self.identity_hint,
            )
        except ProductionJobUpsertError as exc:
            raise ValueError(str(exc)) from exc
        return self

    def best_canonical_url(self) -> str | None:
        return self.canonical_url or self.job_url or self.source_url or self.apply_url


class Phase6CIdentity(ContractModel):
    strategy: JobIdentityStrategy
    identity_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    job_id: str
    canonical_url: str | None = None
    external_job_id: str | None = None


class Phase6CUpsertResult(ContractModel):
    contract_version: Literal["1.0"] = PHASE_6C_CONTRACT_VERSION
    phase: Literal["6C"] = "6C"
    outcome: JobUpsertOutcome
    job_id: str
    source_id: str
    identity_strategy: JobIdentityStrategy
    identity_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    version: int = Field(ge=1)
    raw_evidence_id: str
    normalized_job_writes_enabled: bool = True
    lifecycle_reconciliation_enabled: bool = False
    deactivation_enabled: bool = False


def build_phase6c_identity(*, source_id: str, job: Phase6CNormalizedJobInput) -> Phase6CIdentity:
    source = _text(source_id)
    if not source:
        raise ProductionJobUpsertError("source_id cannot be empty")
    canonical_value = job.best_canonical_url()
    strategy, parts = _identity_parts(
        source_id=source,
        external_job_id=job.external_job_id,
        canonical_url_value=canonical_value,
        title=job.title,
        company=job.company,
        location_text=job.location_text,
        job_reference=job.job_reference,
        identity_hint=job.identity_hint,
    )
    identity_hash = stable_hash({"contract_version": PHASE_6C_CONTRACT_VERSION, **parts})
    return Phase6CIdentity(
        strategy=strategy,
        identity_hash=identity_hash,
        job_id="job_" + identity_hash[:24],
        canonical_url=canonical_value,
        external_job_id=job.external_job_id,
    )


def build_phase6c_content_payload(job: Phase6CNormalizedJobInput) -> dict[str, Any]:
    """Return deterministic mutable job content used for update detection.

    Relative freshness labels such as ``3 days ago`` are intentionally excluded,
    because they change on every crawl even when the job itself is unchanged.
    """

    return {
        "canonical_url": job.best_canonical_url(),
        "apply_url": job.apply_url,
        "title": job.title,
        "company": job.company,
        "location_text": job.location_text,
        "description": job.description,
        "employment_type": job.employment_type,
        "duration": job.duration,
        "compensation_text": job.compensation_text,
        "responsibilities": job.responsibilities,
        "required_skills": job.required_skills,
        "preferred_skills": job.preferred_skills,
        "job_reference": job.job_reference,
    }


def build_phase6c_content_hash(job: Phase6CNormalizedJobInput) -> str:
    return stable_hash(build_phase6c_content_payload(job))


class ProductionJobUpsertRepository:
    """Phase 6C normalized-job persistence with stable identity and no deactivation."""

    def __init__(self, db: Database):
        self.db = db
        self.audit = ProductionIngestionPersistenceRepository(db)

    def _validate_context(
        self,
        *,
        fleet_run_id: str,
        source_run_id: str,
        source_id: str,
        raw_evidence_id: str,
    ) -> None:
        fleet = self.audit.get_fleet_run(fleet_run_id)
        source = self.audit.get_source_run(source_run_id)
        evidence = self.audit.get_raw_evidence(raw_evidence_id)
        if fleet.status != "running":
            raise ProductionJobUpsertError("Normalized jobs require a running fleet run")
        if source.status != "running":
            raise ProductionJobUpsertError("Normalized jobs require a running source run")
        if source.fleet_run_id != fleet_run_id or source.source_id != source_id:
            raise ProductionJobUpsertError("Source run does not match fleet/source context")
        if evidence.fleet_run_id != fleet_run_id:
            raise ProductionJobUpsertError("Raw evidence does not match fleet_run_id")
        if evidence.source_run_id != source_run_id or evidence.source_id != source_id:
            raise ProductionJobUpsertError("Raw evidence does not match source context")
        controls = fleet.controls
        if controls.get("normalized_job_writes_enabled") is not True:
            raise ProductionJobUpsertError("NORMALIZED_JOB_WRITES_DISABLED")
        if controls.get("lifecycle_reconciliation_enabled") is not False:
            raise ProductionJobUpsertError("Lifecycle reconciliation must remain disabled")
        if controls.get("deactivation_enabled") is not False:
            raise ProductionJobUpsertError("Job deactivation must remain disabled")

    def _find_existing(
        self,
        *,
        source_id: str,
        identity: Phase6CIdentity,
    ) -> dict[str, Any] | None:
        collection = self.db[JobCurrentDocument.collection_name]
        queries: list[dict[str, Any]] = [
            {"identity_hash": identity.identity_hash},
            {"job_id": identity.job_id},
        ]
        if identity.external_job_id:
            queries.append(
                {"source_id": source_id, "external_job_id": identity.external_job_id}
            )
        if identity.canonical_url:
            queries.extend(
                [
                    {"target_id": source_id, "canonical_job_url": identity.canonical_url},
                    {"target_id": source_id, "job_url": identity.canonical_url},
                    {"target_id": source_id, "source_url": identity.canonical_url},
                    {"target_id": source_id, "apply_url": identity.canonical_url},
                ]
            )
        for query in queries:
            existing = collection.find_one(query)
            if existing:
                return existing
        return None

    def upsert_job(
        self,
        *,
        fleet_run_id: str,
        source_run_id: str,
        source_id: str,
        raw_evidence_id: str,
        job: Phase6CNormalizedJobInput,
        observed_at: datetime | None = None,
    ) -> Phase6CUpsertResult:
        self._validate_context(
            fleet_run_id=fleet_run_id,
            source_run_id=source_run_id,
            source_id=source_id,
            raw_evidence_id=raw_evidence_id,
        )
        observed = observed_at or utc_now()
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ProductionJobUpsertError("observed_at must be timezone-aware")

        identity = build_phase6c_identity(source_id=source_id, job=job)
        content_payload = build_phase6c_content_payload(job)
        content_hash = stable_hash(content_payload)
        existing = self._find_existing(source_id=source_id, identity=identity)

        if existing is not None:
            existing_source = str(existing.get("source_id") or existing.get("target_id") or "")
            if existing_source and existing_source != source_id:
                raise ProductionJobUpsertError("Identity collision across production sources")

        inserted = existing is None
        was_inactive = bool(existing is not None and existing.get("is_active") is False)
        content_changed = inserted or str((existing or {}).get("content_hash") or "") != content_hash
        if inserted:
            outcome: JobUpsertOutcome = "inserted"
        elif was_inactive:
            outcome = "reactivated"
        elif content_changed:
            outcome = "updated"
        else:
            outcome = "unchanged"

        job_id = str((existing or {}).get("job_id") or identity.job_id)
        version = int((existing or {}).get("version") or 0)
        if content_changed:
            version += 1
        if version < 1:
            version = 1

        posted_at = parse_job_posted_at(job.posted_date, reference_time=observed)
        effective_posted_date = job.posted_date or (existing or {}).get("posted_date")
        effective_posted_at = posted_at or (existing or {}).get("posted_at")
        first_seen_at = (existing or {}).get("first_seen_at") or observed
        canonical_value = identity.canonical_url
        source_url = job.source_url or job.job_url or canonical_value

        document = JobCurrentDocument(
            _id=job_id,
            job_id=job_id,
            target_id=source_id,
            source_id=source_id,
            external_job_id=identity.external_job_id,
            identity_hash=identity.identity_hash,
            identity_strategy=identity.strategy,
            source_url=source_url,
            job_url=job.job_url or canonical_value,
            canonical_job_url=canonical_value,
            apply_url=job.apply_url,
            title=job.title,
            company=job.company,
            location_text=job.location_text,
            employment_type=job.employment_type,
            duration=job.duration,
            compensation_text=job.compensation_text,
            posted_date=effective_posted_date,
            posted_at=effective_posted_at,
            summary=job.description,
            responsibilities=job.responsibilities,
            required_skills=job.required_skills,
            preferred_skills=job.preferred_skills,
            job_reference=job.job_reference or identity.external_job_id,
            content_hash=content_hash,
            first_seen_at=first_seen_at,
            last_seen_at=observed,
            last_deep_scraped_at=observed,
            last_run_session_id=source_run_id,
            last_fleet_run_id=fleet_run_id,
            last_source_run_id=source_run_id,
            raw_evidence_id=raw_evidence_id,
            missing_count=0,
            missing_complete_run_count=0,
            missing_since=None,
            freshness_status="active",
            inactive_reason=None,
            deactivated_at=None,
            deactivation_run_id=None,
            reactivated_at=(
                observed
                if was_inactive
                else (existing or {}).get("reactivated_at")
            ),
            is_active=True,
            version=version,
            raw_payload={
                "normalized": job.model_dump(mode="python"),
                "content_payload": content_payload,
                "metadata": to_plain_data(job.metadata),
            },
        )
        data = document.to_mongo()
        document_id = data.pop("_id")
        created_at = data.pop("created_at")
        try:
            self.db[JobCurrentDocument.collection_name].update_one(
                {"job_id": job_id},
                {
                    "$set": data,
                    "$setOnInsert": {"_id": document_id, "created_at": created_at},
                },
                upsert=True,
            )
        except DuplicateKeyError:
            retry_existing = self._find_existing(source_id=source_id, identity=identity)
            if not retry_existing:
                raise
            return self.upsert_job(
                fleet_run_id=fleet_run_id,
                source_run_id=source_run_id,
                source_id=source_id,
                raw_evidence_id=raw_evidence_id,
                job=job,
                observed_at=observed,
            )

        if content_changed:
            history_id = f"jobhist_{job_id}_{content_hash[:16]}"
            history = JobHistoryDocument(
                _id=history_id,
                history_id=history_id,
                job_id=job_id,
                target_id=source_id,
                source_url=source_url,
                content_hash=content_hash,
                version=version,
                payload={
                    "normalized": job.model_dump(mode="python"),
                    "identity": identity.model_dump(mode="python"),
                    "raw_evidence_id": raw_evidence_id,
                    "fleet_run_id": fleet_run_id,
                    "source_run_id": source_run_id,
                },
                run_session_id=source_run_id,
            )
            self.db[JobHistoryDocument.collection_name].update_one(
                {"history_id": history_id},
                {"$setOnInsert": history.to_mongo()},
                upsert=True,
            )

        counter_field = {
            "inserted": "inserted_job_count",
            "updated": "updated_job_count",
            "unchanged": "unchanged_job_count",
            "reactivated": "reactivated_job_count",
        }[outcome]
        self.db["production_ingestion_source_runs"].update_one(
            {"source_run_id": source_run_id, "status": "running"},
            {"$inc": {counter_field: 1}, "$set": {"updated_at": utc_now()}},
            upsert=False,
        )

        return Phase6CUpsertResult(
            outcome=outcome,
            job_id=job_id,
            source_id=source_id,
            identity_strategy=identity.strategy,
            identity_hash=identity.identity_hash,
            content_hash=content_hash,
            version=version,
            raw_evidence_id=raw_evidence_id,
        )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.db[JobCurrentDocument.collection_name].find_one({"job_id": job_id})

    def delete_validation_job(self, job_id: str) -> dict[str, int]:
        current = self.db[JobCurrentDocument.collection_name].delete_many({"job_id": job_id})
        history = self.db[JobHistoryDocument.collection_name].delete_many({"job_id": job_id})
        return {
            "jobs_current": int(getattr(current, "deleted_count", 0)),
            "jobs_history": int(getattr(history, "deleted_count", 0)),
        }


def init_phase6c_indexes(db: Database) -> dict[str, int]:
    from ..warehouse.indexes import INDEXES

    created: dict[str, int] = {}
    for collection_name in (JobCurrentDocument.collection_name, JobHistoryDocument.collection_name):
        indexes = INDEXES[collection_name]
        db[collection_name].create_indexes(indexes)
        created[collection_name] = len(indexes)
    return created

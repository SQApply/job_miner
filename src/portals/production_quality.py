from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from pydantic import Field
from pymongo.database import Database

from .contracts import ContractModel
from .production_jobs import (
    Phase6CNormalizedJobInput,
    Phase6CUpsertResult,
    ProductionJobUpsertRepository,
)
from .production_persistence import ProductionIngestionPersistenceRepository
from ..warehouse.base import utc_now
from ..warehouse.documents import ProductionJobQuarantineDocument
from ..warehouse.hashing import compact_text, stable_hash
from ..warehouse.serializers import as_str_list, to_plain_data
from ..warehouse.url_utils import canonical_job_url

PHASE_6D_CONTRACT_VERSION = "1.0"
PHASE_6D = "6D"

QuarantineReasonCode = Literal[
    "missing_title",
    "missing_identity",
    "empty_description",
    "description_too_short",
    "invalid_canonical_url",
    "untrusted_host",
    "listing_page_selected",
    "access_denied_page",
    "non_job_content",
    "normalization_failure",
]

ProcessingStatus = Literal["accepted", "quarantined"]

_TITLE_ALIASES = ("title", "job_title", "jobTitle", "position_title", "positionTitle", "name")
_COMPANY_ALIASES = ("company", "company_name", "companyName", "employer", "organization")
_LOCATION_ALIASES = ("location_text", "location", "job_location", "jobLocation", "city")
_DESCRIPTION_ALIASES = (
    "description",
    "job_description",
    "jobDescription",
    "summary",
    "content",
    "body",
)
_EXTERNAL_ID_ALIASES = (
    "external_job_id",
    "externalJobId",
    "requisition_id",
    "requisitionId",
    "job_id",
    "jobId",
    "id",
)
_CANONICAL_URL_ALIASES = ("canonical_url", "canonicalUrl", "job_url", "jobUrl", "url", "source_url")
_APPLY_URL_ALIASES = ("apply_url", "applyUrl", "application_url", "applicationUrl")
_EMPLOYMENT_ALIASES = ("employment_type", "employmentType", "job_type", "jobType", "type")
_DURATION_ALIASES = ("duration", "contract_duration", "contractDuration")
_COMPENSATION_ALIASES = ("compensation_text", "compensation", "salary", "salary_text", "salaryText")
_POSTED_DATE_ALIASES = ("posted_date", "postedDate", "date_posted", "datePosted", "published_at", "publishedAt")
_JOB_REFERENCE_ALIASES = ("job_reference", "jobReference", "reference", "requisition_number", "requisitionNumber")
_RESPONSIBILITIES_ALIASES = ("responsibilities", "duties")
_REQUIRED_SKILLS_ALIASES = ("required_skills", "requiredSkills", "skills", "qualifications")
_PREFERRED_SKILLS_ALIASES = ("preferred_skills", "preferredSkills", "nice_to_have", "niceToHave")

_GENERIC_TITLES = {
    "careers",
    "career",
    "jobs",
    "job search",
    "search jobs",
    "open positions",
    "opportunities",
    "page not found",
    "access denied",
    "forbidden",
    "home",
}

_ACCESS_DENIED_MARKERS = (
    "access denied",
    "request blocked",
    "forbidden",
    "you do not have permission",
    "verify you are human",
    "captcha",
    "cloudflare ray id",
    "incapsula incident id",
)

_NON_JOB_MARKERS = (
    "page not found",
    "the page you requested could not be found",
    "cookie preferences",
    "privacy policy",
    "terms of use",
)

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_SPLIT_LIST_RE = re.compile(r"[\n\r;,|]+")


class ProductionJobQualityError(RuntimeError):
    """Raised when Phase 6D context or persistence invariants are violated."""


class Phase6DValidationPolicy(ContractModel):
    min_description_chars: int = Field(default=80, ge=1, le=5000)
    min_overall_score: int = Field(default=60, ge=0, le=100)
    require_trusted_host: bool = True
    reject_listing_url: bool = True


class Phase6DQualityScores(ContractModel):
    identity: int = Field(ge=0, le=25)
    title: int = Field(ge=0, le=20)
    description: int = Field(ge=0, le=30)
    company: int = Field(ge=0, le=10)
    location: int = Field(ge=0, le=10)
    posted_date: int = Field(ge=0, le=5)
    overall: int = Field(ge=0, le=100)


class Phase6DValidationResult(ContractModel):
    contract_version: Literal["1.0"] = PHASE_6D_CONTRACT_VERSION
    phase: Literal["6D"] = PHASE_6D
    status: ProcessingStatus
    normalized_job: Phase6CNormalizedJobInput | None = None
    reason_codes: list[QuarantineReasonCode] = Field(default_factory=list)
    reason_messages: list[str] = Field(default_factory=list)
    field_errors: dict[str, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    quality_scores: Phase6DQualityScores
    candidate_identity: str


class Phase6DProcessingResult(ContractModel):
    contract_version: Literal["1.0"] = PHASE_6D_CONTRACT_VERSION
    phase: Literal["6D"] = PHASE_6D
    status: ProcessingStatus
    source_id: str
    raw_evidence_id: str
    quality_scores: Phase6DQualityScores
    reason_codes: list[QuarantineReasonCode] = Field(default_factory=list)
    quarantine_id: str | None = None
    quarantine_inserted: bool = False
    upsert: Phase6CUpsertResult | None = None
    normalized_job_writes_enabled: bool = True
    lifecycle_reconciliation_enabled: bool = False
    deactivation_enabled: bool = False


def _first(payload: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    for key in aliases:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = html.unescape(str(value)).replace("\x00", " ")
    text = _TAG_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text or None


def _clean_list(value: Any) -> list[str]:
    values: list[str] = []
    for item in as_str_list(value):
        parts = _SPLIT_LIST_RE.split(item) if isinstance(item, str) else [str(item)]
        for part in parts:
            normalized = _clean_text(part)
            if normalized:
                values.append(normalized)
    return list(dict.fromkeys(values))


def _clean_url(value: Any) -> str | None:
    text = compact_text(None if value is None else str(value))
    if not text:
        return None
    normalized = canonical_job_url(text)
    return normalized or None


def _valid_http_url(value: str | None) -> bool:
    if not value:
        return False
    parts = urlsplit(value)
    return parts.scheme.lower() in {"http", "https"} and bool(parts.hostname)


def _host(value: str | None) -> str | None:
    if not value:
        return None
    return (urlsplit(value).hostname or "").casefold() or None


def _normalize_host_values(values: Sequence[str] | None) -> set[str]:
    out: set[str] = set()
    for value in values or []:
        host = _host(value) or compact_text(str(value)).casefold().strip(".")
        if host:
            out.add(host)
    return out


def _employment_type(value: Any) -> str | None:
    normalized = _clean_text(value)
    if not normalized:
        return None
    key = normalized.casefold().replace("_", "-")
    mapping = {
        "full time": "Full-time",
        "full-time": "Full-time",
        "fulltime": "Full-time",
        "part time": "Part-time",
        "part-time": "Part-time",
        "parttime": "Part-time",
        "contract": "Contract",
        "contractor": "Contract",
        "temporary": "Temporary",
        "temp": "Temporary",
        "intern": "Internship",
        "internship": "Internship",
    }
    return mapping.get(key, normalized)


def normalize_phase6d_candidate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize common scraper aliases without inventing missing values."""

    canonical_value = _clean_url(_first(payload, _CANONICAL_URL_ALIASES))
    source_url = _clean_url(payload.get("source_url"))
    job_url = _clean_url(_first(payload, ("job_url", "jobUrl", "url")))
    apply_url = _clean_url(_first(payload, _APPLY_URL_ALIASES))
    return {
        "external_job_id": _clean_text(_first(payload, _EXTERNAL_ID_ALIASES)),
        "canonical_url": canonical_value,
        "source_url": source_url,
        "job_url": job_url,
        "apply_url": apply_url,
        "title": _clean_text(_first(payload, _TITLE_ALIASES)),
        "company": _clean_text(_first(payload, _COMPANY_ALIASES)),
        "location_text": _clean_text(_first(payload, _LOCATION_ALIASES)),
        "description": _clean_text(_first(payload, _DESCRIPTION_ALIASES)),
        "employment_type": _employment_type(_first(payload, _EMPLOYMENT_ALIASES)),
        "duration": _clean_text(_first(payload, _DURATION_ALIASES)),
        "compensation_text": _clean_text(_first(payload, _COMPENSATION_ALIASES)),
        "posted_date": _clean_text(_first(payload, _POSTED_DATE_ALIASES)),
        "responsibilities": _clean_list(_first(payload, _RESPONSIBILITIES_ALIASES)),
        "required_skills": _clean_list(_first(payload, _REQUIRED_SKILLS_ALIASES)),
        "preferred_skills": _clean_list(_first(payload, _PREFERRED_SKILLS_ALIASES)),
        "job_reference": _clean_text(_first(payload, _JOB_REFERENCE_ALIASES)),
        "identity_hint": _clean_text(payload.get("identity_hint")),
        "metadata": to_plain_data(dict(payload.get("metadata") or {})),
    }


def _candidate_identity(normalized: Mapping[str, Any], source_id: str) -> str:
    external = compact_text(str(normalized.get("external_job_id") or ""))
    canonical_value = compact_text(str(normalized.get("canonical_url") or normalized.get("job_url") or ""))
    if external:
        return f"external:{source_id}:{external.casefold()}"
    if canonical_value:
        return f"url:{source_id}:{canonical_value}"
    return "fingerprint:" + stable_hash(
        {
            "source_id": source_id,
            "title": normalized.get("title"),
            "company": normalized.get("company"),
            "location_text": normalized.get("location_text"),
            "job_reference": normalized.get("job_reference"),
        }
    )


def validate_phase6d_candidate(
    *,
    source_id: str,
    payload: Mapping[str, Any],
    source_listing_url: str | None = None,
    trusted_hosts: Sequence[str] | None = None,
    policy: Phase6DValidationPolicy | None = None,
) -> Phase6DValidationResult:
    policy = policy or Phase6DValidationPolicy()
    normalized = normalize_phase6d_candidate(payload)
    reasons: list[QuarantineReasonCode] = []
    messages: list[str] = []
    field_errors: dict[str, list[str]] = {}
    warnings: list[str] = []

    def add(code: QuarantineReasonCode, message: str, field: str | None = None) -> None:
        if code not in reasons:
            reasons.append(code)
            messages.append(message)
        if field:
            field_errors.setdefault(field, []).append(message)

    title = normalized.get("title")
    description = normalized.get("description")
    external_id = normalized.get("external_job_id")
    canonical_value = normalized.get("canonical_url") or normalized.get("job_url") or normalized.get("source_url")
    candidate_host = _host(canonical_value)

    if not title:
        add("missing_title", "Job title is required", "title")
    elif str(title).casefold() in _GENERIC_TITLES:
        add("non_job_content", f"Generic non-job title: {title}", "title")

    if not description:
        add("empty_description", "Job description is required", "description")
    else:
        description_text = str(description)
        description_folded = description_text.casefold()
        if any(marker in description_folded for marker in _ACCESS_DENIED_MARKERS):
            add("access_denied_page", "Description contains an access-control marker", "description")
        elif any(marker in description_folded for marker in _NON_JOB_MARKERS) and len(description_text) < 1000:
            add("non_job_content", "Description appears to be a non-job page", "description")
        elif len(description_text) < policy.min_description_chars:
            add(
                "description_too_short",
                f"Description has {len(description_text)} characters; minimum is {policy.min_description_chars}",
                "description",
            )

    if canonical_value and not _valid_http_url(str(canonical_value)):
        add("invalid_canonical_url", "Canonical job URL must be an absolute HTTP(S) URL", "canonical_url")

    listing_canonical = _clean_url(source_listing_url)
    if policy.reject_listing_url and canonical_value and listing_canonical and canonical_value == listing_canonical:
        add("listing_page_selected", "Candidate URL is the source listing page, not a job detail page", "canonical_url")

    allowed_hosts = _normalize_host_values(trusted_hosts)
    if not allowed_hosts:
        listing_host = _host(listing_canonical)
        if listing_host:
            allowed_hosts.add(listing_host)
    if policy.require_trusted_host and candidate_host and allowed_hosts and candidate_host not in allowed_hosts:
        add(
            "untrusted_host",
            f"Candidate host {candidate_host} is not in the evidence-backed trusted host set",
            "canonical_url",
        )

    fallback_evidence = any(
        normalized.get(name)
        for name in ("company", "location_text", "job_reference", "identity_hint")
    )
    if not external_id and not (_valid_http_url(str(canonical_value)) if canonical_value else False) and not (title and fallback_evidence):
        add("missing_identity", "Job requires an external ID, valid canonical URL, or stable fallback identity", "identity")

    if normalized.get("posted_date") and len(str(normalized["posted_date"])) > 300:
        normalized["posted_date"] = None
        warnings.append("posted_date exceeded 300 characters and was removed")

    identity_score = 25 if external_id else 22 if _valid_http_url(str(canonical_value)) else 15 if title and fallback_evidence else 0
    title_score = 20 if title and str(title).casefold() not in _GENERIC_TITLES else 0
    description_len = len(str(description or ""))
    if description_len >= 500:
        description_score = 30
    elif description_len >= policy.min_description_chars:
        description_score = 24
    elif description_len:
        description_score = 8
    else:
        description_score = 0
    company_score = 10 if normalized.get("company") else 0
    location_score = 10 if normalized.get("location_text") else 0
    posted_score = 5 if normalized.get("posted_date") else 0
    overall = identity_score + title_score + description_score + company_score + location_score + posted_score
    scores = Phase6DQualityScores(
        identity=identity_score,
        title=title_score,
        description=description_score,
        company=company_score,
        location=location_score,
        posted_date=posted_score,
        overall=overall,
    )

    if not reasons and overall < policy.min_overall_score:
        add(
            "normalization_failure",
            f"Quality score {overall} is below minimum {policy.min_overall_score}",
        )

    normalized_job: Phase6CNormalizedJobInput | None = None
    if not reasons:
        try:
            normalized_job = Phase6CNormalizedJobInput.model_validate(normalized)
        except Exception as exc:
            add("normalization_failure", f"Normalized job contract failed: {exc}")

    status: ProcessingStatus = "accepted" if not reasons and normalized_job is not None else "quarantined"
    return Phase6DValidationResult(
        status=status,
        normalized_job=normalized_job if status == "accepted" else None,
        reason_codes=reasons,
        reason_messages=messages,
        field_errors=field_errors,
        warnings=warnings,
        quality_scores=scores,
        candidate_identity=_candidate_identity(normalized, source_id),
    )


class ProductionJobQualityRepository:
    """Phase 6D validation/quarantine boundary in front of Phase 6C upserts."""

    def __init__(self, db: Database):
        self.db = db
        self.audit = ProductionIngestionPersistenceRepository(db)
        self.jobs = ProductionJobUpsertRepository(db)

    def _validate_context(
        self,
        *,
        fleet_run_id: str,
        source_run_id: str,
        source_id: str,
        raw_evidence_id: str,
    ) -> tuple[Any, Any, Any]:
        fleet = self.audit.get_fleet_run(fleet_run_id)
        source = self.audit.get_source_run(source_run_id)
        evidence = self.audit.get_raw_evidence(raw_evidence_id)
        if fleet.status != "running" or source.status != "running":
            raise ProductionJobQualityError("Phase 6D requires running fleet and source runs")
        if source.fleet_run_id != fleet_run_id or source.source_id != source_id:
            raise ProductionJobQualityError("Source run does not match Phase 6D context")
        if evidence.fleet_run_id != fleet_run_id or evidence.source_run_id != source_run_id or evidence.source_id != source_id:
            raise ProductionJobQualityError("Raw evidence does not match Phase 6D context")
        controls = fleet.controls
        if controls.get("normalized_job_writes_enabled") is not True:
            raise ProductionJobQualityError("NORMALIZED_JOB_WRITES_DISABLED")
        if controls.get("lifecycle_reconciliation_enabled") is not False:
            raise ProductionJobQualityError("Lifecycle reconciliation must remain disabled")
        if controls.get("deactivation_enabled") is not False:
            raise ProductionJobQualityError("Job deactivation must remain disabled")
        return fleet, source, evidence

    def process_raw_evidence(
        self,
        *,
        fleet_run_id: str,
        source_run_id: str,
        source_id: str,
        raw_evidence_id: str,
        candidate: Mapping[str, Any] | None = None,
        trusted_hosts: Sequence[str] | None = None,
        policy: Phase6DValidationPolicy | None = None,
        observed_at: datetime | None = None,
    ) -> Phase6DProcessingResult:
        _, source, evidence = self._validate_context(
            fleet_run_id=fleet_run_id,
            source_run_id=source_run_id,
            source_id=source_id,
            raw_evidence_id=raw_evidence_id,
        )
        payload = dict(candidate or evidence.payload)
        result = validate_phase6d_candidate(
            source_id=source_id,
            payload=payload,
            source_listing_url=source.source_url,
            trusted_hosts=trusted_hosts,
            policy=policy,
        )

        if result.status == "accepted" and result.normalized_job is not None:
            upsert = self.jobs.upsert_job(
                fleet_run_id=fleet_run_id,
                source_run_id=source_run_id,
                source_id=source_id,
                raw_evidence_id=raw_evidence_id,
                job=result.normalized_job,
                observed_at=observed_at,
            )
            self.db["production_ingestion_source_runs"].update_one(
                {"source_run_id": source_run_id, "status": "running"},
                {"$inc": {"valid_count": 1}, "$set": {"updated_at": utc_now()}},
                upsert=False,
            )
            return Phase6DProcessingResult(
                status="accepted",
                source_id=source_id,
                raw_evidence_id=raw_evidence_id,
                quality_scores=result.quality_scores,
                upsert=upsert,
            )

        quarantine_id = "phase6dquar_" + stable_hash(
            {
                "raw_evidence_id": raw_evidence_id,
                "candidate_identity": result.candidate_identity,
            }
        )[:24]
        doc = ProductionJobQuarantineDocument(
            _id=quarantine_id,
            quarantine_id=quarantine_id,
            fleet_run_id=fleet_run_id,
            source_run_id=source_run_id,
            source_id=source_id,
            raw_evidence_id=raw_evidence_id,
            candidate_identity=result.candidate_identity,
            reason_codes=list(result.reason_codes),
            reason_messages=list(result.reason_messages),
            field_errors=to_plain_data(result.field_errors),
            warnings=list(result.warnings),
            quality_scores=result.quality_scores.model_dump(mode="python"),
            normalized_candidate=normalize_phase6d_candidate(payload),
            review_status="pending",
        )
        write = self.db[doc.collection_name].update_one(
            {"quarantine_id": quarantine_id},
            {"$setOnInsert": doc.to_mongo()},
            upsert=True,
        )
        inserted = bool(getattr(write, "upserted_id", None))
        if inserted:
            self.db["production_ingestion_source_runs"].update_one(
                {"source_run_id": source_run_id, "status": "running"},
                {"$inc": {"quarantined_count": 1}, "$set": {"updated_at": utc_now()}},
                upsert=False,
            )
        return Phase6DProcessingResult(
            status="quarantined",
            source_id=source_id,
            raw_evidence_id=raw_evidence_id,
            quality_scores=result.quality_scores,
            reason_codes=list(result.reason_codes),
            quarantine_id=quarantine_id,
            quarantine_inserted=inserted,
        )

    def get_quarantine(self, quarantine_id: str) -> ProductionJobQuarantineDocument:
        payload = self.db[ProductionJobQuarantineDocument.collection_name].find_one(
            {"quarantine_id": quarantine_id}
        )
        if not payload:
            raise ProductionJobQualityError(f"Unknown quarantine_id: {quarantine_id}")
        return ProductionJobQuarantineDocument.from_mongo(payload)

    def delete_validation_records(self, *, fleet_run_id: str, job_id: str | None = None) -> dict[str, int]:
        quarantine = self.db[ProductionJobQuarantineDocument.collection_name].delete_many(
            {"fleet_run_id": fleet_run_id}
        )
        job_counts = self.jobs.delete_validation_job(job_id) if job_id else {"jobs_current": 0, "jobs_history": 0}
        audit_counts = self.audit.delete_validation_run(fleet_run_id)
        return {
            **job_counts,
            **audit_counts,
            "job_quarantine": int(getattr(quarantine, "deleted_count", 0)),
        }


def init_phase6d_indexes(db: Database) -> dict[str, int]:
    from ..warehouse.indexes import INDEXES

    name = ProductionJobQuarantineDocument.collection_name
    db[name].create_indexes(INDEXES[name])
    return {name: len(INDEXES[name])}

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import Field, field_validator, model_validator
from pymongo.database import Database

from .contracts import ContractModel
from .production_ingestion import Phase6AIngestionPlan, Phase6ASource, ProductionIngestionError
from ..warehouse.base import utc_now
from ..warehouse.documents import (
    ProductionIngestionFleetRunDocument,
    ProductionIngestionSourceRunDocument,
    ProductionRawJobEvidenceDocument,
)
from ..warehouse.hashing import stable_hash
from ..warehouse.serializers import to_plain_data
from ..warehouse.url_utils import canonical_job_url


PHASE_6B_CONTRACT_VERSION = "1.0"
PHASE_6B = "6B"
TERMINAL_SOURCE_STATUSES = {"success", "failed", "blocked", "cancelled"}


class ProductionPersistenceError(RuntimeError):
    """Raised when Phase 6B persistence invariants are violated."""


def _require_aware_utc(value: datetime, *, field_name: str) -> datetime:
    """Validate caller-supplied datetimes and normalize them to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ProductionPersistenceError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _mongo_datetime_as_utc(value: datetime | None) -> datetime | None:
    """Normalize a datetime read from MongoDB.

    MongoDB stores BSON datetimes as UTC. PyMongo returns timezone-naive UTC
    values unless the client is configured with ``tz_aware=True``. Treating a
    naive value read from Mongo as UTC is therefore correct and prevents mixed
    naive/aware arithmetic during source and fleet finalization.
    """

    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Phase6BSourceCounters(ContractModel):
    discovered_count: int = Field(default=0, ge=0)
    attempted_count: int = Field(default=0, ge=0)
    extracted_count: int = Field(default=0, ge=0)
    valid_count: int = Field(default=0, ge=0)
    quarantined_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_classification_totals(self) -> "Phase6BSourceCounters":
        classified = self.valid_count + self.quarantined_count + self.rejected_count
        if classified > self.extracted_count:
            raise ValueError(
                "valid_count + quarantined_count + rejected_count cannot exceed extracted_count"
            )
        return self


class Phase6BRawEvidenceInput(ContractModel):
    source_url: str | None = None
    canonical_url: str | None = None
    external_job_id: str | None = None
    payload: dict[str, Any]
    payload_format: Literal[
        "json",
        "html",
        "json_ld",
        "api_response",
        "rendered_state",
        "normalized_scraper_output",
        "text",
    ] = "normalized_scraper_output"
    extracted_at: datetime = Field(default_factory=utc_now)
    extractor_name: str = Field(min_length=1, max_length=200)
    extractor_version: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extracted_at")
    @classmethod
    def validate_extracted_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("extracted_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def require_job_identity_evidence(self) -> "Phase6BRawEvidenceInput":
        if not any(
            str(value or "").strip()
            for value in (self.external_job_id, self.canonical_url, self.source_url)
        ):
            raise ValueError(
                "raw evidence requires external_job_id, canonical_url, or source_url"
            )
        return self


class Phase6BLiveValidationResult(ContractModel):
    contract_version: Literal["1.0"] = PHASE_6B_CONTRACT_VERSION
    phase: Literal["6B"] = PHASE_6B
    fleet_run_id: str
    source_run_id: str
    evidence_id: str
    source_id: str
    evidence_inserted: bool
    duplicate_evidence_inserted: bool
    fleet_status: str
    source_status: str
    raw_evidence_count: int
    normalized_job_writes_enabled: bool = False
    lifecycle_reconciliation_enabled: bool = False
    deactivation_enabled: bool = False



def read_phase6a_ingestion_plan(path: Path) -> Phase6AIngestionPlan:
    source = Path(path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Phase 6A ingestion plan does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ProductionPersistenceError(f"Phase 6A plan contains invalid JSON: {source}") from exc
    try:
        plan = Phase6AIngestionPlan.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise ProductionPersistenceError(f"Invalid Phase 6A ingestion plan: {exc}") from exc
    if plan.controls.get("execution_mode") != "plan_only":
        raise ProductionPersistenceError("Phase 6B requires an immutable Phase 6A plan_only plan")
    for key in (
        "production_writes_enabled",
        "lifecycle_reconciliation_enabled",
        "deactivation_enabled",
    ):
        if plan.controls.get(key) is not False:
            raise ProductionPersistenceError(f"Unsafe Phase 6A control in plan: {key}")
    return plan



def _selected_sources(
    plan: Phase6AIngestionPlan,
    selected_source_ids: Sequence[str] | None,
) -> list[Phase6ASource]:
    requested = [str(value or "").strip() for value in selected_source_ids or []]
    if any(not value for value in requested):
        raise ProductionPersistenceError("selected_source_ids cannot contain empty values")
    if len(requested) != len(set(requested)):
        raise ProductionPersistenceError("selected_source_ids cannot contain duplicates")
    if not requested:
        return list(plan.sources)
    allowed = set(plan.selected_source_ids)
    rejected = [value for value in requested if value not in allowed]
    if rejected:
        raise ProductionPersistenceError(
            "SOURCE_NOT_IN_PHASE_6A_PLAN: " + ", ".join(rejected)
        )
    requested_set = set(requested)
    return [source for source in plan.sources if source.source_id in requested_set]



def _fleet_run_id(plan: Phase6AIngestionPlan, started_at: datetime) -> str:
    nonce = uuid.uuid4().hex[:8]
    seed = {
        "plan_id": plan.plan_id,
        "cohort_sha256": plan.cohort_sha256,
        "started_at": started_at.isoformat(),
        "nonce": nonce,
    }
    return f"phase6b_{started_at.strftime('%Y%m%dT%H%M%SZ')}_{stable_hash(seed)[:12]}"



def _source_run_id(fleet_run_id: str, source_id: str, attempt_number: int) -> str:
    return "phase6bsrc_" + stable_hash(
        {
            "fleet_run_id": fleet_run_id,
            "source_id": source_id,
            "attempt_number": attempt_number,
        }
    )[:24]



def _evidence_identity(
    *,
    source_run_id: str,
    source_id: str,
    payload_sha256: str,
    external_job_id: str | None,
    canonical_url_value: str | None,
    source_url: str | None,
) -> str:
    return "phase6bevd_" + stable_hash(
        {
            "source_run_id": source_run_id,
            "source_id": source_id,
            "payload_sha256": payload_sha256,
            "external_job_id": str(external_job_id or "").strip(),
            "canonical_url": str(canonical_url_value or "").strip(),
            "source_url": str(source_url or "").strip(),
        }
    )[:24]


class ProductionIngestionPersistenceRepository:
    """MongoDB persistence boundary for Phase 6B audit and raw evidence records.

    This step intentionally does not write normalized jobs, reconcile missing jobs,
    or deactivate jobs. Those controls remain hard-disabled in every fleet record.
    """

    def __init__(self, db: Database):
        self.db = db

    def create_fleet_run(
        self,
        *,
        plan: Phase6AIngestionPlan,
        selected_source_ids: Sequence[str] | None = None,
        fleet_run_id: str | None = None,
        started_at: datetime | None = None,
        normalized_job_writes_enabled: bool = False,
    ) -> ProductionIngestionFleetRunDocument:
        started = _require_aware_utc(
            started_at or datetime.now(timezone.utc),
            field_name="started_at",
        )
        sources = _selected_sources(plan, selected_source_ids)
        if not sources:
            raise ProductionPersistenceError("A Phase 6B fleet run requires at least one source")
        run_id = str(fleet_run_id or _fleet_run_id(plan, started)).strip()
        if not run_id:
            raise ProductionPersistenceError("fleet_run_id cannot be empty")
        controls = {
            "persistence_writes_enabled": True,
            "normalized_job_writes_enabled": bool(normalized_job_writes_enabled),
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
        }
        doc = ProductionIngestionFleetRunDocument(
            _id=run_id,
            fleet_run_id=run_id,
            plan_id=plan.plan_id,
            cohort_sha256=plan.cohort_sha256,
            status="running",
            started_at=started,
            selected_source_ids=[source.source_id for source in sources],
            requested_source_count=len(sources),
            controls=controls,
        )
        self.db[doc.collection_name].update_one(
            {"fleet_run_id": run_id},
            {"$setOnInsert": doc.to_mongo()},
            upsert=True,
        )
        stored = self.db[doc.collection_name].find_one({"fleet_run_id": run_id})
        if not stored:
            raise ProductionPersistenceError("Fleet run was not persisted")
        existing = ProductionIngestionFleetRunDocument.from_mongo(stored)
        if existing.plan_id != plan.plan_id or existing.cohort_sha256 != plan.cohort_sha256:
            raise ProductionPersistenceError("fleet_run_id already belongs to another plan")
        return existing

    def start_source_run(
        self,
        *,
        fleet_run_id: str,
        source: Phase6ASource,
        attempt_number: int = 1,
        source_run_id: str | None = None,
        started_at: datetime | None = None,
        extractor_version: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProductionIngestionSourceRunDocument:
        if attempt_number < 1:
            raise ProductionPersistenceError("attempt_number must be at least 1")
        fleet = self.get_fleet_run(fleet_run_id)
        if fleet.status != "running":
            raise ProductionPersistenceError("Cannot start a source under a terminal fleet run")
        if source.source_id not in fleet.selected_source_ids:
            raise ProductionPersistenceError(
                f"SOURCE_NOT_IN_FLEET_RUN: {source.source_id}"
            )
        started = _require_aware_utc(
            started_at or datetime.now(timezone.utc),
            field_name="started_at",
        )
        run_id = str(
            source_run_id
            or _source_run_id(fleet_run_id, source.source_id, attempt_number)
        ).strip()
        doc = ProductionIngestionSourceRunDocument(
            _id=run_id,
            source_run_id=run_id,
            fleet_run_id=fleet_run_id,
            source_id=source.source_id,
            source_url=source.listing_url,
            resolved_route_url=source.resolved_route_url,
            status="running",
            attempt_number=attempt_number,
            started_at=started,
            extractor_version=extractor_version,
            metadata=to_plain_data(dict(metadata or {})),
        )
        self.db[doc.collection_name].update_one(
            {"source_run_id": run_id},
            {"$setOnInsert": doc.to_mongo()},
            upsert=True,
        )
        stored = self.db[doc.collection_name].find_one({"source_run_id": run_id})
        if not stored:
            raise ProductionPersistenceError("Source run was not persisted")
        existing = ProductionIngestionSourceRunDocument.from_mongo(stored)
        if existing.fleet_run_id != fleet_run_id or existing.source_id != source.source_id:
            raise ProductionPersistenceError("source_run_id already belongs to another source")
        return existing

    def store_raw_evidence(
        self,
        *,
        fleet_run_id: str,
        source_run_id: str,
        source_id: str,
        evidence: Phase6BRawEvidenceInput,
    ) -> tuple[ProductionRawJobEvidenceDocument, bool]:
        source_run = self.get_source_run(source_run_id)
        if source_run.fleet_run_id != fleet_run_id or source_run.source_id != source_id:
            raise ProductionPersistenceError("Raw evidence does not match its source run")
        if source_run.status != "running":
            raise ProductionPersistenceError("Raw evidence can only be added to a running source run")

        payload = to_plain_data(evidence.payload)
        payload_sha256 = stable_hash(payload)
        canonical_value = canonical_job_url(evidence.canonical_url or evidence.source_url)
        evidence_id = _evidence_identity(
            source_run_id=source_run_id,
            source_id=source_id,
            payload_sha256=payload_sha256,
            external_job_id=evidence.external_job_id,
            canonical_url_value=canonical_value,
            source_url=evidence.source_url,
        )
        doc = ProductionRawJobEvidenceDocument(
            _id=evidence_id,
            evidence_id=evidence_id,
            fleet_run_id=fleet_run_id,
            source_run_id=source_run_id,
            source_id=source_id,
            source_url=evidence.source_url,
            canonical_url=canonical_value or None,
            external_job_id=str(evidence.external_job_id or "").strip() or None,
            payload=payload,
            payload_format=evidence.payload_format,
            payload_sha256=payload_sha256,
            extracted_at=evidence.extracted_at,
            extractor_name=evidence.extractor_name,
            extractor_version=evidence.extractor_version,
            metadata=to_plain_data(evidence.metadata),
        )
        result = self.db[doc.collection_name].update_one(
            {"evidence_id": evidence_id},
            {"$setOnInsert": doc.to_mongo()},
            upsert=True,
        )
        inserted = bool(getattr(result, "upserted_id", None))
        stored = self.db[doc.collection_name].find_one({"evidence_id": evidence_id})
        if not stored:
            raise ProductionPersistenceError("Raw evidence was not persisted")
        existing = ProductionRawJobEvidenceDocument.from_mongo(stored)
        if existing.payload_sha256 != payload_sha256:
            raise ProductionPersistenceError("Immutable raw evidence hash mismatch")
        return existing, inserted

    def finalize_source_run(
        self,
        *,
        source_run_id: str,
        status: Literal["success", "failed", "blocked", "cancelled"],
        counters: Phase6BSourceCounters,
        completed_at: datetime | None = None,
        acquisition_strategy: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProductionIngestionSourceRunDocument:
        existing = self.get_source_run(source_run_id)
        if existing.status in TERMINAL_SOURCE_STATUSES:
            if existing.status != status:
                raise ProductionPersistenceError("Source run is already terminal")
            return existing
        if existing.status != "running":
            raise ProductionPersistenceError("Only a running source can be finalized")
        completed = _require_aware_utc(
            completed_at or datetime.now(timezone.utc),
            field_name="completed_at",
        )
        started = _mongo_datetime_as_utc(existing.started_at)
        if started and completed < started:
            raise ProductionPersistenceError("completed_at cannot be before started_at")
        if status == "success" and (error_type or error_message):
            raise ProductionPersistenceError("Successful source runs cannot contain errors")
        raw_count = self.db[ProductionRawJobEvidenceDocument.collection_name].count_documents(
            {"source_run_id": source_run_id}
        )
        elapsed = (completed - started).total_seconds() if started else None
        merged_metadata = dict(existing.metadata)
        merged_metadata.update(to_plain_data(dict(metadata or {})))
        update = {
            "status": status,
            "completed_at": completed,
            "discovered_count": counters.discovered_count,
            "attempted_count": counters.attempted_count,
            "extracted_count": counters.extracted_count,
            "raw_evidence_count": int(raw_count),
            "valid_count": counters.valid_count,
            "quarantined_count": counters.quarantined_count,
            "rejected_count": counters.rejected_count,
            "elapsed_seconds": elapsed,
            "acquisition_strategy": acquisition_strategy,
            "error_type": error_type,
            "error_message": error_message,
            "metadata": merged_metadata,
            "updated_at": utc_now(),
        }
        self.db[ProductionIngestionSourceRunDocument.collection_name].update_one(
            {"source_run_id": source_run_id, "status": "running"},
            {"$set": update},
            upsert=False,
        )
        return self.get_source_run(source_run_id)

    def finalize_fleet_run(
        self,
        *,
        fleet_run_id: str,
        completed_at: datetime | None = None,
    ) -> ProductionIngestionFleetRunDocument:
        fleet = self.get_fleet_run(fleet_run_id)
        if fleet.status != "running":
            return fleet
        source_rows = list(
            self.db[ProductionIngestionSourceRunDocument.collection_name].find(
                {"fleet_run_id": fleet_run_id}
            )
        )
        by_source = {str(row.get("source_id") or ""): row for row in source_rows}
        missing = [source_id for source_id in fleet.selected_source_ids if source_id not in by_source]
        if missing:
            raise ProductionPersistenceError(
                "Cannot finalize fleet; source runs are missing for: " + ", ".join(missing)
            )
        nonterminal = [
            source_id
            for source_id, row in by_source.items()
            if str(row.get("status") or "") not in TERMINAL_SOURCE_STATUSES
        ]
        if nonterminal:
            raise ProductionPersistenceError(
                "Cannot finalize fleet; source runs are not terminal: " + ", ".join(nonterminal)
            )
        completed = _require_aware_utc(
            completed_at or datetime.now(timezone.utc),
            field_name="completed_at",
        )
        fleet_started = _mongo_datetime_as_utc(fleet.started_at)
        if fleet_started and completed < fleet_started:
            raise ProductionPersistenceError("completed_at cannot be before started_at")

        statuses = [str(row.get("status") or "") for row in source_rows]
        success_count = statuses.count("success")
        failed_count = statuses.count("failed")
        blocked_count = statuses.count("blocked")
        cancelled_count = statuses.count("cancelled")
        if success_count == len(statuses):
            fleet_status = "completed"
        elif success_count > 0:
            fleet_status = "completed_with_failures"
        elif cancelled_count == len(statuses):
            fleet_status = "cancelled"
        else:
            fleet_status = "failed"

        errors = [
            {
                "source_id": row.get("source_id"),
                "status": row.get("status"),
                "error_type": row.get("error_type"),
                "error_message": row.get("error_message"),
            }
            for row in source_rows
            if row.get("status") != "success"
        ]
        raw_count = self.db[ProductionRawJobEvidenceDocument.collection_name].count_documents(
            {"fleet_run_id": fleet_run_id}
        )
        update = {
            "status": fleet_status,
            "completed_at": completed,
            "completed_source_count": len(source_rows),
            "successful_source_count": success_count,
            "failed_source_count": failed_count,
            "blocked_source_count": blocked_count,
            "cancelled_source_count": cancelled_count,
            "raw_evidence_count": int(raw_count),
            "inserted_job_count": sum(int(row.get("inserted_job_count") or 0) for row in source_rows),
            "updated_job_count": sum(int(row.get("updated_job_count") or 0) for row in source_rows),
            "unchanged_job_count": sum(int(row.get("unchanged_job_count") or 0) for row in source_rows),
            "reactivated_job_count": sum(int(row.get("reactivated_job_count") or 0) for row in source_rows),
            "quarantined_job_count": sum(int(row.get("quarantined_count") or 0) for row in source_rows),
            "error_summary": errors,
            "updated_at": utc_now(),
        }
        self.db[ProductionIngestionFleetRunDocument.collection_name].update_one(
            {"fleet_run_id": fleet_run_id, "status": "running"},
            {"$set": update},
            upsert=False,
        )
        return self.get_fleet_run(fleet_run_id)

    def get_fleet_run(self, fleet_run_id: str) -> ProductionIngestionFleetRunDocument:
        payload = self.db[ProductionIngestionFleetRunDocument.collection_name].find_one(
            {"fleet_run_id": fleet_run_id}
        )
        if not payload:
            raise ProductionPersistenceError(f"Unknown fleet_run_id: {fleet_run_id}")
        return ProductionIngestionFleetRunDocument.from_mongo(payload)

    def get_source_run(self, source_run_id: str) -> ProductionIngestionSourceRunDocument:
        payload = self.db[ProductionIngestionSourceRunDocument.collection_name].find_one(
            {"source_run_id": source_run_id}
        )
        if not payload:
            raise ProductionPersistenceError(f"Unknown source_run_id: {source_run_id}")
        return ProductionIngestionSourceRunDocument.from_mongo(payload)

    def get_raw_evidence(self, evidence_id: str) -> ProductionRawJobEvidenceDocument:
        payload = self.db[ProductionRawJobEvidenceDocument.collection_name].find_one(
            {"evidence_id": evidence_id}
        )
        if not payload:
            raise ProductionPersistenceError(f"Unknown evidence_id: {evidence_id}")
        return ProductionRawJobEvidenceDocument.from_mongo(payload)

    def delete_validation_run(self, fleet_run_id: str) -> dict[str, int]:
        """Delete only records belonging to an explicitly supplied validation run."""

        source_result = self.db[
            ProductionIngestionSourceRunDocument.collection_name
        ].delete_many({"fleet_run_id": fleet_run_id})
        evidence_result = self.db[
            ProductionRawJobEvidenceDocument.collection_name
        ].delete_many({"fleet_run_id": fleet_run_id})
        fleet_result = self.db[
            ProductionIngestionFleetRunDocument.collection_name
        ].delete_many({"fleet_run_id": fleet_run_id})
        return {
            "fleet_runs": int(getattr(fleet_result, "deleted_count", 0)),
            "source_runs": int(getattr(source_result, "deleted_count", 0)),
            "raw_evidence": int(getattr(evidence_result, "deleted_count", 0)),
        }


def init_phase6b_indexes(db: Database) -> dict[str, int]:
    """Create only the three Phase 6B collection indexes."""

    from ..warehouse.indexes import INDEXES

    collections = (
        ProductionIngestionFleetRunDocument.collection_name,
        ProductionIngestionSourceRunDocument.collection_name,
        ProductionRawJobEvidenceDocument.collection_name,
    )
    created: dict[str, int] = {}
    for collection_name in collections:
        indexes = INDEXES[collection_name]
        db[collection_name].create_indexes(indexes)
        created[collection_name] = len(indexes)
    return created

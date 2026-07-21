from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

import pytest

from src.portals.production_ingestion import Phase6AIngestionPlan, Phase6ASource
from src.portals.production_persistence import (
    Phase6BRawEvidenceInput,
    Phase6BSourceCounters,
    ProductionIngestionPersistenceRepository,
)
from src.portals.production_quality import (
    Phase6DValidationPolicy,
    ProductionJobQualityError,
    ProductionJobQualityRepository,
    normalize_phase6d_candidate,
    validate_phase6d_candidate,
)
from src.warehouse.indexes import INDEXES


class _UpdateResult:
    def __init__(self, *, upserted_id: str | None = None, matched_count: int = 0):
        self.upserted_id = upserted_id
        self.matched_count = matched_count
        self.modified_count = matched_count


class _DeleteResult:
    def __init__(self, deleted_count: int):
        self.deleted_count = deleted_count


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        actual = document.get(key)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif actual != expected:
            return False
    return True


class _FakeCollection:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    def update_one(self, query, update, *, upsert=False):
        key = next((key for key, value in self.documents.items() if _matches(value, query)), None)
        if key is None:
            if not upsert:
                return _UpdateResult()
            payload = copy.deepcopy(update.get("$setOnInsert") or {})
            payload.update(copy.deepcopy(update.get("$set") or {}))
            document_id = str(payload.get("_id") or "fake_" + str(len(self.documents) + 1))
            payload["_id"] = document_id
            self.documents[document_id] = payload
            return _UpdateResult(upserted_id=document_id)
        payload = self.documents[key]
        payload.update(copy.deepcopy(update.get("$set") or {}))
        for field, amount in (update.get("$inc") or {}).items():
            payload[field] = int(payload.get(field) or 0) + int(amount)
        return _UpdateResult(matched_count=1)

    def find_one(self, query, projection=None):
        del projection
        for document in self.documents.values():
            if _matches(document, query):
                return copy.deepcopy(document)
        return None

    def find(self, query, projection=None):
        del projection
        return [copy.deepcopy(document) for document in self.documents.values() if _matches(document, query)]

    def count_documents(self, query):
        return sum(1 for document in self.documents.values() if _matches(document, query))

    def delete_many(self, query):
        keys = [key for key, value in self.documents.items() if _matches(value, query)]
        for key in keys:
            self.documents.pop(key, None)
        return _DeleteResult(len(keys))

    def create_indexes(self, indexes):
        return [str(index.document) for index in indexes]


class _FakeDatabase:
    def __init__(self) -> None:
        self.collections: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        return self.collections.setdefault(name, _FakeCollection())


def _source() -> Phase6ASource:
    return Phase6ASource(
        source_id="source_a",
        source_row=1,
        display_name="Source A",
        listing_url="https://source.example/jobs",
        detected_platform="custom_listing",
        bounded_extracted_jobs=10,
        evidence_run_id="cert-run",
    )


def _plan() -> Phase6AIngestionPlan:
    source = _source()
    return Phase6AIngestionPlan(
        plan_id="phase6a-test-plan",
        generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        cohort_sha256="a" * 64,
        cohort_source_count=1,
        selected_source_count=1,
        deferred_source_count=0,
        selected_source_ids=[source.source_id],
        sources=[source],
        controls={
            "execution_mode": "plan_only",
            "max_source_concurrency": 1,
            "source_timeout_seconds": 600,
            "production_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
        },
    )


def _valid_payload(*, description: str | None = None) -> dict[str, Any]:
    return {
        "jobId": "REQ-123",
        "jobUrl": "https://source.example/jobs/123?utm_source=test",
        "jobTitle": "  Senior   Data Engineer ",
        "companyName": " Acme ",
        "jobLocation": " Remote ",
        "jobDescription": description
        or "Design, build, test, operate, and monitor reliable production data pipelines using Python and MongoDB for critical customer workflows.",
        "jobType": "full time",
        "datePosted": "2026-07-17",
        "skills": ["Python", " MongoDB ", "Python"],
    }


def _context(*, normalized_writes: bool = True, payload: dict[str, Any] | None = None):
    db = _FakeDatabase()
    audit = ProductionIngestionPersistenceRepository(db)
    plan = _plan()
    fleet = audit.create_fleet_run(
        plan=plan,
        fleet_run_id="fleet_1",
        normalized_job_writes_enabled=normalized_writes,
    )
    source_run = audit.start_source_run(fleet_run_id=fleet.fleet_run_id, source=plan.sources[0])
    candidate = payload or _valid_payload()
    evidence, _ = audit.store_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        evidence=Phase6BRawEvidenceInput(
            source_url="https://source.example/jobs/123",
            canonical_url="https://source.example/jobs/123",
            external_job_id=str(candidate.get("jobId") or candidate.get("external_job_id") or "REQ-123"),
            payload=candidate,
            extractor_name="phase6d-test",
        ),
    )
    return db, audit, fleet, source_run, evidence


def test_common_aliases_are_normalized_without_inventing_values() -> None:
    normalized = normalize_phase6d_candidate(_valid_payload())
    assert normalized["external_job_id"] == "REQ-123"
    assert normalized["canonical_url"] == "https://source.example/jobs/123"
    assert normalized["title"] == "Senior Data Engineer"
    assert normalized["employment_type"] == "Full-time"
    assert normalized["required_skills"] == ["Python", "MongoDB"]
    assert normalized["compensation_text"] is None


def test_valid_candidate_is_accepted_with_quality_score() -> None:
    result = validate_phase6d_candidate(
        source_id="source_a",
        payload=_valid_payload(),
        source_listing_url="https://source.example/jobs",
        trusted_hosts=["source.example"],
    )
    assert result.status == "accepted"
    assert result.normalized_job is not None
    assert result.quality_scores.overall >= 60
    assert result.reason_codes == []


def test_generic_find_work_title_drift_is_quarantined() -> None:
    payload = _valid_payload()
    payload["jobTitle"] = "FIND WORK"
    result = validate_phase6d_candidate(
        source_id="source_a",
        payload=payload,
        source_listing_url="https://source.example/jobs",
        trusted_hosts=["source.example"],
    )
    assert result.status == "quarantined"
    assert "non_job_content" in result.reason_codes


def test_missing_title_and_short_description_are_quarantined() -> None:
    payload = _valid_payload(description="Too short")
    payload.pop("jobTitle")
    result = validate_phase6d_candidate(
        source_id="source_a",
        payload=payload,
        source_listing_url="https://source.example/jobs",
        trusted_hosts=["source.example"],
    )
    assert result.status == "quarantined"
    assert "missing_title" in result.reason_codes
    assert "description_too_short" in result.reason_codes


def test_access_denied_payload_is_quarantined() -> None:
    payload = _valid_payload(description="Access denied. Verify you are human before continuing to this protected page.")
    result = validate_phase6d_candidate(
        source_id="source_a",
        payload=payload,
        trusted_hosts=["source.example"],
    )
    assert result.status == "quarantined"
    assert "access_denied_page" in result.reason_codes


def test_listing_page_and_untrusted_host_are_rejected() -> None:
    listing = _valid_payload()
    listing["jobUrl"] = "https://source.example/jobs"
    result = validate_phase6d_candidate(
        source_id="source_a",
        payload=listing,
        source_listing_url="https://source.example/jobs",
        trusted_hosts=["source.example"],
    )
    assert "listing_page_selected" in result.reason_codes

    external = _valid_payload()
    external["jobUrl"] = "https://evil.example/jobs/123"
    external_result = validate_phase6d_candidate(
        source_id="source_a",
        payload=external,
        source_listing_url="https://source.example/jobs",
        trusted_hosts=["source.example"],
    )
    assert "untrusted_host" in external_result.reason_codes


def test_repository_accepts_valid_job_and_writes_one_current_record() -> None:
    db, _, fleet, source_run, evidence = _context()
    result = ProductionJobQualityRepository(db).process_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        raw_evidence_id=evidence.evidence_id,
        trusted_hosts=["source.example"],
    )
    assert result.status == "accepted"
    assert result.upsert is not None and result.upsert.outcome == "inserted"
    assert db["jobs_current"].count_documents({}) == 1
    assert db["production_job_quarantine"].count_documents({}) == 0


def test_quarantine_is_idempotent_and_counter_increments_once() -> None:
    payload = _valid_payload(description="Access denied. Verify you are human before continuing.")
    db, audit, fleet, source_run, evidence = _context(payload=payload)
    repo = ProductionJobQualityRepository(db)
    first = repo.process_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        raw_evidence_id=evidence.evidence_id,
        trusted_hosts=["source.example"],
    )
    second = repo.process_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        raw_evidence_id=evidence.evidence_id,
        trusted_hosts=["source.example"],
    )
    assert first.status == second.status == "quarantined"
    assert first.quarantine_inserted is True
    assert second.quarantine_inserted is False
    assert first.quarantine_id == second.quarantine_id
    assert db["production_job_quarantine"].count_documents({}) == 1
    assert audit.get_source_run(source_run.source_run_id).quarantined_count == 1


def test_invalid_followup_does_not_overwrite_existing_valid_job() -> None:
    db, audit, fleet, source_run, evidence = _context()
    repo = ProductionJobQualityRepository(db)
    accepted = repo.process_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        raw_evidence_id=evidence.evidence_id,
        trusted_hosts=["source.example"],
    )
    before = copy.deepcopy(db["jobs_current"].find_one({"job_id": accepted.upsert.job_id}))

    invalid_payload = _valid_payload(description="Access denied")
    invalid_evidence, _ = audit.store_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        evidence=Phase6BRawEvidenceInput(
            source_url="https://source.example/jobs/123",
            canonical_url="https://source.example/jobs/123",
            external_job_id="REQ-123",
            payload=invalid_payload,
            extractor_name="phase6d-test",
        ),
    )
    quarantined = repo.process_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        raw_evidence_id=invalid_evidence.evidence_id,
        trusted_hosts=["source.example"],
    )
    after = db["jobs_current"].find_one({"job_id": accepted.upsert.job_id})
    assert quarantined.status == "quarantined"
    assert after["content_hash"] == before["content_hash"]
    assert after["version"] == before["version"] == 1


def test_quality_processing_requires_normalized_write_control() -> None:
    db, _, fleet, source_run, evidence = _context(normalized_writes=False)
    with pytest.raises(ProductionJobQualityError, match="NORMALIZED_JOB_WRITES_DISABLED"):
        ProductionJobQualityRepository(db).process_raw_evidence(
            fleet_run_id=fleet.fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source_run.source_id,
            raw_evidence_id=evidence.evidence_id,
            trusted_hosts=["source.example"],
        )


def test_source_and_fleet_quality_counters_finalize_consistently() -> None:
    db, audit, fleet, source_run, evidence = _context()
    repo = ProductionJobQualityRepository(db)
    accepted = repo.process_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        raw_evidence_id=evidence.evidence_id,
        trusted_hosts=["source.example"],
    )
    invalid_payload = _valid_payload(description="Access denied")
    invalid_evidence, _ = audit.store_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        evidence=Phase6BRawEvidenceInput(
            source_url="https://source.example/jobs/123",
            canonical_url="https://source.example/jobs/123",
            external_job_id="REQ-123",
            payload=invalid_payload,
            extractor_name="phase6d-test",
        ),
    )
    repo.process_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        raw_evidence_id=invalid_evidence.evidence_id,
        trusted_hosts=["source.example"],
    )
    source = audit.finalize_source_run(
        source_run_id=source_run.source_run_id,
        status="success",
        counters=Phase6BSourceCounters(
            discovered_count=2,
            attempted_count=2,
            extracted_count=2,
            valid_count=1,
            quarantined_count=1,
        ),
    )
    finalized = audit.finalize_fleet_run(fleet_run_id=fleet.fleet_run_id)
    assert accepted.upsert.outcome == "inserted"
    assert source.valid_count == 1
    assert source.quarantined_count == 1
    assert finalized.inserted_job_count == 1
    assert finalized.quarantined_job_count == 1


def test_phase6d_indexes_protect_quarantine_identity() -> None:
    indexes = INDEXES["production_job_quarantine"]
    assert len(indexes) == 5
    assert any(index.document.get("unique") and "quarantine_id" in str(index.document) for index in indexes)
    assert any(index.document.get("unique") and "candidate_identity" in str(index.document) for index in indexes)

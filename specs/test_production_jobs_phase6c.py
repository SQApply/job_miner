from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

import pytest

from src.portals.production_ingestion import Phase6AIngestionPlan, Phase6ASource
from src.portals.production_jobs import (
    Phase6CNormalizedJobInput,
    ProductionJobUpsertError,
    ProductionJobUpsertRepository,
    build_phase6c_content_hash,
    build_phase6c_identity,
)
from src.portals.production_persistence import (
    Phase6BRawEvidenceInput,
    Phase6BSourceCounters,
    ProductionIngestionPersistenceRepository,
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


def _job(*, description: str = "Build data pipelines", posted_date: str = "3 days ago") -> Phase6CNormalizedJobInput:
    return Phase6CNormalizedJobInput(
        external_job_id="REQ-123",
        canonical_url="https://source.example/jobs/123?utm_source=test",
        job_url="https://source.example/jobs/123?utm_campaign=x",
        apply_url="https://source.example/jobs/123/apply?ref=careers",
        title="  Senior   Data Engineer ",
        company=" Acme ",
        location_text=" Remote ",
        description=description,
        employment_type=" Full Time ",
        posted_date=posted_date,
        required_skills=["Python", " Spark ", "Python"],
    )


def _context(*, normalized_writes: bool = True):
    db = _FakeDatabase()
    audit = ProductionIngestionPersistenceRepository(db)
    plan = _plan()
    fleet = audit.create_fleet_run(
        plan=plan,
        fleet_run_id="fleet_1",
        normalized_job_writes_enabled=normalized_writes,
    )
    source_run = audit.start_source_run(fleet_run_id=fleet.fleet_run_id, source=plan.sources[0])
    evidence, _ = audit.store_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        evidence=Phase6BRawEvidenceInput(
            source_url="https://source.example/jobs/123",
            canonical_url="https://source.example/jobs/123",
            external_job_id="REQ-123",
            payload=_job().model_dump(mode="python"),
            extractor_name="phase6c-test",
        ),
    )
    return db, audit, fleet, source_run, evidence


def test_identity_prefers_external_job_id_over_url() -> None:
    first = build_phase6c_identity(source_id="source_a", job=_job())
    changed_url = _job().model_copy(update={"canonical_url": "https://other.example/jobs/999"})
    second = build_phase6c_identity(source_id="source_a", job=changed_url)
    assert first.strategy == "external_job_id"
    assert first.identity_hash == second.identity_hash


def test_canonical_url_identity_removes_tracking_parameters() -> None:
    first = Phase6CNormalizedJobInput(canonical_url="https://EXAMPLE.com/jobs/1?utm_source=x", title="X")
    second = Phase6CNormalizedJobInput(canonical_url="https://example.com/jobs/1", title="X")
    assert build_phase6c_identity(source_id="s", job=first).identity_hash == build_phase6c_identity(source_id="s", job=second).identity_hash


def test_fallback_identity_requires_sufficient_evidence() -> None:
    with pytest.raises(ValueError):
        Phase6CNormalizedJobInput(title="Engineer")
    fallback = Phase6CNormalizedJobInput(title="Engineer", company="Acme")
    assert build_phase6c_identity(source_id="s", job=fallback).strategy == "fingerprint"


def test_content_hash_ignores_relative_posted_date_and_whitespace_noise() -> None:
    first = _job(posted_date="3 days ago")
    second = _job(posted_date="4 days ago").model_copy(update={"title": "Senior Data Engineer"})
    assert build_phase6c_content_hash(first) == build_phase6c_content_hash(second)


def test_upsert_is_insert_then_unchanged_then_updated_without_duplicates() -> None:
    db, _, fleet, source_run, evidence = _context()
    repo = ProductionJobUpsertRepository(db)
    first = repo.upsert_job(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        raw_evidence_id=evidence.evidence_id,
        job=_job(),
    )
    second = repo.upsert_job(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        raw_evidence_id=evidence.evidence_id,
        job=_job(posted_date="4 days ago"),
    )
    third = repo.upsert_job(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        raw_evidence_id=evidence.evidence_id,
        job=_job(description="Build and operate production data pipelines"),
    )
    assert [first.outcome, second.outcome, third.outcome] == ["inserted", "unchanged", "updated"]
    assert len({first.job_id, second.job_id, third.job_id}) == 1
    assert db["jobs_current"].count_documents({}) == 1
    assert db["jobs_history"].count_documents({"job_id": first.job_id}) == 2
    stored = repo.get_job(first.job_id)
    assert stored is not None
    assert stored["version"] == 2
    assert stored["is_active"] is True
    assert stored["deactivated_at"] is None


def test_normalized_writes_are_rejected_when_phase6c_control_is_disabled() -> None:
    db, _, fleet, source_run, evidence = _context(normalized_writes=False)
    with pytest.raises(ProductionJobUpsertError, match="NORMALIZED_JOB_WRITES_DISABLED"):
        ProductionJobUpsertRepository(db).upsert_job(
            fleet_run_id=fleet.fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source_run.source_id,
            raw_evidence_id=evidence.evidence_id,
            job=_job(),
        )


def test_raw_evidence_must_belong_to_same_source_context() -> None:
    db, _, fleet, source_run, evidence = _context()
    db["production_raw_job_evidence"].documents[evidence.evidence_id]["source_id"] = "other"
    with pytest.raises(ProductionJobUpsertError, match="Raw evidence does not match source context"):
        ProductionJobUpsertRepository(db).upsert_job(
            fleet_run_id=fleet.fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source_run.source_id,
            raw_evidence_id=evidence.evidence_id,
            job=_job(),
        )


def test_inactive_job_is_reactivated_but_nothing_is_deactivated() -> None:
    db, _, fleet, source_run, evidence = _context()
    repo = ProductionJobUpsertRepository(db)
    first = repo.upsert_job(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        raw_evidence_id=evidence.evidence_id,
        job=_job(),
    )
    db["jobs_current"].documents[first.job_id].update({"is_active": False, "deactivated_at": datetime(2026, 7, 1, tzinfo=timezone.utc)})
    result = repo.upsert_job(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source_run.source_run_id,
        source_id=source_run.source_id,
        raw_evidence_id=evidence.evidence_id,
        job=_job(),
    )
    assert result.outcome == "reactivated"
    assert repo.get_job(first.job_id)["is_active"] is True
    assert repo.get_job(first.job_id)["deactivated_at"] is None


def test_source_and_fleet_counters_capture_upsert_outcomes() -> None:
    db, audit, fleet, source_run, evidence = _context()
    repo = ProductionJobUpsertRepository(db)
    repo.upsert_job(fleet_run_id=fleet.fleet_run_id, source_run_id=source_run.source_run_id, source_id=source_run.source_id, raw_evidence_id=evidence.evidence_id, job=_job())
    repo.upsert_job(fleet_run_id=fleet.fleet_run_id, source_run_id=source_run.source_run_id, source_id=source_run.source_id, raw_evidence_id=evidence.evidence_id, job=_job())
    repo.upsert_job(fleet_run_id=fleet.fleet_run_id, source_run_id=source_run.source_run_id, source_id=source_run.source_id, raw_evidence_id=evidence.evidence_id, job=_job(description="Changed"))
    source = audit.finalize_source_run(
        source_run_id=source_run.source_run_id,
        status="success",
        counters=Phase6BSourceCounters(discovered_count=1, attempted_count=3, extracted_count=3, valid_count=3),
    )
    finalized = audit.finalize_fleet_run(fleet_run_id=fleet.fleet_run_id)
    assert source.inserted_job_count == 1
    assert source.unchanged_job_count == 1
    assert source.updated_job_count == 1
    assert finalized.inserted_job_count == 1
    assert finalized.unchanged_job_count == 1
    assert finalized.updated_job_count == 1
    assert finalized.reactivated_job_count == 0


def test_phase6c_indexes_include_unique_identity_guards() -> None:
    job_indexes = INDEXES["jobs_current"]
    assert any(index.document.get("unique") and "identity_hash" in str(index.document) for index in job_indexes)
    assert any(index.document.get("unique") and "external_job_id" in str(index.document) for index in job_indexes)

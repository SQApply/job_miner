from __future__ import annotations

import copy
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.portals.production_ingestion import Phase6AIngestionPlan, Phase6ASource
from src.portals.production_persistence import (
    Phase6BRawEvidenceInput,
    Phase6BSourceCounters,
    ProductionIngestionPersistenceRepository,
    ProductionPersistenceError,
    read_phase6a_ingestion_plan,
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
        self.created_indexes: list[Any] = []

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
        if "$set" in update:
            payload.update(copy.deepcopy(update["$set"]))
        return _UpdateResult(matched_count=1)

    def find_one(self, query):
        for document in self.documents.values():
            if _matches(document, query):
                return copy.deepcopy(document)
        return None

    def find(self, query):
        return [
            copy.deepcopy(document)
            for document in self.documents.values()
            if _matches(document, query)
        ]

    def count_documents(self, query):
        return sum(1 for document in self.documents.values() if _matches(document, query))

    def delete_many(self, query):
        keys = [key for key, value in self.documents.items() if _matches(value, query)]
        for key in keys:
            self.documents.pop(key, None)
        return _DeleteResult(len(keys))

    def create_indexes(self, indexes):
        self.created_indexes.extend(indexes)
        return [str(index.document.get("name") or "index") for index in indexes]


class _FakeDatabase:
    def __init__(self) -> None:
        self.collections: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        return self.collections.setdefault(name, _FakeCollection())


def _source(source_id: str, row: int) -> Phase6ASource:
    return Phase6ASource(
        source_id=source_id,
        source_row=row,
        display_name=source_id,
        listing_url=f"https://{source_id}.example/jobs",
        detected_platform="custom_listing",
        bounded_extracted_jobs=10,
        evidence_run_id="cert-run",
    )


def _plan() -> Phase6AIngestionPlan:
    sources = [_source("source_a", 1), _source("source_b", 2)]
    return Phase6AIngestionPlan(
        plan_id="phase6a-test-plan",
        generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        cohort_sha256="a" * 64,
        cohort_source_count=2,
        selected_source_count=2,
        deferred_source_count=1,
        selected_source_ids=[source.source_id for source in sources],
        sources=sources,
        controls={
            "execution_mode": "plan_only",
            "max_source_concurrency": 2,
            "source_timeout_seconds": 600,
            "production_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
        },
    )


def _evidence() -> Phase6BRawEvidenceInput:
    return Phase6BRawEvidenceInput(
        source_url="https://source_a.example/jobs/123",
        canonical_url="https://source_a.example/jobs/123?utm_source=test",
        external_job_id="123",
        payload={"title": "Data Engineer", "description": "Build pipelines"},
        extractor_name="phase6b-test",
        extractor_version="1.0",
    )


def test_reads_only_safe_phase6a_plan() -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "plan.json"
        path.write_text(json.dumps(_plan().model_dump(mode="json")), encoding="utf-8")
        loaded = read_phase6a_ingestion_plan(path)
        assert loaded.selected_source_ids == ["source_a", "source_b"]

        payload = loaded.model_dump(mode="json")
        payload["controls"]["production_writes_enabled"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ProductionPersistenceError):
            read_phase6a_ingestion_plan(path)


def test_creates_subset_fleet_with_all_dangerous_controls_disabled() -> None:
    repo = ProductionIngestionPersistenceRepository(_FakeDatabase())
    fleet = repo.create_fleet_run(
        plan=_plan(),
        selected_source_ids=["source_b"],
        fleet_run_id="fleet_1",
        started_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    assert fleet.selected_source_ids == ["source_b"]
    assert fleet.requested_source_count == 1
    assert fleet.controls == {
        "persistence_writes_enabled": True,
        "normalized_job_writes_enabled": False,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
    }


def test_rejects_source_not_selected_for_fleet_run() -> None:
    repo = ProductionIngestionPersistenceRepository(_FakeDatabase())
    repo.create_fleet_run(plan=_plan(), selected_source_ids=["source_a"], fleet_run_id="fleet_1")
    with pytest.raises(ProductionPersistenceError, match="SOURCE_NOT_IN_FLEET_RUN"):
        repo.start_source_run(fleet_run_id="fleet_1", source=_source("source_b", 2))


def test_raw_evidence_is_immutable_and_idempotent() -> None:
    db = _FakeDatabase()
    repo = ProductionIngestionPersistenceRepository(db)
    plan = _plan()
    fleet = repo.create_fleet_run(plan=plan, selected_source_ids=["source_a"], fleet_run_id="fleet_1")
    source = repo.start_source_run(fleet_run_id=fleet.fleet_run_id, source=plan.sources[0])

    first, first_inserted = repo.store_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source.source_run_id,
        source_id=source.source_id,
        evidence=_evidence(),
    )
    second, second_inserted = repo.store_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source.source_run_id,
        source_id=source.source_id,
        evidence=_evidence(),
    )
    assert first.evidence_id == second.evidence_id
    assert first.payload_sha256 == second.payload_sha256
    assert first_inserted is True
    assert second_inserted is False
    assert db["production_raw_job_evidence"].count_documents({}) == 1


def test_finalization_accepts_timezone_naive_utc_values_returned_by_pymongo() -> None:
    db = _FakeDatabase()
    repo = ProductionIngestionPersistenceRepository(db)
    plan = _plan()
    started = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    fleet = repo.create_fleet_run(
        plan=plan,
        selected_source_ids=["source_a"],
        fleet_run_id="fleet_naive_mongo",
        started_at=started,
    )
    source = repo.start_source_run(
        fleet_run_id=fleet.fleet_run_id,
        source=plan.sources[0],
        started_at=started,
    )

    # PyMongo's default BSON decoder returns UTC datetimes without tzinfo.
    db["production_ingestion_fleet_runs"].documents[fleet.fleet_run_id][
        "started_at"
    ] = started.replace(tzinfo=None)
    db["production_ingestion_source_runs"].documents[source.source_run_id][
        "started_at"
    ] = started.replace(tzinfo=None)

    completed = started + timedelta(seconds=3)
    finalized_source = repo.finalize_source_run(
        source_run_id=source.source_run_id,
        status="success",
        counters=Phase6BSourceCounters(),
        completed_at=completed,
    )
    finalized_fleet = repo.finalize_fleet_run(
        fleet_run_id=fleet.fleet_run_id,
        completed_at=completed,
    )

    assert finalized_source.elapsed_seconds == 3.0
    assert finalized_source.status == "success"
    assert finalized_fleet.status == "completed"


def test_finalizes_source_and_fleet_from_persisted_records() -> None:
    db = _FakeDatabase()
    repo = ProductionIngestionPersistenceRepository(db)
    plan = _plan()
    started = datetime(2026, 7, 17, tzinfo=timezone.utc)
    fleet = repo.create_fleet_run(
        plan=plan,
        selected_source_ids=["source_a"],
        fleet_run_id="fleet_1",
        started_at=started,
    )
    source = repo.start_source_run(
        fleet_run_id=fleet.fleet_run_id,
        source=plan.sources[0],
        started_at=started,
    )
    repo.store_raw_evidence(
        fleet_run_id=fleet.fleet_run_id,
        source_run_id=source.source_run_id,
        source_id=source.source_id,
        evidence=_evidence(),
    )
    completed = started + timedelta(seconds=2)
    source = repo.finalize_source_run(
        source_run_id=source.source_run_id,
        status="success",
        counters=Phase6BSourceCounters(
            discovered_count=1,
            attempted_count=1,
            extracted_count=1,
        ),
        completed_at=completed,
    )
    fleet = repo.finalize_fleet_run(fleet_run_id=fleet.fleet_run_id, completed_at=completed)
    assert source.status == "success"
    assert source.raw_evidence_count == 1
    assert fleet.status == "completed"
    assert fleet.completed_source_count == 1
    assert fleet.successful_source_count == 1
    assert fleet.raw_evidence_count == 1
    assert fleet.inserted_job_count == 0


def test_mixed_source_outcomes_finalize_as_completed_with_failures() -> None:
    repo = ProductionIngestionPersistenceRepository(_FakeDatabase())
    plan = _plan()
    fleet = repo.create_fleet_run(plan=plan, fleet_run_id="fleet_1")
    first = repo.start_source_run(fleet_run_id=fleet.fleet_run_id, source=plan.sources[0])
    second = repo.start_source_run(fleet_run_id=fleet.fleet_run_id, source=plan.sources[1])
    repo.finalize_source_run(
        source_run_id=first.source_run_id,
        status="success",
        counters=Phase6BSourceCounters(),
    )
    repo.finalize_source_run(
        source_run_id=second.source_run_id,
        status="failed",
        counters=Phase6BSourceCounters(),
        error_type="SyntheticFailure",
        error_message="expected",
    )
    fleet = repo.finalize_fleet_run(fleet_run_id=fleet.fleet_run_id)
    assert fleet.status == "completed_with_failures"
    assert fleet.successful_source_count == 1
    assert fleet.failed_source_count == 1
    assert fleet.error_summary[0]["source_id"] == "source_b"


def test_fleet_cannot_finalize_until_every_selected_source_is_terminal() -> None:
    repo = ProductionIngestionPersistenceRepository(_FakeDatabase())
    plan = _plan()
    fleet = repo.create_fleet_run(plan=plan, fleet_run_id="fleet_1")
    first = repo.start_source_run(fleet_run_id=fleet.fleet_run_id, source=plan.sources[0])
    repo.finalize_source_run(
        source_run_id=first.source_run_id,
        status="success",
        counters=Phase6BSourceCounters(),
    )
    with pytest.raises(ProductionPersistenceError, match="source runs are missing"):
        repo.finalize_fleet_run(fleet_run_id=fleet.fleet_run_id)


def test_raw_evidence_cannot_be_added_after_source_is_terminal() -> None:
    repo = ProductionIngestionPersistenceRepository(_FakeDatabase())
    plan = _plan()
    fleet = repo.create_fleet_run(plan=plan, selected_source_ids=["source_a"], fleet_run_id="fleet_1")
    source = repo.start_source_run(fleet_run_id=fleet.fleet_run_id, source=plan.sources[0])
    repo.finalize_source_run(
        source_run_id=source.source_run_id,
        status="success",
        counters=Phase6BSourceCounters(),
    )
    with pytest.raises(ProductionPersistenceError, match="running source run"):
        repo.store_raw_evidence(
            fleet_run_id=fleet.fleet_run_id,
            source_run_id=source.source_run_id,
            source_id=source.source_id,
            evidence=_evidence(),
        )


def test_phase6b_indexes_include_unique_run_and_evidence_constraints() -> None:
    fleet_indexes = INDEXES["production_ingestion_fleet_runs"]
    source_indexes = INDEXES["production_ingestion_source_runs"]
    evidence_indexes = INDEXES["production_raw_job_evidence"]
    assert any(index.document.get("unique") for index in fleet_indexes)
    assert sum(bool(index.document.get("unique")) for index in source_indexes) >= 2
    assert sum(bool(index.document.get("unique")) for index in evidence_indexes) >= 2

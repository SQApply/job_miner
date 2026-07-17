from __future__ import annotations

import asyncio
import copy
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.portals.certification import PortalCertificationRecord
from src.portals.production_ingestion import Phase6AIngestionPlan, Phase6ASource
from src.portals.production_runner import (
    Phase6EProductionRunner,
    Phase6ERunnerConfig,
    ProductionRunnerError,
    select_phase6e_sources,
    write_phase6e_manifest,
)


class _UpdateResult:
    def __init__(self, *, upserted_id: str | None = None, matched_count: int = 0):
        self.upserted_id = upserted_id
        self.matched_count = matched_count
        self.modified_count = matched_count


class _DeleteResult:
    def __init__(self, deleted_count: int):
        self.deleted_count = deleted_count


def _lookup(document: dict[str, Any], key: str) -> Any:
    value: Any = document
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        actual = _lookup(document, key)
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
            for field, amount in (update.get("$inc") or {}).items():
                payload[field] = int(payload.get(field) or 0) + int(amount)
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
        self.created_indexes.extend(indexes)
        return [str(index.document) for index in indexes]


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


def _record(
    source: Phase6ASource,
    *,
    attempt: int,
    status: str = "success",
    error_type: str | None = None,
    error_message: str | None = None,
    valid_job: bool = True,
) -> PortalCertificationRecord:
    jobs: list[dict[str, Any]] = []
    if status == "success":
        jobs = [
            {
                "title": "Data Engineer" if valid_job else "Access Denied",
                "job_url": f"https://{source.source_id}.example/jobs/123",
                "company": "Acme",
                "location_text": "Remote",
                "summary": (
                    "Design, build, test, operate, and monitor reliable production data pipelines using Python and MongoDB."
                    if valid_job
                    else "Access denied. Verify you are human."
                ),
                "job_reference": "REQ-123",
            }
        ]
    now = datetime.now(timezone.utc).isoformat()
    return PortalCertificationRecord(
        contract_version="1.3",
        run_id="cert-run",
        attempt_number=attempt,
        source_id=source.source_id,
        display_name=source.display_name,
        provided_url=source.listing_url,
        effective_listing_url=source.listing_url,
        status=status,
        certification_status="passed" if status == "success" else "needs_repair",
        stage="complete",
        detected_platform="custom_listing",
        detected_profile=None,
        discovered_urls=len(jobs),
        attempted_urls=len(jobs),
        extracted_jobs=len(jobs),
        sample_jobs=jobs,
        acquisition={"strategy": "test", "trusted_hosts": [f"{source.source_id}.example"]},
        detail_failures=[],
        rejected_urls=0,
        event_counts={},
        gpu_before={},
        gpu_after={},
        started_at=now,
        completed_at=now,
        elapsed_seconds=0.01,
        error_type=error_type,
        error_message=error_message,
    )


class _SequenceExecutor:
    def __init__(self, responses: dict[str, list[Any]]):
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls: dict[str, int] = {}

    async def execute(self, source, *, attempt_number: int, run_id: str):
        del run_id
        self.calls[source.source_id] = self.calls.get(source.source_id, 0) + 1
        value = self.responses[source.source_id].pop(0)
        if isinstance(value, BaseException):
            raise value
        return value(source, attempt_number) if callable(value) else value


def _config(*, write: bool = False, max_attempts: int = 2) -> Phase6ERunnerConfig:
    return Phase6ERunnerConfig(
        execution_mode="write" if write else "dry_run",
        normalized_job_writes_enabled=write,
        max_source_concurrency=2,
        max_attempts=max_attempts,
        retry_backoff_seconds=0,
        max_jobs=1,
    )


def test_phase6e_controls_forbid_reconciliation_and_unsafe_write_modes() -> None:
    with pytest.raises(ValueError):
        Phase6ERunnerConfig(execution_mode="write", normalized_job_writes_enabled=False)
    with pytest.raises(ValueError):
        Phase6ERunnerConfig(execution_mode="dry_run", normalized_job_writes_enabled=True)
    with pytest.raises(ValueError):
        Phase6ERunnerConfig(lifecycle_reconciliation_enabled=True)
    with pytest.raises(ValueError):
        Phase6ERunnerConfig(deactivation_enabled=True)


def test_source_selection_rejects_deferred_and_preserves_plan_order() -> None:
    plan = _plan()
    selected = select_phase6e_sources(plan, ["source_b", "source_a"])
    assert [source.source_id for source in selected] == ["source_b", "source_a"]
    with pytest.raises(ProductionRunnerError, match="SOURCE_NOT_IN_PRODUCTION_COHORT"):
        select_phase6e_sources(plan, ["source_c"])


def test_dry_run_retries_transient_failure_and_performs_no_writes() -> None:
    plan = _plan()
    executor = _SequenceExecutor(
        {
            "source_a": [
                lambda source, attempt: _record(source, attempt=attempt, status="failed", error_type="network_error", error_message="timeout"),
                lambda source, attempt: _record(source, attempt=attempt),
            ],
        }
    )
    db = _FakeDatabase()
    manifest = asyncio.run(
        Phase6EProductionRunner(plan=plan, db=db, executor=executor, config=_config()).run(
            requested_source_ids=["source_a"]
        )
    )
    assert manifest.successful_source_count == 1
    assert manifest.inserted_job_count == 1
    assert executor.calls["source_a"] == 2
    assert db["production_ingestion_fleet_runs"].count_documents({}) == 0
    assert db["jobs_current"].count_documents({}) == 0


def test_permanent_failure_is_not_retried() -> None:
    plan = _plan()
    executor = _SequenceExecutor(
        {
            "source_a": [
                lambda source, attempt: _record(source, attempt=attempt, status="failed", error_type="zero_discovery", error_message="none"),
            ],
        }
    )
    manifest = asyncio.run(
        Phase6EProductionRunner(plan=plan, db=_FakeDatabase(), executor=executor, config=_config()).run(
            requested_source_ids=["source_a"]
        )
    )
    assert manifest.failed_source_count == 1
    assert executor.calls["source_a"] == 1


def test_source_failure_is_isolated_from_successful_source() -> None:
    plan = _plan()
    executor = _SequenceExecutor(
        {
            "source_a": [lambda source, attempt: _record(source, attempt=attempt)],
            "source_b": [lambda source, attempt: _record(source, attempt=attempt, status="failed", error_type="zero_discovery")],
        }
    )
    manifest = asyncio.run(
        Phase6EProductionRunner(plan=plan, db=_FakeDatabase(), executor=executor, config=_config(max_attempts=1)).run()
    )
    assert manifest.completed_source_count == 2
    assert manifest.successful_source_count == 1
    assert manifest.failed_source_count == 1
    assert manifest.accepted_job_count == 1


def test_write_mode_persists_job_and_finalizes_mixed_fleet() -> None:
    plan = _plan()
    db = _FakeDatabase()
    executor = _SequenceExecutor(
        {
            "source_a": [lambda source, attempt: _record(source, attempt=attempt)],
            "source_b": [lambda source, attempt: _record(source, attempt=attempt, status="failed", error_type="zero_discovery")],
        }
    )
    manifest = asyncio.run(
        Phase6EProductionRunner(plan=plan, db=db, executor=executor, config=_config(write=True, max_attempts=1)).run(
            run_id="phase6e_test_write"
        )
    )
    assert manifest.inserted_job_count == 1
    assert db["jobs_current"].count_documents({}) == 1
    fleet = db["production_ingestion_fleet_runs"].find_one({"fleet_run_id": "phase6e_test_write"})
    assert fleet["status"] == "completed_with_failures"
    assert fleet["successful_source_count"] == 1
    assert fleet["failed_source_count"] == 1
    assert fleet["controls"]["lifecycle_reconciliation_enabled"] is False
    assert fleet["controls"]["deactivation_enabled"] is False


def test_successful_scrape_with_all_jobs_quarantined_is_failed() -> None:
    plan = _plan()
    executor = _SequenceExecutor(
        {"source_a": [lambda source, attempt: _record(source, attempt=attempt, valid_job=False)]}
    )
    manifest = asyncio.run(
        Phase6EProductionRunner(plan=plan, db=_FakeDatabase(), executor=executor, config=_config()).run(
            requested_source_ids=["source_a"]
        )
    )
    result = manifest.source_results[0]
    assert result.status == "failed"
    assert result.quarantined_count == 1
    assert result.error_type == "quality_gate_rejected_all"


def test_manifest_write_is_atomic_and_round_trips() -> None:
    plan = _plan()
    executor = _SequenceExecutor(
        {"source_a": [lambda source, attempt: _record(source, attempt=attempt)]}
    )
    manifest = asyncio.run(
        Phase6EProductionRunner(plan=plan, db=_FakeDatabase(), executor=executor, config=_config()).run(
            requested_source_ids=["source_a"], run_id="phase6e_manifest"
        )
    )
    with tempfile.TemporaryDirectory() as raw:
        path = write_phase6e_manifest(Path(raw) / "manifest.json", manifest)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["run_id"] == "phase6e_manifest"
        assert payload["controls"]["lifecycle_reconciliation_enabled"] is False
        assert not path.with_suffix(".json.tmp").exists()

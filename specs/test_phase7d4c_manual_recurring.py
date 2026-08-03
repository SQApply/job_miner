from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.portals.certification import PortalInventoryEntry
from src.portals.production_manual_recurring import (
    PHASE_7D4C_MANUAL_WRITE_CONFIRMATION,
    ProductionManualRecurringError,
    Phase7D4CManualRecurringRunner,
    build_phase7d4c_manual_plan,
    build_phase7d4c_runner_config,
    load_phase7d4c_manual_policy,
    process_changed_only_downstream,
    require_phase7d4c_manual_confirmation,
)
from src.portals.production_ingestion import Phase6AIngestionPlan, Phase6ASource
from src.portals.production_runner import Phase6ESourceResult
from src.warehouse.repositories import WarehouseRepository


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    if "$or" in query and not any(_matches(document, item) for item in query["$or"]):
        return False
    for key, expected in query.items():
        if key.startswith("$"):
            continue
        actual = document.get(key)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$gte" in expected and not (
                actual is not None and actual >= expected["$gte"]
            ):
                return False
            if "$lte" in expected and not (
                actual is not None and actual <= expected["$lte"]
            ):
                return False
            if "$exists" in expected and (key in document) is not expected["$exists"]:
                return False
        elif actual != expected:
            return False
    return True


class _Collection:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = copy.deepcopy(rows or [])

    def find(
        self,
        query: dict[str, Any] | None = None,
        projection: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        rows = [copy.deepcopy(row) for row in self.rows if _matches(row, query or {})]
        if not projection:
            return rows
        included = {key for key, enabled in projection.items() if enabled}
        return [
            {key: value for key, value in row.items() if key in included}
            for row in rows
        ]

    def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for row in self.rows if _matches(row, query))

    def create_indexes(self, indexes: list[Any]) -> list[str]:
        return [f"index-{index}" for index, _ in enumerate(indexes)]

    def find_one(
        self,
        query: dict[str, Any],
        projection: dict[str, Any] | None = None,
        sort: list[tuple[str, int]] | None = None,
    ) -> dict[str, Any] | None:
        rows = self.find(query, projection)
        if sort:
            for field, direction in reversed(sort):
                rows.sort(
                    key=lambda row: row.get(field),
                    reverse=direction < 0,
                )
        return rows[0] if rows else None

    @staticmethod
    def _apply_update(
        row: dict[str, Any],
        update: dict[str, Any],
        *,
        inserted: bool,
    ) -> None:
        if inserted:
            row.update(copy.deepcopy(update.get("$setOnInsert") or {}))
        for key, value in (update.get("$inc") or {}).items():
            row[key] = int(row.get(key) or 0) + int(value)
        row.update(copy.deepcopy(update.get("$set") or {}))
        for key in (update.get("$unset") or {}):
            row.pop(key, None)

    def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> SimpleNamespace:
        for row in self.rows:
            if _matches(row, query):
                before = copy.deepcopy(row)
                self._apply_update(row, update, inserted=False)
                return SimpleNamespace(
                    matched_count=1,
                    modified_count=int(row != before),
                    upserted_id=None,
                )
        if not upsert:
            return SimpleNamespace(
                matched_count=0,
                modified_count=0,
                upserted_id=None,
            )
        row = {
            key: copy.deepcopy(value)
            for key, value in query.items()
            if not key.startswith("$") and not isinstance(value, dict)
        }
        self._apply_update(row, update, inserted=True)
        self.rows.append(row)
        return SimpleNamespace(
            matched_count=0,
            modified_count=0,
            upserted_id=row.get("_id"),
        )

    def find_one_and_update(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        *,
        upsert: bool,
        return_document: Any,
    ) -> dict[str, Any] | None:
        del return_document
        for row in self.rows:
            if _matches(row, query):
                self._apply_update(row, update, inserted=False)
                return copy.deepcopy(row)
        if not upsert:
            return None
        row = {"_id": query.get("_id")}
        self._apply_update(row, update, inserted=True)
        self.rows.append(row)
        return copy.deepcopy(row)

    def insert_one(self, document: dict[str, Any]) -> SimpleNamespace:
        self.rows.append(copy.deepcopy(document))
        return SimpleNamespace(inserted_id=document.get("_id"))

    def update_many(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
    ) -> SimpleNamespace:
        matched = modified = 0
        for row in self.rows:
            if not _matches(row, query):
                continue
            matched += 1
            before = copy.deepcopy(row)
            self._apply_update(row, update, inserted=False)
            modified += int(row != before)
        return SimpleNamespace(matched_count=matched, modified_count=modified)

    def delete_many(self, query: dict[str, Any]) -> SimpleNamespace:
        before = len(self.rows)
        self.rows = [row for row in self.rows if not _matches(row, query)]
        return SimpleNamespace(deleted_count=before - len(self.rows))


class _Database:
    def __init__(self, rows: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.collections = {
            name: _Collection(values) for name, values in (rows or {}).items()
        }

    def __getitem__(self, name: str) -> _Collection:
        return self.collections.setdefault(name, _Collection())


def _policy() -> tuple[Path, dict[str, Any]]:
    root = Path(__file__).resolve().parents[1]
    path = (
        root
        / "configs"
        / "portal_cohorts"
        / "phase7d4c_manual_recurring_policy.json"
    )
    return root, load_phase7d4c_manual_policy(root=root, policy_path=path)


def test_repository_policy_is_manual_and_exactly_22_sources() -> None:
    _, policy = _policy()
    assert len(policy["source_ids"]) == 22
    assert len(set(policy["source_ids"])) == 22
    assert policy["cadence_hours"] == 72
    assert policy["execution"]["automatic_scheduler_enabled"] is False
    assert policy["execution"]["manual_command_required"] is True
    assert policy["execution"]["incremental_detail_rescrape"] is True
    assert policy["execution"]["deep_refresh_days"] == 14
    assert policy["lifecycle"]["deactivate_after_complete_misses"] == 2
    assert policy["lifecycle"]["failed_or_partial_runs_increment_missing_count"] is False
    assert policy["downstream"]["changed_only"] is True


def test_manual_write_confirmation_is_exact() -> None:
    with pytest.raises(ProductionManualRecurringError, match="confirm-production-writes"):
        require_phase7d4c_manual_confirmation("")
    require_phase7d4c_manual_confirmation(
        PHASE_7D4C_MANUAL_WRITE_CONFIRMATION
    )


def test_inventory_builds_exact_policy_scope_without_yaml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root, policy = _policy()
    entries = [
        PortalInventoryEntry(
            source_id=source_id,
            display_name=source_id,
            listing_url=f"https://example.com/{index}",
            source_row=index,
        )
        for index, source_id in enumerate(policy["source_ids"], start=1)
    ]
    monkeypatch.setattr(
        "src.portals.production_manual_recurring.read_portal_inventory",
        lambda _: entries,
    )
    plan = build_phase7d4c_manual_plan(
        root=root,
        policy=policy,
        inventory_path=tmp_path / "inventory.xlsx",
        requested_source_ids=[policy["source_ids"][1], policy["source_ids"][3]],
    )
    assert plan.cohort_source_count == 22
    assert plan.selected_source_count == 2
    assert plan.selected_source_ids == [
        policy["source_ids"][1],
        policy["source_ids"][3],
    ]
    assert plan.controls["automatic_scheduler_enabled"] is False


def test_complete_catalog_runner_is_gpu_serial_and_unbounded() -> None:
    _, policy = _policy()
    config = build_phase7d4c_runner_config(policy)
    assert config.catalog_mode == "complete_catalog"
    assert config.max_jobs is None
    assert config.max_source_concurrency == 1
    assert config.detail_concurrency == 1
    assert config.incremental_rescrape is True
    assert config.normalized_job_writes_enabled is True
    assert config.lifecycle_reconciliation_enabled is False
    assert config.deactivation_enabled is False


def test_two_complete_misses_deactivate_and_reappearance_reactivates() -> None:
    source_id = "source-a"
    database = _Database(
        {
            "jobs_current": [
                {
                    "_id": "job-1",
                    "job_id": "job-1",
                    "target_id": source_id,
                    "canonical_job_url": "https://example.com/jobs/1",
                    "is_active": True,
                    "missing_count": 0,
                    "missing_complete_run_count": 0,
                },
                {
                    "_id": "job-2",
                    "job_id": "job-2",
                    "target_id": source_id,
                    "canonical_job_url": "https://example.com/jobs/2",
                    "is_active": True,
                    "missing_count": 0,
                    "missing_complete_run_count": 0,
                },
            ]
        }
    )
    warehouse = WarehouseRepository(database)  # type: ignore[arg-type]

    first = warehouse.reconcile_missing_jobs_after_discovery(
        target_id=source_id,
        run_session_id="complete-run-1",
        discovered_urls=["https://example.com/jobs/1"],
        deactivate_after_misses=2,
        min_discovery_coverage_ratio=0.25,
    )
    missing = database["jobs_current"].rows[1]
    assert first["deactivated"] == 0
    assert first["missing_job_ids"] == ["job-2"]
    assert missing["missing_complete_run_count"] == 1
    assert missing["is_active"] is True
    first_missing_since = missing["missing_since"]

    second = warehouse.reconcile_missing_jobs_after_discovery(
        target_id=source_id,
        run_session_id="complete-run-2",
        discovered_urls=["https://example.com/jobs/1"],
        deactivate_after_misses=2,
        min_discovery_coverage_ratio=0.25,
    )
    missing = database["jobs_current"].rows[1]
    assert second["deactivated"] == 1
    assert second["deactivated_job_ids"] == ["job-2"]
    assert missing["is_active"] is False
    assert missing["missing_since"] == first_missing_since
    assert missing["deactivation_run_id"] == "complete-run-2"

    third = warehouse.reconcile_missing_jobs_after_discovery(
        target_id=source_id,
        run_session_id="complete-run-3",
        discovered_urls=[
            "https://example.com/jobs/1",
            "https://example.com/jobs/2",
        ],
        deactivate_after_misses=2,
        min_discovery_coverage_ratio=0.25,
    )
    reappeared = database["jobs_current"].rows[1]
    assert third["reactivated_job_ids"] == ["job-2"]
    assert reappeared["is_active"] is True
    assert reappeared["missing_complete_run_count"] == 0
    assert "missing_since" not in reappeared
    assert reappeared.get("reactivated_at") is not None


def test_empty_or_low_coverage_discovery_never_increments_missing() -> None:
    source_id = "source-a"
    jobs = [
        {
            "job_id": f"job-{index}",
            "target_id": source_id,
            "canonical_job_url": f"https://example.com/jobs/{index}",
            "is_active": True,
            "missing_count": 0,
            "missing_complete_run_count": 0,
        }
        for index in range(10)
    ]
    database = _Database({"jobs_current": jobs})
    warehouse = WarehouseRepository(database)  # type: ignore[arg-type]
    empty = warehouse.reconcile_missing_jobs_after_discovery(
        target_id=source_id,
        run_session_id="empty",
        discovered_urls=[],
        min_discovery_coverage_ratio=0.25,
    )
    low = warehouse.reconcile_missing_jobs_after_discovery(
        target_id=source_id,
        run_session_id="low",
        discovered_urls=["https://example.com/jobs/0"],
        min_discovery_coverage_ratio=0.25,
    )
    assert empty["status"] == "skipped_empty_discovery"
    assert low["status"] == "skipped_low_coverage"
    assert all(
        row["missing_complete_run_count"] == 0
        for row in database["jobs_current"].rows
    )


def test_changed_only_downstream_skips_all_external_services_when_unchanged() -> None:
    result = process_changed_only_downstream(
        _Database(),  # type: ignore[arg-type]
        source_id="source-a",
        run_session_id="run-a",
        changed_job_ids=[],
        deactivated_job_ids=[],
    )
    assert result["status"] == "skipped_no_changes"
    assert result["qdrant_indexed"] == 0
    assert result["qdrant_deleted"] == 0


def test_changed_only_downstream_removes_newly_inactive_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _Database(
        {
            "qdrant_index_state": [
                {
                    "record_type": "job",
                    "record_id": "job-2",
                    "qdrant_point_id": "point-2",
                    "collection_name_value": "jobs",
                }
            ],
            "job_tower_records": [{"job_id": "job-2"}],
            "candidate_job_matches": [{"job_id": "job-2"}],
            "candidate_job_matches_llm_reranked": [{"job_id": "job-2"}],
        }
    )

    class _Store:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, list[str]]] = []

        def healthcheck(self) -> dict[str, Any]:
            return {"ok": True}

        def delete_points(
            self,
            collection_name: str,
            *,
            point_ids: list[str],
        ) -> int:
            self.deleted.append((collection_name, list(point_ids)))
            return len(point_ids)

    store = _Store()
    monkeypatch.setattr(
        "src.portals.production_manual_recurring.load_app_settings",
        lambda: SimpleNamespace(
            vector=SimpleNamespace(jobs_collection="jobs")
        ),
    )
    monkeypatch.setattr(
        "src.portals.production_manual_recurring.build_vector_store",
        lambda _: store,
    )
    monkeypatch.setattr(
        WarehouseRepository,
        "record_recommendation_refresh_requests",
        lambda *args, **kwargs: {"status": "recorded"},
    )

    result = process_changed_only_downstream(
        database,  # type: ignore[arg-type]
        source_id="source-a",
        run_session_id="run-a",
        changed_job_ids=[],
        deactivated_job_ids=["job-2"],
    )
    assert result["status"] == "completed"
    assert result["qdrant_deleted"] == 1
    assert store.deleted == [("jobs", ["point-2"])]
    assert database["qdrant_index_state"].rows == []
    assert database["job_tower_records"].rows == []
    assert database["candidate_job_matches"].rows == []
    assert database["candidate_job_matches_llm_reranked"].rows == []


def _one_source_plan(source_id: str) -> Phase6AIngestionPlan:
    source = Phase6ASource(
        source_id=source_id,
        source_row=1,
        display_name="Example",
        listing_url="https://example.com/jobs",
    )
    return Phase6AIngestionPlan(
        plan_id="manual-test-plan",
        generated_at=datetime.now(timezone.utc),
        cohort_sha256="a" * 64,
        cohort_source_count=22,
        selected_source_count=1,
        deferred_source_count=80,
        selected_source_ids=[source_id],
        sources=[source],
        controls={
            "execution_mode": "plan_only",
            "max_source_concurrency": 1,
            "source_timeout_seconds": 1800,
            "production_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
        },
    )


def test_manual_cycle_checkpoints_complete_snapshot_and_reconciliation(
    tmp_path: Path,
) -> None:
    root, policy = _policy()
    source_id = policy["source_ids"][0]
    job_url = "https://example.com/jobs/1"
    database = _Database(
        {
            "jobs_current": [
                {
                    "job_id": "job-1",
                    "target_id": source_id,
                    "canonical_job_url": job_url,
                    "is_active": True,
                    "missing_count": 0,
                    "missing_complete_run_count": 0,
                }
            ]
        }
    )

    async def source_run(
        source: Phase6ASource,
        run_id: str,
    ) -> Phase6ESourceResult:
        del run_id
        return Phase6ESourceResult(
            source_id=source.source_id,
            display_name=source.display_name,
            status="success",
            discovered_count=1,
            catalog_mode="complete_catalog",
            discovery_complete=True,
            catalog_complete=False,
            discovered_job_urls=[job_url],
            reconciliation_safe=True,
        )

    downstream_calls: list[dict[str, Any]] = []

    def downstream(database_value: Any, **kwargs: Any) -> dict[str, Any]:
        del database_value
        downstream_calls.append(dict(kwargs))
        return {"status": "skipped_no_changes", "qdrant_indexed": 0}

    report = asyncio.run(
        Phase7D4CManualRecurringRunner(
            root=root,
            output_dir=tmp_path,
            database=database,  # type: ignore[arg-type]
            plan=_one_source_plan(source_id),
            policy=policy,
            source_run_callable=source_run,
            downstream_callable=downstream,
        ).run()
    )
    assert report["status"] == "completed"
    assert report["status_counts"] == {"complete": 1}
    assert report["counters"]["missing_marked"] == 0
    assert len(database["production_recurring_source_snapshots"].rows) == 1
    assert database["production_recurring_source_snapshots"].rows[0][
        "reconciliation_safe"
    ] is True
    assert len(downstream_calls) == 1


def test_partial_cycle_never_increments_missing_state(tmp_path: Path) -> None:
    root, policy = _policy()
    source_id = policy["source_ids"][0]
    database = _Database(
        {
            "jobs_current": [
                {
                    "job_id": "job-1",
                    "target_id": source_id,
                    "canonical_job_url": "https://example.com/jobs/1",
                    "is_active": True,
                    "missing_count": 0,
                    "missing_complete_run_count": 0,
                }
            ]
        }
    )

    async def partial_source(
        source: Phase6ASource,
        run_id: str,
    ) -> Phase6ESourceResult:
        del run_id
        return Phase6ESourceResult(
            source_id=source.source_id,
            display_name=source.display_name,
            status="failed",
            accepted_count=1,
            unchanged_count=1,
            discovered_count=1,
            catalog_mode="complete_catalog",
            discovery_complete=False,
            catalog_complete=False,
            discovered_job_urls=[],
            reconciliation_safe=False,
            error_type="catalog_incomplete",
        )

    report = asyncio.run(
        Phase7D4CManualRecurringRunner(
            root=root,
            output_dir=tmp_path,
            database=database,  # type: ignore[arg-type]
            plan=_one_source_plan(source_id),
            policy=policy,
            source_run_callable=partial_source,
            downstream_callable=lambda *args, **kwargs: {
                "status": "skipped_no_changes"
            },
        ).run()
    )
    current = database["jobs_current"].rows[0]
    assert report["status"] == "completed_with_partial"
    assert report["status_counts"] == {"partial": 1}
    assert current["missing_complete_run_count"] == 0
    assert current["is_active"] is True


def test_resume_retries_downstream_without_repeating_scrape(tmp_path: Path) -> None:
    root, policy = _policy()
    source_id = policy["source_ids"][0]
    job_url = "https://example.com/jobs/1"
    database = _Database(
        {
            "jobs_current": [
                {
                    "job_id": "job-1",
                    "target_id": source_id,
                    "canonical_job_url": job_url,
                    "is_active": True,
                    "missing_count": 0,
                    "missing_complete_run_count": 0,
                }
            ]
        }
    )
    source_calls = 0

    async def source_run(
        source: Phase6ASource,
        run_id: str,
    ) -> Phase6ESourceResult:
        nonlocal source_calls
        del run_id
        source_calls += 1
        return Phase6ESourceResult(
            source_id=source.source_id,
            display_name=source.display_name,
            status="success",
            discovered_count=1,
            catalog_mode="complete_catalog",
            discovery_complete=True,
            discovered_job_urls=[job_url],
            reconciliation_safe=True,
            changed_job_ids=["job-1"],
        )

    def failing_downstream(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("qdrant unavailable")

    first = asyncio.run(
        Phase7D4CManualRecurringRunner(
            root=root,
            output_dir=tmp_path,
            database=database,  # type: ignore[arg-type]
            plan=_one_source_plan(source_id),
            policy=policy,
            source_run_callable=source_run,
            downstream_callable=failing_downstream,
        ).run()
    )
    assert first["status"] == "completed_with_failures"
    assert first["status_counts"] == {"failed_downstream": 1}

    downstream_calls = 0

    def recovered_downstream(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal downstream_calls
        downstream_calls += 1
        return {"status": "completed", "qdrant_indexed": 1}

    resumed = asyncio.run(
        Phase7D4CManualRecurringRunner(
            root=root,
            output_dir=tmp_path,
            database=database,  # type: ignore[arg-type]
            plan=_one_source_plan(source_id),
            policy=policy,
            source_run_callable=source_run,
            downstream_callable=recovered_downstream,
        ).run(resume_cycle_id=first["cycle_id"])
    )
    assert resumed["status"] == "completed"
    assert resumed["status_counts"] == {"complete": 1}
    assert source_calls == 1
    assert downstream_calls == 1

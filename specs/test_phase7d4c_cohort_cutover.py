from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.portals.production_cohort_cutover import (
    CUTOVER_CONFIRMATION,
    CohortCutoverError,
    build_cutover_plan,
    classify_job_documents,
    execute_cutover,
    load_cutover_policy,
    portal_identity,
    verify_cutover,
)


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    if not query:
        return True
    if "$and" in query:
        return all(_matches(document, item) for item in query["$and"])
    for field, condition in query.items():
        actual = document.get(field)
        if isinstance(condition, dict):
            if "$in" in condition and actual not in condition["$in"]:
                return False
            if "$nin" in condition and actual in condition["$nin"]:
                return False
            if "$ne" in condition and actual == condition["$ne"]:
                return False
        elif actual != condition:
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
        found = [
            copy.deepcopy(row)
            for row in self.rows
            if _matches(row, query or {})
        ]
        if not projection:
            return found
        included = {key for key, value in projection.items() if value}
        return [
            {key: value for key, value in row.items() if key in included}
            for row in found
        ]

    def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for row in self.rows if _matches(row, query))

    def update_many(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
    ) -> SimpleNamespace:
        modified = 0
        for row in self.rows:
            if not _matches(row, query):
                continue
            before = copy.deepcopy(row)
            row.update(copy.deepcopy(update.get("$set") or {}))
            if row != before:
                modified += 1
        return SimpleNamespace(modified_count=modified)

    def delete_many(self, query: dict[str, Any]) -> SimpleNamespace:
        before = len(self.rows)
        self.rows = [
            row for row in self.rows if not _matches(row, query)
        ]
        return SimpleNamespace(deleted_count=before - len(self.rows))


class _Database:
    name = "job_miner_test"

    def __init__(self, collections: dict[str, list[dict[str, Any]]]) -> None:
        self.collections = {
            name: _Collection(rows)
            for name, rows in collections.items()
        }

    def __getitem__(self, name: str) -> _Collection:
        return self.collections.setdefault(name, _Collection())


def test_target_id_is_authoritative_over_source_id() -> None:
    assert (
        portal_identity(
            {
                "target_id": "legacy_target",
                "source_id": "cohort_source",
            }
        )
        == "legacy_target"
    )


def test_source_id_is_used_only_when_target_id_is_missing() -> None:
    assert (
        portal_identity({"target_id": "", "source_id": "cohort_source"})
        == "cohort_source"
    )


def test_cutover_keeps_cohort_and_deactivates_only_active_outside_rows() -> None:
    result = classify_job_documents(
        [
            {
                "_id": "1",
                "job_id": "job_1",
                "target_id": "cohort_a",
                "is_active": True,
            },
            {
                "_id": "2",
                "job_id": "job_2",
                "target_id": "cohort_b",
                "is_active": True,
            },
            {
                "_id": "3",
                "job_id": "job_3",
                "target_id": "legacy",
                "is_active": True,
            },
            {
                "_id": "4",
                "job_id": "job_4",
                "target_id": "legacy",
                "is_active": False,
            },
        ],
        cohort_source_ids=["cohort_a", "cohort_b"],
    )
    assert result["cohort_active"] == 2
    assert result["outside_active"] == 1
    assert result["outside_inactive"] == 1
    assert result["expected_active_after_cutover"] == 2
    assert result["expected_inactive_after_cutover"] == 2
    assert result["outside_active_job_ids"] == ["job_3"]
    assert result["outside_job_ids"] == ["job_3", "job_4"]


def test_state_hash_changes_if_activity_changes() -> None:
    rows = [
        {
            "_id": "1",
            "job_id": "job_1",
            "target_id": "cohort_a",
            "is_active": True,
        }
    ]
    before = classify_job_documents(
        rows,
        cohort_source_ids=["cohort_a"],
    )
    rows[0]["is_active"] = False
    after = classify_job_documents(
        rows,
        cohort_source_ids=["cohort_a"],
    )
    assert before["state_sha256"] != after["state_sha256"]


def test_policy_rejects_non_22_source_cutover(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "contract_version": "1.0",
                "phase": "7D4C",
                "cohort_name": "bad",
                "expected_source_count": 1,
                "source_ids": ["only_one"],
                "controls": {
                    "physical_job_deletion_enabled": False,
                    "automatic_scheduler_enabled": False,
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        CohortCutoverError,
        match="exactly_22_sources",
    ):
        load_cutover_policy(path)


def test_repository_policy_contains_exactly_22_unique_sources() -> None:
    root = Path(__file__).resolve().parents[1]
    policy = load_cutover_policy(
        root
        / "configs"
        / "portal_cohorts"
        / "phase7d4c_active22_cutover_policy.json"
    )
    assert len(policy["source_ids"]) == 22
    assert len(set(policy["source_ids"])) == 22
    assert policy["controls"]["automatic_scheduler_enabled"] is False
    assert policy["controls"]["physical_job_deletion_enabled"] is False


def test_cutover_deactivates_only_outside_jobs_and_cleans_derived_data(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    policy = load_cutover_policy(
        root
        / "configs"
        / "portal_cohorts"
        / "phase7d4c_active22_cutover_policy.json"
    )
    cohort_jobs = [
        {
            "_id": f"mongo_{index}",
            "job_id": f"job_{index}",
            "target_id": source_id,
            "source_id": source_id,
            "is_active": True,
        }
        for index, source_id in enumerate(policy["source_ids"], start=1)
    ]
    retained_ids = [row["job_id"] for row in cohort_jobs]
    database = _Database(
        {
            "jobs_current": cohort_jobs
            + [
                {
                    "_id": "legacy_active",
                    "job_id": "legacy_job_active",
                    "target_id": "legacy",
                    "is_active": True,
                },
                {
                    "_id": "legacy_inactive",
                    "job_id": "legacy_job_inactive",
                    "target_id": "legacy",
                    "is_active": False,
                },
            ],
            "jobs_history": [{"job_id": "legacy_job_active"}],
            "job_raw_extractions": [{"job_id": "legacy_job_active"}],
            "job_tower_records": [
                *[{"job_id": job_id} for job_id in retained_ids],
                {"job_id": "legacy_job_active"},
                {"job_id": "legacy_job_inactive"},
            ],
            "qdrant_index_state": [
                *[
                    {"record_type": "job", "record_id": job_id}
                    for job_id in retained_ids
                ],
                {
                    "record_type": "job",
                    "record_id": "legacy_job_active",
                },
                {
                    "record_type": "candidate",
                    "record_id": "candidate_1",
                },
            ],
            "candidate_job_matches": [
                {"job_id": retained_ids[0]},
                {"job_id": "legacy_job_active"},
            ],
            "candidate_job_matches_llm_reranked": [
                {"job_id": retained_ids[0]},
                {"job_id": "legacy_job_inactive"},
            ],
            "recommendation_refresh_requests": [
                {
                    "target_id": policy["source_ids"][0],
                    "status": "pending",
                },
                {"target_id": "legacy", "status": "pending"},
            ],
        }
    )
    plan = build_cutover_plan(database, policy=policy)
    assert plan["ready_to_apply"] is True
    assert plan["counts"]["cohort_active"] == 22
    assert plan["counts"]["outside_active"] == 1

    backup = tmp_path / "mongo.archive.gz"
    with gzip.open(backup, "wb") as handle:
        handle.write(b"verified-test-backup")
    snapshot = tmp_path / "lifecycle.json.gz"
    report = execute_cutover(
        database,
        policy=policy,
        plan=plan,
        backup_archive=backup,
        snapshot_path=snapshot,
        confirmation=CUTOVER_CONFIRMATION,
    )

    assert report["status"] == "passed"
    assert report["deactivated_jobs"] == 1
    assert database["jobs_current"].count_documents({}) == 24
    assert database["jobs_current"].count_documents(
        {
            "target_id": "legacy",
            "is_active": False,
        }
    ) == 2
    assert database["jobs_history"].count_documents({}) == 1
    assert database["job_raw_extractions"].count_documents({}) == 1
    assert database["job_tower_records"].count_documents({}) == 22
    assert database["qdrant_index_state"].count_documents(
        {"record_type": "job"}
    ) == 0
    assert database["qdrant_index_state"].count_documents(
        {"record_type": "candidate"}
    ) == 1
    assert database["candidate_job_matches"].count_documents({}) == 1
    assert (
        database["candidate_job_matches_llm_reranked"].count_documents({})
        == 1
    )
    assert database["recommendation_refresh_requests"].count_documents(
        {"target_id": "legacy", "status": "cancelled"}
    ) == 1
    assert snapshot.is_file()

    before_rebuild = verify_cutover(database, policy=policy)
    assert before_rebuild["status"] == "failed"
    assert "cohort_qdrant_index_state_count_mismatch" in before_rebuild[
        "blockers"
    ]

    database["qdrant_index_state"].rows.extend(
        [
            {"record_type": "job", "record_id": job_id}
            for job_id in retained_ids
        ]
    )
    after_rebuild = verify_cutover(database, policy=policy)
    assert after_rebuild["status"] == "passed"
    assert after_rebuild["active_jobs"] == 22
    assert after_rebuild["inactive_jobs"] == 2
    assert after_rebuild["ready_for_candidate_pipeline"] is True

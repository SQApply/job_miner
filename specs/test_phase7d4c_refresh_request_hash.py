from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import src.warehouse.repositories as repositories_module
from src.warehouse.repositories import WarehouseRepository


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = list(rows)

    def sort(self, *args: Any, **kwargs: Any) -> "_Cursor":
        return self

    def limit(self, count: int) -> "_Cursor":
        self.rows = self.rows[:count]
        return self

    def __iter__(self):
        return iter(self.rows)


class _CandidateCollection:
    def find(self, *args: Any, **kwargs: Any) -> _Cursor:
        return _Cursor(
            [
                {
                    "candidate_id": "candidate-a",
                    "resume_id": "resume-a",
                    "email": "candidate@example.com",
                }
            ]
        )


class _RefreshCollection:
    def __init__(self) -> None:
        self.operations: list[Any] = []

    def bulk_write(
        self,
        operations: list[Any],
        *,
        ordered: bool,
    ) -> SimpleNamespace:
        self.operations = list(operations)
        assert ordered is False
        return SimpleNamespace(upserted_count=1, matched_count=0)


class _Database:
    def __init__(self) -> None:
        self.candidates = _CandidateCollection()
        self.refreshes = _RefreshCollection()

    def __getitem__(self, name: str) -> Any:
        if name == "candidate_tower_records":
            return self.candidates
        if name == "recommendation_refresh_requests":
            return self.refreshes
        raise KeyError(name)


def test_recommendation_refresh_request_hash_receives_one_canonical_value(
    monkeypatch: Any,
) -> None:
    captured: list[Any] = []

    def one_argument_stable_hash(value: Any) -> str:
        captured.append(value)
        return "a" * 64

    monkeypatch.setattr(
        repositories_module,
        "stable_hash",
        one_argument_stable_hash,
    )
    database = _Database()

    result = WarehouseRepository(database).record_recommendation_refresh_requests(
        portal_id="portal-a",
        target_id="target-a",
        run_session_id="run-a",
        changed_job_ids=["job-a"],
    )

    assert result["status"] == "recorded_pending_requests"
    assert result["requests_created"] == 1
    assert len(database.refreshes.operations) == 1
    assert captured == [
        {
            "kind": "recommendation_refresh_request",
            "portal_id": "portal-a",
            "run_session_id": "run-a",
            "candidate_id": "candidate-a",
        }
    ]

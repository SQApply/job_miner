from __future__ import annotations

import copy
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.portals.production_closeout import (
    build_phase6f_closeout_report,
    read_phase6e_manifest,
    write_phase6f_report,
)
from src.portals.production_ingestion import Phase6AIngestionPlan, Phase6ASource
from src.portals.production_runner import (
    Phase6ERunManifest,
    Phase6ESourceResult,
)


class _FakeCollection:
    def __init__(self, documents: list[dict[str, Any]] | None = None) -> None:
        self.documents = [copy.deepcopy(item) for item in (documents or [])]

    @staticmethod
    def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
        for key, expected in query.items():
            actual = document.get(key)
            if isinstance(expected, dict) and "$in" in expected:
                if actual not in expected["$in"]:
                    return False
            elif actual != expected:
                return False
        return True

    def find(self, query: dict[str, Any]):
        return [copy.deepcopy(item) for item in self.documents if self._matches(item, query)]

    def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for item in self.documents if self._matches(item, query))


class _FakeDatabase:
    def __init__(self, collections: dict[str, list[dict[str, Any]]]) -> None:
        self.collections = {
            name: _FakeCollection(documents) for name, documents in collections.items()
        }

    def __getitem__(self, name: str) -> _FakeCollection:
        return self.collections.setdefault(name, _FakeCollection())


def _source(source_id: str, row: int) -> Phase6ASource:
    return Phase6ASource(
        source_id=source_id,
        source_row=row,
        display_name=source_id,
        listing_url=f"https://{source_id}.example/jobs",
        detected_platform="custom_listing",
        bounded_extracted_jobs=3,
        evidence_run_id="cert-run",
    )


def _plan(source_ids: list[str]) -> Phase6AIngestionPlan:
    sources = [_source(source_id, index + 1) for index, source_id in enumerate(source_ids)]
    return Phase6AIngestionPlan(
        plan_id="phase6a-plan",
        generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        cohort_sha256="a" * 64,
        cohort_source_count=len(sources),
        selected_source_count=len(sources),
        deferred_source_count=0,
        selected_source_ids=source_ids,
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


def _manifest(
    *,
    run_id: str,
    source_ids: list[str],
    inserted: int,
    unchanged: int,
    updated: int = 0,
    failed: int = 0,
) -> Phase6ERunManifest:
    results = []
    for index, source_id in enumerate(source_ids):
        if failed and index == len(source_ids) - 1:
            results.append(
                Phase6ESourceResult(
                    source_id=source_id,
                    display_name=source_id,
                    status="failed",
                    accepted_count=0,
                    error_type="network_error",
                )
            )
        else:
            result_inserted = inserted if len(source_ids) == 1 else int(inserted > 0)
            result_unchanged = unchanged if len(source_ids) == 1 else int(unchanged > 0)
            result_updated = updated if len(source_ids) == 1 else int(updated > 0)
            accepted = result_inserted + result_unchanged + result_updated
            results.append(
                Phase6ESourceResult(
                    source_id=source_id,
                    display_name=source_id,
                    status="success",
                    accepted_count=accepted,
                    extracted_count=accepted,
                    inserted_count=result_inserted,
                    unchanged_count=result_unchanged,
                    updated_count=result_updated,
                )
            )
    accepted_total = sum(item.accepted_count for item in results)
    return Phase6ERunManifest(
        run_id=run_id,
        generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        plan_id="phase6a-plan",
        cohort_sha256="a" * 64,
        execution_mode="write",
        selected_source_ids=source_ids,
        requested_source_count=len(source_ids),
        completed_source_count=len(source_ids),
        successful_source_count=len(source_ids) - failed,
        failed_source_count=failed,
        blocked_source_count=0,
        cancelled_source_count=0,
        extracted_job_count=accepted_total,
        accepted_job_count=accepted_total,
        quarantined_job_count=0,
        inserted_job_count=inserted,
        updated_job_count=updated,
        unchanged_job_count=unchanged,
        reactivated_job_count=0,
        source_results=results,
        controls={
            "normalized_job_writes_enabled": True,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
        },
    )


def _manifest_files(first: Phase6ERunManifest, rerun: Phase6ERunManifest) -> tuple[Path, Path, tempfile.TemporaryDirectory[str]]:
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    first_path = root / "first.json"
    rerun_path = root / "rerun.json"
    first_path.write_text(json.dumps(first.model_dump(mode="json")), encoding="utf-8")
    rerun_path.write_text(json.dumps(rerun.model_dump(mode="json")), encoding="utf-8")
    return first_path, rerun_path, temporary


def _database(source_id: str, *, duplicate: bool = False, history_versions: int = 1) -> _FakeDatabase:
    jobs = [
        {
            "job_id": "job-1",
            "source_id": source_id,
            "external_job_id": "REQ-1",
            "identity_hash": "1" * 64,
            "last_fleet_run_id": "run-rerun",
            "is_active": True,
            "deactivated_at": None,
            "version": history_versions,
        }
    ]
    if duplicate:
        jobs.append(
            {
                "job_id": "job-2",
                "source_id": source_id,
                "external_job_id": "REQ-2",
                "identity_hash": "1" * 64,
                "last_fleet_run_id": "run-rerun",
                "is_active": True,
                "deactivated_at": None,
                "version": 1,
            }
        )
    history = [
        {"history_id": f"history-{index}", "job_id": "job-1"}
        for index in range(history_versions)
    ]
    if duplicate:
        history.append({"history_id": "history-job-2", "job_id": "job-2"})
    return _FakeDatabase(
        {
            "production_ingestion_fleet_runs": [
                {"fleet_run_id": "run-first", "status": "completed"},
                {"fleet_run_id": "run-rerun", "status": "completed"},
            ],
            "production_ingestion_source_runs": [
                {"source_run_id": "source-first", "fleet_run_id": "run-first", "source_id": source_id, "status": "success"},
                {"source_run_id": "source-rerun", "fleet_run_id": "run-rerun", "source_id": source_id, "status": "success"},
            ],
            "jobs_current": jobs,
            "jobs_history": history,
            "production_job_quarantine": [],
        }
    )


def test_phase6f_passes_for_idempotent_write_and_rerun() -> None:
    source_ids = ["source-a"]
    first = _manifest(run_id="run-first", source_ids=source_ids, inserted=1, unchanged=0)
    rerun = _manifest(run_id="run-rerun", source_ids=source_ids, inserted=0, unchanged=1)
    first_path, rerun_path, temporary = _manifest_files(first, rerun)
    try:
        report = build_phase6f_closeout_report(
            plan=_plan(source_ids),
            first_write_manifest=first,
            rerun_manifest=rerun,
            first_write_manifest_path=first_path,
            rerun_manifest_path=rerun_path,
            db=_database(source_ids[0]),
            expected_source_count=1,
        )
        assert report.status == "passed"
        assert report.ready_for_phase7 is True
        assert report.issues == []
        assert report.database_checks.current_jobs_observed_on_rerun == 1
    finally:
        temporary.cleanup()


def test_phase6f_rejects_non_idempotent_rerun_insertions() -> None:
    source_ids = ["source-a"]
    first = _manifest(run_id="run-first", source_ids=source_ids, inserted=1, unchanged=0)
    rerun = _manifest(run_id="run-rerun", source_ids=source_ids, inserted=1, unchanged=0)
    first_path, rerun_path, temporary = _manifest_files(first, rerun)
    try:
        report = build_phase6f_closeout_report(
            plan=_plan(source_ids),
            first_write_manifest=first,
            rerun_manifest=rerun,
            first_write_manifest_path=first_path,
            rerun_manifest_path=rerun_path,
            db=_database(source_ids[0]),
            expected_source_count=1,
        )
        assert report.status == "failed"
        assert any("idempotent rerun" in issue for issue in report.issues)
    finally:
        temporary.cleanup()


def test_phase6f_rejects_duplicate_identity_hashes() -> None:
    source_ids = ["source-a"]
    first = _manifest(run_id="run-first", source_ids=source_ids, inserted=1, unchanged=0)
    rerun = _manifest(run_id="run-rerun", source_ids=source_ids, inserted=0, unchanged=1)
    first_path, rerun_path, temporary = _manifest_files(first, rerun)
    try:
        report = build_phase6f_closeout_report(
            plan=_plan(source_ids),
            first_write_manifest=first,
            rerun_manifest=rerun,
            first_write_manifest_path=first_path,
            rerun_manifest_path=rerun_path,
            db=_database(source_ids[0], duplicate=True),
            expected_source_count=1,
        )
        assert report.status == "failed"
        assert report.database_checks.duplicate_identity_hashes == 1
    finally:
        temporary.cleanup()


def test_phase6f_rejects_history_version_mismatch() -> None:
    source_ids = ["source-a"]
    first = _manifest(run_id="run-first", source_ids=source_ids, inserted=1, unchanged=0)
    rerun = _manifest(run_id="run-rerun", source_ids=source_ids, inserted=0, unchanged=1)
    first_path, rerun_path, temporary = _manifest_files(first, rerun)
    database = _database(source_ids[0], history_versions=2)
    database["jobs_history"].documents.pop()
    try:
        report = build_phase6f_closeout_report(
            plan=_plan(source_ids),
            first_write_manifest=first,
            rerun_manifest=rerun,
            first_write_manifest_path=first_path,
            rerun_manifest_path=rerun_path,
            db=database,
            expected_source_count=1,
        )
        assert report.status == "failed"
        assert report.database_checks.history_mismatches == 1
    finally:
        temporary.cleanup()


def test_phase6f_rejects_failed_source_runs() -> None:
    source_ids = ["source-a"]
    first = _manifest(run_id="run-first", source_ids=source_ids, inserted=0, unchanged=0, failed=1)
    rerun = _manifest(run_id="run-rerun", source_ids=source_ids, inserted=0, unchanged=1)
    first_path, rerun_path, temporary = _manifest_files(first, rerun)
    try:
        report = build_phase6f_closeout_report(
            plan=_plan(source_ids),
            first_write_manifest=first,
            rerun_manifest=rerun,
            first_write_manifest_path=first_path,
            rerun_manifest_path=rerun_path,
            db=_database(source_ids[0]),
            expected_source_count=1,
        )
        assert report.status == "failed"
        assert any("successful_source_count" in issue for issue in report.issues)
    finally:
        temporary.cleanup()


def test_phase6f_manifest_read_and_atomic_report_write() -> None:
    source_ids = ["source-a"]
    first = _manifest(run_id="run-first", source_ids=source_ids, inserted=1, unchanged=0)
    rerun = _manifest(run_id="run-rerun", source_ids=source_ids, inserted=0, unchanged=1)
    first_path, rerun_path, temporary = _manifest_files(first, rerun)
    try:
        loaded = read_phase6e_manifest(first_path)
        assert loaded.run_id == "run-first"
        report = build_phase6f_closeout_report(
            plan=_plan(source_ids),
            first_write_manifest=first,
            rerun_manifest=rerun,
            first_write_manifest_path=first_path,
            rerun_manifest_path=rerun_path,
            db=_database(source_ids[0]),
            expected_source_count=1,
        )
        output = write_phase6f_report(Path(temporary.name) / "report.json", report)
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["phase"] == "6F"
        assert payload["status"] == "passed"
    finally:
        temporary.cleanup()

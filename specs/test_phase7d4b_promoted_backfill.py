from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from specs.test_phase7d4a_production_cohort import _phase7d4a_fixtures
from src.portals.production_cohort23 import write_phase7d4a_artifacts
from src.portals.production_cohort23_backfill import (
    PHASE_7D4B_WRITE_CONFIRMATION,
    Phase7D4BSourceSnapshot,
    ProductionCohort23BackfillError,
    batch_checkpoint_path,
    build_phase7d4b_batch_report,
    build_phase7d4b_runner_config,
    load_phase7d4b_authorization,
    read_phase7d4b_report,
    require_phase7d4b_checkpoint_state,
    require_phase7d4b_write_confirmation,
    write_phase7d4b_report,
)
from src.portals.production_runner import (
    Phase6ERunManifest,
    Phase6ESourceResult,
)


def _authorization(root: Path, *, ordinal: int = 1):
    root.mkdir(parents=True, exist_ok=True)
    _, _, _, _, cohort, rollout = _phase7d4a_fixtures()
    cohort_path = root / "cohort.json"
    rollout_path = root / "rollout.json"
    write_phase7d4a_artifacts(
        cohort_path=cohort_path,
        rollout_path=rollout_path,
        cohort=cohort,
        rollout=rollout,
    )
    plan, authorization = load_phase7d4b_authorization(
        cohort_path=cohort_path,
        rollout_plan_path=rollout_path,
        batch_ordinal=ordinal,
    )
    return cohort, rollout, plan, authorization


def _manifest(
    authorization,
    *,
    partial_index: int | None = None,
    deferred_index: int | None = None,
    rejected_index: int | None = None,
) -> Phase6ERunManifest:
    results: list[Phase6ESourceResult] = []
    for index, source_id in enumerate(authorization.source_ids):
        partial = index == partial_index
        deferred = index == deferred_index
        rejected = index == rejected_index
        accepted = 0 if deferred else 1
        complete = not (partial or deferred or rejected)
        results.append(
            Phase6ESourceResult(
                source_id=source_id,
                display_name=source_id,
                status="success" if complete else "failed",
                certification_status=(
                    "passed" if complete else "catalog_incomplete"
                ),
                discovered_count=0 if deferred else 1,
                attempted_count=0 if deferred else 1,
                extracted_count=0 if deferred else 1,
                accepted_count=accepted,
                rejected_count=1 if rejected else 0,
                inserted_count=accepted,
                catalog_mode="complete_catalog",
                discovery_complete=complete,
                catalog_complete=complete,
                error_type=None if complete else "catalog_incomplete",
            )
        )
    return Phase6ERunManifest(
        run_id=f"phase7d4b-test-{authorization.batch_ordinal}",
        generated_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        plan_id=authorization.production_plan_id,
        cohort_sha256=authorization.cohort_sha256,
        execution_mode="write",
        selected_source_ids=authorization.source_ids,
        requested_source_count=authorization.source_count,
        completed_source_count=authorization.source_count,
        successful_source_count=sum(result.status == "success" for result in results),
        failed_source_count=sum(result.status == "failed" for result in results),
        blocked_source_count=0,
        cancelled_source_count=0,
        extracted_job_count=sum(result.extracted_count for result in results),
        accepted_job_count=sum(result.accepted_count for result in results),
        quarantined_job_count=0,
        inserted_job_count=sum(result.inserted_count for result in results),
        updated_job_count=0,
        unchanged_job_count=0,
        reactivated_job_count=0,
        source_results=results,
        controls={
            "normalized_job_writes_enabled": True,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "max_source_concurrency": 1,
            "max_attempts": 2,
            "max_jobs_per_source": None,
            "max_pages_per_source": 500,
            "catalog_mode": "complete_catalog",
            "catalog_completion_required": True,
            "bounded_pilot_execution": False,
        },
    )


def _snapshots(authorization, manifest):
    before_values = {source_id: 0 for source_id in authorization.source_ids}
    inserted_by_source = {
        result.source_id: result.inserted_count for result in manifest.source_results
    }
    return (
        Phase7D4BSourceSnapshot(
            source_ids=authorization.source_ids,
            current_by_source=before_values,
            active_by_source=before_values,
            current_total=0,
            active_total=0,
        ),
        Phase7D4BSourceSnapshot(
            source_ids=authorization.source_ids,
            current_by_source=inserted_by_source,
            active_by_source=inserted_by_source,
            current_total=sum(inserted_by_source.values()),
            active_total=sum(inserted_by_source.values()),
        ),
    )


def _report(authorization, manifest):
    before, after = _snapshots(authorization, manifest)
    return build_phase7d4b_batch_report(
        authorization=authorization,
        manifest=manifest,
        before=before,
        after=after,
        generated_at=datetime(2026, 7, 24, 12, tzinfo=timezone.utc),
    )


def test_authorization_is_exactly_the_signed_seven_promoted_sources() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        cohort, rollout, plan, first = _authorization(root / "first", ordinal=1)
        _, _, _, second = _authorization(root / "second", ordinal=2)
    assert first.source_ids + second.source_ids == cohort["promoted_backfill_source_ids"]
    assert first.source_ids + second.source_ids == rollout["initial_backfill"]["source_ids"]
    assert [first.source_count, second.source_count] == [4, 3]
    assert plan.selected_source_ids == cohort["cohort_source_ids"]
    assert len(plan.selected_source_ids) == 23
    assert set(cohort["failed_candidate_source_ids"]).isdisjoint(
        first.source_ids + second.source_ids
    )


def test_runner_config_is_unbounded_single_gpu_safe_write_mode() -> None:
    with TemporaryDirectory() as raw:
        _, _, _, authorization = _authorization(Path(raw))
    config = build_phase7d4b_runner_config(authorization)
    assert config.execution_mode == "write"
    assert config.catalog_mode == "complete_catalog"
    assert config.max_jobs is None
    assert config.max_pages == 500
    assert config.max_source_concurrency == 1
    assert config.detail_concurrency == 1
    assert config.allow_llm_fallback is True
    assert config.lifecycle_reconciliation_enabled is False
    assert config.deactivation_enabled is False


def test_write_confirmation_is_exact() -> None:
    require_phase7d4b_write_confirmation(PHASE_7D4B_WRITE_CONFIRMATION)
    with pytest.raises(
        ProductionCohort23BackfillError,
        match="confirm-production-writes",
    ):
        require_phase7d4b_write_confirmation("yes")


def test_complete_batch_passes_and_round_trips() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        _, _, _, authorization = _authorization(root / "inputs")
        report = _report(authorization, _manifest(authorization))
        stored = write_phase7d4b_report(root / "report.json", report)
        loaded = read_phase7d4b_report(stored)
    assert loaded["status"] == "passed"
    assert loaded["complete_catalog_source_count"] == authorization.source_count
    assert loaded["ready_for_next_batch_or_closeout"] is True
    assert loaded["controls"]["lifecycle_reconciliation_enabled"] is False
    assert loaded["controls"]["deactivation_enabled"] is False


def test_productive_incomplete_source_is_kept_as_partial_without_reconciliation() -> None:
    with TemporaryDirectory() as raw:
        _, _, _, authorization = _authorization(Path(raw))
        report = _report(
            authorization,
            _manifest(authorization, partial_index=0),
        )
    assert report["status"] == "passed_with_partial"
    assert report["productive_partial_source_ids"] == [authorization.source_ids[0]]
    assert report["deferred_source_ids"] == []
    assert report["ready_for_next_batch_or_closeout"] is True
    assert report["controls"]["failed_or_partial_runs_increment_missing_count"] is False


def test_nonproductive_source_is_deferred_without_blocking_other_sources() -> None:
    with TemporaryDirectory() as raw:
        _, _, _, authorization = _authorization(Path(raw))
        report = _report(
            authorization,
            _manifest(authorization, deferred_index=0),
        )
    assert report["status"] == "passed_with_partial"
    assert report["deferred_source_ids"] == [authorization.source_ids[0]]
    assert report["productive_source_ids"] == authorization.source_ids[1:]
    assert report["ready_for_next_batch_or_closeout"] is True


def test_rejected_persistence_record_is_a_safety_blocker() -> None:
    with TemporaryDirectory() as raw:
        _, _, _, authorization = _authorization(Path(raw))
        report = _report(
            authorization,
            _manifest(authorization, rejected_index=0),
        )
    assert report["status"] == "failed"
    assert report["ready_for_next_batch_or_closeout"] is False
    assert any(
        "source_persistence_or_validation_exception" in blocker
        for blocker in report["blockers"]
    )


def test_database_delta_mismatch_blocks_progression() -> None:
    with TemporaryDirectory() as raw:
        _, _, _, authorization = _authorization(Path(raw))
        manifest = _manifest(authorization)
        before, after = _snapshots(authorization, manifest)
        bad_after = after.model_copy(
            update={
                "current_by_source": {
                    **after.current_by_source,
                    authorization.source_ids[0]: 0,
                },
                "active_by_source": {
                    **after.active_by_source,
                    authorization.source_ids[0]: 0,
                },
                "current_total": after.current_total - 1,
                "active_total": after.active_total - 1,
            }
        )
        report = build_phase7d4b_batch_report(
            authorization=authorization,
            manifest=manifest,
            before=before,
            after=bad_after,
        )
    assert report["status"] == "failed"
    assert "database_current_delta_mismatch" in report["blockers"]
    assert "database_active_delta_mismatch" in report["blockers"]


def test_checkpoint_order_and_duplicate_write_guards() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        _, _, _, first = _authorization(root / "first", ordinal=1)
        _, _, _, second = _authorization(root / "second", ordinal=2)
        with pytest.raises(
            ProductionCohort23BackfillError,
            match="checkpoint is missing",
        ):
            require_phase7d4b_checkpoint_state(
                output_dir=root / "checkpoints",
                authorization=second,
                resume_incomplete=False,
            )
        first_report = _report(first, _manifest(first, partial_index=0))
        write_phase7d4b_report(
            batch_checkpoint_path(root / "checkpoints", 1),
            first_report,
        )
        require_phase7d4b_checkpoint_state(
            output_dir=root / "checkpoints",
            authorization=second,
            resume_incomplete=False,
        )
        with pytest.raises(
            ProductionCohort23BackfillError,
            match="already completed",
        ):
            require_phase7d4b_checkpoint_state(
                output_dir=root / "checkpoints",
                authorization=first,
                resume_incomplete=False,
            )


def test_tampered_report_is_rejected() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        _, _, _, authorization = _authorization(root / "inputs")
        report = _report(authorization, _manifest(authorization))
        stored = write_phase7d4b_report(root / "report.json", report)
        payload = stored.read_text(encoding="utf-8")
        stored.write_text(
            payload.replace('"status": "passed"', '"status": "failed"'),
            encoding="utf-8",
        )
        with pytest.raises(
            ProductionCohort23BackfillError,
            match="checksum",
        ):
            read_phase7d4b_report(stored)

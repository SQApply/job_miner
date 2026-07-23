from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from specs.test_phase7d3a_guarded_rollout import _production_inputs
from src.portals.certification import CertificationOptions
from src.portals.production_guarded_execution import (
    PHASE_7D3B_WRITE_CONFIRMATION,
    Phase7D3BSourceSnapshot,
    ProductionGuardedExecutionError,
    _canonical_sha256,
    batch_checkpoint_path,
    build_phase7d3b_batch_report,
    can_defer_phase7d3b_quality_shortfall,
    load_phase7d3b_authorization,
    read_phase7d3b_report,
    reclassify_phase7d3b_deferred_quality_checkpoint,
    require_phase7d3b_checkpoint_state,
    require_phase7d3b_write_confirmation,
    write_phase7d3b_report,
)
from src.portals.production_guarded_rollout import (
    build_phase7d3a_rollout_plan,
    write_phase7d3a_rollout_plan,
)
from src.portals.production_runner import (
    Phase6ERunManifest,
    Phase6ERunnerConfig,
    Phase6ESourceResult,
)


def _authorization(root: Path, *, ordinal: int = 1):
    root.mkdir(parents=True, exist_ok=True)
    plan, selection, semantic = _production_inputs()
    plan_path = root / "production_plan.json"
    plan_path.write_text(
        json.dumps(plan.model_dump(mode="json")),
        encoding="utf-8",
    )
    rollout = build_phase7d3a_rollout_plan(
        production_plan=plan,
        selection=selection,
        semantic_closeout=semantic,
        semantic_closeout_sha256=semantic["report_sha256"],
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    rollout_path = root / "rollout.json"
    write_phase7d3a_rollout_plan(rollout_path, rollout)
    loaded_plan, authorization = load_phase7d3b_authorization(
        rollout_plan_path=rollout_path,
        production_plan_path=plan_path,
        batch_ordinal=ordinal,
    )
    return loaded_plan, authorization


def _manifest(authorization, *, complete: bool = True) -> Phase6ERunManifest:
    results: list[Phase6ESourceResult] = []
    for index, source_id in enumerate(authorization.source_ids):
        source_complete = complete or index > 0
        results.append(
            Phase6ESourceResult(
                source_id=source_id,
                display_name=source_id,
                status="success" if source_complete else "failed",
                certification_status=(
                    "passed" if source_complete else "catalog_incomplete"
                ),
                discovered_count=1,
                attempted_count=1,
                extracted_count=1,
                accepted_count=1,
                inserted_count=1,
                catalog_mode="complete_catalog",
                discovery_complete=source_complete,
                catalog_complete=source_complete,
                error_type=None if source_complete else "catalog_incomplete",
            )
        )
    successful = sum(result.status == "success" for result in results)
    return Phase6ERunManifest(
        run_id=f"phase7d3b-test-{authorization.batch_ordinal}",
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        plan_id=authorization.production_plan_id,
        cohort_sha256=authorization.cohort_sha256,
        execution_mode="write",
        selected_source_ids=authorization.source_ids,
        requested_source_count=authorization.source_count,
        completed_source_count=authorization.source_count,
        successful_source_count=successful,
        failed_source_count=authorization.source_count - successful,
        blocked_source_count=0,
        cancelled_source_count=0,
        extracted_job_count=authorization.source_count,
        accepted_job_count=authorization.source_count,
        quarantined_job_count=0,
        inserted_job_count=authorization.source_count,
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


def _snapshot(source_ids: list[str], *, count: int) -> Phase7D3BSourceSnapshot:
    values = {source_id: count for source_id in source_ids}
    return Phase7D3BSourceSnapshot(
        source_ids=source_ids,
        current_by_source=values,
        active_by_source=values,
        current_total=len(source_ids) * count,
        active_total=len(source_ids) * count,
    )


def test_authorization_uses_exact_ordered_complete_catalog_batch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        plan, authorization = _authorization(Path(raw), ordinal=1)
    assert authorization.source_ids == plan.selected_source_ids[:4]
    assert authorization.source_count == 4
    assert authorization.catalog_mode == "complete_catalog"
    assert authorization.max_jobs_per_source is None
    assert authorization.page_safety_cap == 500


def test_last_batch_contains_two_sources() -> None:
    with tempfile.TemporaryDirectory() as raw:
        plan, authorization = _authorization(Path(raw), ordinal=7)
    assert authorization.source_ids == plan.selected_source_ids[-2:]
    assert authorization.source_count == 2


def test_complete_catalog_configs_remove_ten_job_cap() -> None:
    cert = CertificationOptions(
        catalog_mode="complete_catalog",
        max_jobs=None,
        max_pages=500,
    )
    runner = Phase6ERunnerConfig(
        catalog_mode="complete_catalog",
        max_jobs=None,
        max_pages=500,
    )
    assert cert.max_jobs is None
    assert runner.certification_options().catalog_mode == "complete_catalog"
    with pytest.raises(ValueError, match="cannot set max_jobs"):
        CertificationOptions(catalog_mode="complete_catalog", max_jobs=10)
    with pytest.raises(ValueError, match="requires max_jobs"):
        Phase6ERunnerConfig(catalog_mode="bounded_certification", max_jobs=None)


def test_confirmation_is_exact() -> None:
    require_phase7d3b_write_confirmation(PHASE_7D3B_WRITE_CONFIRMATION)
    with pytest.raises(ProductionGuardedExecutionError, match="confirm-production-writes"):
        require_phase7d3b_write_confirmation("yes")


def test_clean_complete_batch_report_passes_and_round_trips() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _, authorization = _authorization(root)
        report = build_phase7d3b_batch_report(
            authorization=authorization,
            manifest=_manifest(authorization),
            before=_snapshot(authorization.source_ids, count=0),
            after=_snapshot(authorization.source_ids, count=1),
            generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )
        target = write_phase7d3b_report(root / "report.json", report)
        loaded = read_phase7d3b_report(target)
    assert loaded["status"] == "passed"
    assert loaded["ready_for_next_batch_or_closeout"] is True
    assert loaded["complete_catalog_source_count"] == authorization.source_count
    assert loaded["blockers"] == []


def test_expected_incomplete_catalog_is_deferred_without_enabling_deactivation() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _, authorization = _authorization(root)
        report = build_phase7d3b_batch_report(
            authorization=authorization,
            manifest=_manifest(authorization, complete=False),
            before=_snapshot(authorization.source_ids, count=0),
            after=_snapshot(authorization.source_ids, count=1),
        )
    assert report["status"] == "passed_with_deferred"
    assert report["ready_for_next_batch_or_closeout"] is True
    assert report["deferred_source_count"] == 1
    assert report["deferred_reasons"][authorization.source_ids[0]] == "catalog_incomplete"
    assert report["controls"]["lifecycle_reconciliation_enabled"] is False
    assert report["controls"]["deactivation_enabled"] is False


def test_unexpected_source_failure_still_blocks_later_batches() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _, authorization = _authorization(root)
        manifest = _manifest(authorization, complete=False)
        first = manifest.source_results[0].model_copy(
            update={"error_type": "unsafe_url"}
        )
        manifest = manifest.model_copy(
            update={"source_results": [first, *manifest.source_results[1:]]}
        )
        report = build_phase7d3b_batch_report(
            authorization=authorization,
            manifest=manifest,
            before=_snapshot(authorization.source_ids, count=0),
            after=_snapshot(authorization.source_ids, count=1),
        )
    assert report["status"] == "failed"
    assert report["ready_for_next_batch_or_closeout"] is False
    assert any("source_unexpected_terminal_state" in item for item in report["blockers"])


def test_incomplete_source_with_isolated_quarantine_is_safely_deferred() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _, authorization = _authorization(root)
        manifest = _manifest(authorization, complete=False)
        first = manifest.source_results[0].model_copy(
            update={"quarantined_count": 2}
        )
        manifest = manifest.model_copy(
            update={
                "quarantined_job_count": 2,
                "source_results": [first, *manifest.source_results[1:]],
            }
        )
        report = build_phase7d3b_batch_report(
            authorization=authorization,
            manifest=manifest,
            before=_snapshot(authorization.source_ids, count=0),
            after=_snapshot(authorization.source_ids, count=1),
        )
    assert can_defer_phase7d3b_quality_shortfall(first) is True
    assert report["status"] == "passed_with_deferred"
    assert report["blockers"] == []
    assert report["counters"]["quarantined"] == 2
    assert report["controls"]["lifecycle_reconciliation_enabled"] is False
    assert report["controls"]["deactivation_enabled"] is False


def test_rejected_record_is_not_offline_deferrable() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _, authorization = _authorization(root)
        manifest = _manifest(authorization, complete=False)
        first = manifest.source_results[0].model_copy(
            update={"quarantined_count": 1, "rejected_count": 1}
        )
        manifest = manifest.model_copy(
            update={
                "quarantined_job_count": 1,
                "source_results": [first, *manifest.source_results[1:]],
            }
        )
        report = build_phase7d3b_batch_report(
            authorization=authorization,
            manifest=manifest,
            before=_snapshot(authorization.source_ids, count=0),
            after=_snapshot(authorization.source_ids, count=1),
        )
    assert can_defer_phase7d3b_quality_shortfall(first) is False
    assert report["status"] == "failed"
    assert any("source_quality_shortfall" in item for item in report["blockers"])


def test_legacy_quality_only_checkpoint_is_reclassified_without_rerun() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _, authorization = _authorization(root)
        manifest = _manifest(authorization, complete=False)
        first = manifest.source_results[0].model_copy(
            update={"quarantined_count": 2}
        )
        manifest = manifest.model_copy(
            update={
                "quarantined_job_count": 2,
                "source_results": [first, *manifest.source_results[1:]],
            }
        )
        current = build_phase7d3b_batch_report(
            authorization=authorization,
            manifest=manifest,
            before=_snapshot(authorization.source_ids, count=0),
            after=_snapshot(authorization.source_ids, count=1),
        )

    legacy = dict(current)
    legacy.pop("report_sha256")
    blocker = f"source_quality_shortfall:{authorization.source_ids[0]}"
    legacy.update(
        {
            "status": "failed",
            "ready_for_next_batch_or_closeout": False,
            "blockers": [blocker],
            "checkpoint_reclassification": None,
        }
    )
    legacy["report_sha256"] = _canonical_sha256(legacy)
    revised = reclassify_phase7d3b_deferred_quality_checkpoint(
        legacy,
        reclassified_at=datetime(2026, 7, 22, 18, 0, tzinfo=timezone.utc),
    )

    assert revised["status"] == "passed_with_deferred"
    assert revised["ready_for_next_batch_or_closeout"] is True
    assert revised["blockers"] == []
    assert revised["checkpoint_reclassification"]["removed_blockers"] == [blocker]
    assert revised["checkpoint_reclassification"]["mongodb_writes"] is False
    assert revised["controls"]["lifecycle_reconciliation_enabled"] is False
    assert revised["controls"]["deactivation_enabled"] is False


def test_checkpoint_order_and_resume_guards() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _, first = _authorization(root / "first", ordinal=1)
        first_report = build_phase7d3b_batch_report(
            authorization=first,
            manifest=_manifest(first),
            before=_snapshot(first.source_ids, count=0),
            after=_snapshot(first.source_ids, count=1),
        )
        with pytest.raises(ProductionGuardedExecutionError, match="checkpoint is missing"):
            require_phase7d3b_checkpoint_state(
                output_dir=root,
                authorization=_authorization(root / "second", ordinal=2)[1],
                resume_incomplete=False,
            )
        write_phase7d3b_report(batch_checkpoint_path(root, 1), first_report)
        require_phase7d3b_checkpoint_state(
            output_dir=root,
            authorization=_authorization(root / "second", ordinal=2)[1],
            resume_incomplete=False,
        )
        with pytest.raises(ProductionGuardedExecutionError, match="already passed"):
            require_phase7d3b_checkpoint_state(
                output_dir=root,
                authorization=first,
                resume_incomplete=False,
            )


def test_report_checksum_tampering_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _, authorization = _authorization(root)
        report = build_phase7d3b_batch_report(
            authorization=authorization,
            manifest=_manifest(authorization),
            before=_snapshot(authorization.source_ids, count=0),
            after=_snapshot(authorization.source_ids, count=1),
        )
        target = write_phase7d3b_report(root / "report.json", report)
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["status"] = "failed"
        target.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ProductionGuardedExecutionError, match="checksum"):
            read_phase7d3b_report(target)

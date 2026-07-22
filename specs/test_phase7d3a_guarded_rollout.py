from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from specs.test_phase7d2a_production_pilot import _fixture as phase7d1_fixture
from src.portals.production_guarded_rollout import (
    ProductionGuardedRolloutError,
    build_phase7d3a_rollout_plan,
    load_phase7d3a_evidence,
    read_phase7d3a_rollout_plan,
    write_phase7d3a_rollout_plan,
)
from src.portals.production_ingestion import Phase6AIngestionPlan, Phase6ASource
from src.portals.production_pilot import Phase7D2PilotSelection


def _canonical(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _semantic(
    *,
    plan_id: str,
    cohort_sha256: str,
    source_ids: list[str],
    accepted: int,
    updated: int,
) -> dict[str, Any]:
    accepted_per_source = accepted // len(source_ids)
    updated_per_source = updated // len(source_ids)
    payload: dict[str, Any] = {
        "contract_version": "1.0",
        "phase": "7D2C1",
        "status": "passed_with_bounded_content_variance",
        "ready_for_phase7d3": True,
        "generated_from_run_id": "phase7d2c-test",
        "plan_id": plan_id,
        "cohort_sha256": cohort_sha256,
        "source_ids": source_ids,
        "evidence": {
            "phase7d2b_report_sha256": "1" * 64,
            "first_write_manifest_sha256": "2" * 64,
            "phase7d2c_strict_report_sha256": "3" * 64,
            "rerun_manifest_sha256": "4" * 64,
            "phase6f_closeout_sha256": "5" * 64,
        },
        "strict_gate": {
            "status": "failed",
            "ready_for_phase7d3": False,
            "blockers": [
                "rerun_manifest_count_mismatch:updated_job_count",
                "rerun_manifest_count_mismatch:unchanged_job_count",
            ],
        },
        "semantic_idempotency": {
            "accepted": accepted,
            "inserted": 0,
            "updated": updated,
            "unchanged": accepted - updated,
            "reactivated": 0,
            "quarantined": 0,
            "maximum_updated_jobs": updated,
            "maximum_updated_jobs_per_source": 1,
            "identity_reuse_proven_by_zero_inserts": True,
        },
        "source_results": [
            {
                "source_id": source_id,
                "accepted": accepted_per_source,
                "inserted": 0,
                "updated": updated_per_source,
                "unchanged": accepted_per_source - updated_per_source,
                "reactivated": 0,
                "quarantined": 0,
                "rejected": 0,
            }
            for source_id in source_ids
        ],
        "database_checks": {
            "fleet_run_records": 2,
            "source_run_records_first": len(source_ids),
            "source_run_records_rerun": len(source_ids),
            "current_jobs_observed_on_rerun": accepted,
            "active_jobs_observed_on_rerun": accepted,
            "duplicate_identity_hashes": 0,
            "duplicate_external_job_keys": 0,
            "history_mismatches": 0,
            "unsafe_deactivated_jobs": 0,
            "quarantine_records_first": 0,
            "quarantine_records_rerun": 0,
        },
        "controls": {
            "network_requests_performed": False,
            "scraping_performed": False,
            "mongodb_reads_performed": False,
            "mongodb_writes_performed": False,
            "normalized_job_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "original_strict_failure_preserved": True,
            "variance_bound_relaxed": False,
        },
        "blockers": [],
    }
    payload["report_sha256"] = _canonical(payload)
    return payload


def _production_inputs() -> tuple[
    Phase6AIngestionPlan,
    Phase7D2PilotSelection,
    dict[str, Any],
]:
    source_ids = [f"cert_source_{index:02d}" for index in range(1, 27)]
    sources = [
        Phase6ASource(
            source_id=source_id,
            source_row=index,
            display_name=source_id,
            listing_url=f"https://source-{index}.example/jobs",
            detected_platform="custom_listing",
            bounded_extracted_jobs=10,
            evidence_run_id="phase7d1-test",
        )
        for index, source_id in enumerate(source_ids, start=1)
    ]
    plan = Phase6AIngestionPlan(
        plan_id="phase6a-production-test",
        generated_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        cohort_sha256="a" * 64,
        cohort_source_count=26,
        selected_source_count=26,
        deferred_source_count=76,
        selected_source_ids=source_ids,
        sources=sources,
        controls={
            "execution_mode": "plan_only",
            "max_source_concurrency": 1,
            "production_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
        },
    )
    seeded = [source_ids[2], source_ids[19]]
    selection = Phase7D2PilotSelection(
        plan_id=plan.plan_id,
        cohort_sha256=plan.cohort_sha256,
        quality_report_sha256="b" * 64,
        full_cohort_source_count=26,
        deferred_source_count=76,
        source_ids=seeded,
        expected_source_count=2,
    )
    semantic = _semantic(
        plan_id=plan.plan_id,
        cohort_sha256=plan.cohort_sha256,
        source_ids=seeded,
        accepted=20,
        updated=2,
    )
    return plan, selection, semantic


class Phase7D3AGuardedRolloutTests(unittest.TestCase):
    def test_signed_phase7d1_and_semantic_evidence_are_linked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cohort, quality, plan_path, seeded = phase7d1_fixture(root)
            plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
            semantic = _semantic(
                plan_id=plan_payload["plan_id"],
                cohort_sha256=plan_payload["cohort_sha256"],
                source_ids=seeded,
                accepted=2,
                updated=2,
            )
            semantic_path = _write(root / "semantic.json", semantic)
            plan, selection, loaded, checksum = load_phase7d3a_evidence(
                cohort_path=cohort,
                quality_report_path=quality,
                production_plan_path=plan_path,
                semantic_closeout_path=semantic_path,
                expected_inventory=4,
                expected_cohort_size=3,
                expected_seeded_sources=2,
                expected_seeded_jobs=2,
                expected_updated_jobs=2,
            )
        self.assertEqual(plan.selected_source_count, 3)
        self.assertEqual(selection.source_ids, seeded)
        self.assertEqual(loaded["source_ids"], seeded)
        self.assertEqual(checksum, semantic["report_sha256"])

    def test_tampered_semantic_closeout_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cohort, quality, plan_path, seeded = phase7d1_fixture(root)
            plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
            semantic = _semantic(
                plan_id=plan_payload["plan_id"],
                cohort_sha256=plan_payload["cohort_sha256"],
                source_ids=seeded,
                accepted=2,
                updated=2,
            )
            semantic["ready_for_phase7d3"] = False
            semantic_path = _write(root / "semantic.json", semantic)
            with self.assertRaisesRegex(ProductionGuardedRolloutError, "checksum"):
                load_phase7d3a_evidence(
                    cohort_path=cohort,
                    quality_report_path=quality,
                    production_plan_path=plan_path,
                    semantic_closeout_path=semantic_path,
                    expected_inventory=4,
                    expected_cohort_size=3,
                    expected_seeded_sources=2,
                    expected_seeded_jobs=2,
                    expected_updated_jobs=2,
                )

    def test_semantic_insert_or_unrelated_strict_failure_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cohort, quality, plan_path, seeded = phase7d1_fixture(root)
            plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
            semantic = _semantic(
                plan_id=plan_payload["plan_id"],
                cohort_sha256=plan_payload["cohort_sha256"],
                source_ids=seeded,
                accepted=2,
                updated=2,
            )
            semantic["semantic_idempotency"]["inserted"] = 1
            semantic["strict_gate"]["blockers"].append(
                "rerun_created_duplicate_identity_hashes"
            )
            semantic.pop("report_sha256")
            semantic["report_sha256"] = _canonical(semantic)
            semantic_path = _write(root / "semantic.json", semantic)
            with self.assertRaisesRegex(
                ProductionGuardedRolloutError,
                "Semantic idempotency count|strict-gate failure",
            ):
                load_phase7d3a_evidence(
                    cohort_path=cohort,
                    quality_report_path=quality,
                    production_plan_path=plan_path,
                    semantic_closeout_path=semantic_path,
                    expected_inventory=4,
                    expected_cohort_size=3,
                    expected_seeded_sources=2,
                    expected_seeded_jobs=2,
                    expected_updated_jobs=2,
                )

    def test_complete_backfill_includes_all_sources_and_preserves_order(self) -> None:
        plan, selection, semantic = _production_inputs()
        payload = build_phase7d3a_rollout_plan(
            production_plan=plan,
            selection=selection,
            semantic_closeout=semantic,
            semantic_closeout_sha256=semantic["report_sha256"],
            generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )
        expected_backfill = plan.selected_source_ids
        self.assertEqual(payload["initial_backfill_source_ids"], expected_backfill)
        self.assertEqual(payload["initial_backfill_source_count"], 26)
        self.assertEqual(payload["initial_backfill_batch_count"], 7)
        self.assertEqual(
            [
                source_id
                for batch in payload["initial_backfill_batches"]
                for source_id in batch["source_ids"]
            ],
            expected_backfill,
        )
        self.assertEqual(
            [
                batch["source_count"]
                for batch in payload["initial_backfill_batches"]
            ],
            [4, 4, 4, 4, 4, 4, 2],
        )
        self.assertTrue(set(selection.source_ids).issubset(set(expected_backfill)))
        self.assertEqual(
            payload["catalog_completion_policy"]["mode"],
            "complete_catalog",
        )
        self.assertIsNone(
            payload["catalog_completion_policy"]["max_jobs_per_source"]
        )
        self.assertTrue(
            payload["controls"]["pilot_seeded_sources_included_in_initial_backfill"]
        )

    def test_all_26_sources_including_seeded_are_in_recurring_rescrape(self) -> None:
        plan, selection, semantic = _production_inputs()
        payload = build_phase7d3a_rollout_plan(
            production_plan=plan,
            selection=selection,
            semantic_closeout=semantic,
            semantic_closeout_sha256=semantic["report_sha256"],
            generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )
        flattened = [
            source_id
            for batch in payload["steady_state_rescrape_batches"]
            for source_id in batch["source_ids"]
        ]
        self.assertEqual(payload["steady_state_rescrape_source_count"], 26)
        self.assertEqual(payload["steady_state_rescrape_source_ids"], plan.selected_source_ids)
        self.assertEqual(flattened, plan.selected_source_ids)
        self.assertTrue(set(selection.source_ids).issubset(set(flattened)))
        self.assertEqual(
            [
                batch["source_count"]
                for batch in payload["steady_state_rescrape_batches"]
            ],
            [4, 4, 4, 4, 4, 4, 2],
        )
        self.assertEqual(payload["rescrape_policy"]["cadence_hours"], 72)
        self.assertTrue(
            payload["controls"]["all_sources_included_in_steady_state_rescrape"]
        )

    def test_batch_size_and_source_scope_are_immutable(self) -> None:
        plan, selection, semantic = _production_inputs()
        with self.assertRaisesRegex(ProductionGuardedRolloutError, "batch size"):
            build_phase7d3a_rollout_plan(
                production_plan=plan,
                selection=selection,
                semantic_closeout=semantic,
                semantic_closeout_sha256=semantic["report_sha256"],
                batch_size=5,
            )
        semantic["source_ids"] = ["not_in_cohort", selection.source_ids[1]]
        with self.assertRaisesRegex(ProductionGuardedRolloutError, "signed Phase 7D2"):
            build_phase7d3a_rollout_plan(
                production_plan=plan,
                selection=selection,
                semantic_closeout=semantic,
                semantic_closeout_sha256=semantic["report_sha256"],
            )

    def test_plan_write_is_idempotent_and_refuses_different_state(self) -> None:
        plan, selection, semantic = _production_inputs()
        first = build_phase7d3a_rollout_plan(
            production_plan=plan,
            selection=selection,
            semantic_closeout=semantic,
            semantic_closeout_sha256=semantic["report_sha256"],
            generated_at=datetime(2026, 7, 22, 1, tzinfo=timezone.utc),
        )
        second = build_phase7d3a_rollout_plan(
            production_plan=plan,
            selection=selection,
            semantic_closeout=semantic,
            semantic_closeout_sha256=semantic["report_sha256"],
            generated_at=datetime(2026, 7, 22, 2, tzinfo=timezone.utc),
        )
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "rollout.json"
            _, stored, created = write_phase7d3a_rollout_plan(target, first)
            self.assertTrue(created)
            _, same, created_again = write_phase7d3a_rollout_plan(target, second)
            self.assertFalse(created_again)
            self.assertEqual(same, stored)
            self.assertEqual(read_phase7d3a_rollout_plan(target), stored)

            different = dict(second)
            different["evidence"] = dict(different["evidence"])
            different["evidence"]["phase7d2c1_generated_from_run_id"] = "different-run"
            different.pop("plan_sha256")
            different["plan_sha256"] = _canonical(different)
            with self.assertRaisesRegex(
                ProductionGuardedRolloutError,
                "refusing to overwrite",
            ):
                write_phase7d3a_rollout_plan(target, different)

    def test_plan_checksum_tampering_is_rejected(self) -> None:
        plan, selection, semantic = _production_inputs()
        payload = build_phase7d3a_rollout_plan(
            production_plan=plan,
            selection=selection,
            semantic_closeout=semantic,
            semantic_closeout_sha256=semantic["report_sha256"],
        )
        with tempfile.TemporaryDirectory() as raw:
            target = _write(Path(raw) / "rollout.json", payload)
            stored = json.loads(target.read_text(encoding="utf-8"))
            stored["initial_backfill_source_count"] = 23
            _write(target, stored)
            with self.assertRaisesRegex(ProductionGuardedRolloutError, "checksum"):
                read_phase7d3a_rollout_plan(target)


if __name__ == "__main__":
    unittest.main()

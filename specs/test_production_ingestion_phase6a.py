from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from src.portals.production_ingestion import (
    Phase6AIngestionConfig,
    ProductionIngestionError,
    build_phase6a_ingestion_plan,
    load_phase6a_production_cohort,
    requested_source_ids_from_inputs,
)


def _canonical_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cohort_payload() -> dict:
    payload = {
        "contract_version": "1.0",
        "phase": "5.5C",
        "cohort_status": "frozen_certified_cohort",
        "frozen_at": "2026-07-17T00:00:00+00:00",
        "inventory": {"path": "portals.xlsx", "sha256": "a" * 64, "source_count": 3},
        "evidence": {},
        "cohort_source_count": 2,
        "deferred_source_count": 1,
        "cohort_source_ids": ["source_a", "source_b"],
        "deferred_source_ids": ["source_c"],
        "sources": [
            {
                "source_id": "source_a",
                "source_row": 1,
                "display_name": "Source A",
                "listing_url": "https://a.example/jobs",
                "detected_platform": "custom_listing",
                "resolved_route_url": "https://a.example/jobs/search",
                "bounded_extracted_jobs": 10,
                "evidence_run_id": "run-1",
            },
            {
                "source_id": "source_b",
                "source_row": 2,
                "display_name": "Source B",
                "listing_url": "https://b.example/jobs",
                "detected_platform": "unknown",
                "resolved_route_url": None,
                "bounded_extracted_jobs": 9,
                "evidence_run_id": "run-1",
            },
        ],
        "safety": {
            "bounded_certification_only": True,
            "full_inventory_extraction_validated": False,
            "production_ingestion_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "failed_or_partial_runs_may_deactivate_jobs": False,
            "phase_6_validation_required": True,
        },
    }
    payload["cohort_sha256"] = _canonical_sha256(payload)
    return payload


class Phase6AProductionIngestionTests(unittest.TestCase):
    def _write_cohort(self, directory: Path, payload: dict | None = None) -> Path:
        path = directory / "cohort.json"
        path.write_text(json.dumps(payload or _cohort_payload()), encoding="utf-8")
        return path

    def test_loads_complete_frozen_cohort_and_preserves_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cohort = load_phase6a_production_cohort(
                self._write_cohort(Path(raw)),
                expected_cohort_size=2,
            )
        self.assertEqual(cohort.source_ids, ["source_a", "source_b"])
        self.assertEqual(cohort.deferred_source_ids, ["source_c"])
        self.assertEqual(cohort.inventory_source_count, 3)

    def test_all_sources_are_selected_when_no_subset_is_requested(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            plan = build_phase6a_ingestion_plan(
                cohort_path=self._write_cohort(Path(raw)),
                config=Phase6AIngestionConfig(expected_cohort_size=2),
                generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
            )
        self.assertEqual(plan.selected_source_ids, ["source_a", "source_b"])
        self.assertEqual(plan.selected_source_count, 2)
        self.assertFalse(plan.controls["production_writes_enabled"])
        self.assertFalse(plan.controls["lifecycle_reconciliation_enabled"])
        self.assertFalse(plan.controls["deactivation_enabled"])

    def test_subset_is_allowed_but_returned_in_frozen_cohort_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            plan = build_phase6a_ingestion_plan(
                cohort_path=self._write_cohort(Path(raw)),
                config=Phase6AIngestionConfig(
                    expected_cohort_size=2,
                    requested_source_ids=["source_b", "source_a"],
                ),
            )
        self.assertEqual(plan.selected_source_ids, ["source_a", "source_b"])

    def test_deferred_or_unknown_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cohort_path = self._write_cohort(Path(raw))
            for source_id in ("source_c", "unknown_source"):
                with self.subTest(source_id=source_id):
                    with self.assertRaisesRegex(
                        ProductionIngestionError,
                        "SOURCE_NOT_IN_PRODUCTION_COHORT",
                    ):
                        build_phase6a_ingestion_plan(
                            cohort_path=cohort_path,
                            config=Phase6AIngestionConfig(
                                expected_cohort_size=2,
                                requested_source_ids=[source_id],
                            ),
                        )

    def test_tampered_cohort_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            payload = _cohort_payload()
            payload["cohort_source_ids"].append("source_c")
            with self.assertRaisesRegex(ProductionIngestionError, "checksum"):
                load_phase6a_production_cohort(
                    self._write_cohort(directory, payload),
                    expected_cohort_size=2,
                )

    def test_phase6a_forbids_writes_reconciliation_and_deactivation(self) -> None:
        for field in (
            "production_writes_enabled",
            "lifecycle_reconciliation_enabled",
            "deactivation_enabled",
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    Phase6AIngestionConfig(expected_cohort_size=2, **{field: True})

    def test_source_id_inputs_reject_duplicates_across_cli_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "ids.txt"
            path.write_text("source_a\nsource_b\n", encoding="utf-8")
            self.assertEqual(
                requested_source_ids_from_inputs(source_id_file=path),
                ["source_a", "source_b"],
            )
            with self.assertRaisesRegex(ProductionIngestionError, "duplicates"):
                requested_source_ids_from_inputs(
                    source_ids=["source_a"],
                    source_id_file=path,
                )


if __name__ == "__main__":
    unittest.main()

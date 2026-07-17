from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.portals.certification import PortalInventoryEntry
from src.portals.production_cohort import (
    ProductionCohortError,
    build_frozen_production_cohort,
    read_source_id_file,
    validate_frozen_production_cohort,
    write_frozen_production_cohort,
)


def _entry(index: int) -> PortalInventoryEntry:
    return PortalInventoryEntry(
        source_id=f"source_{index:03d}",
        display_name=f"Portal {index}",
        listing_url=f"https://portal-{index}.example/jobs",
        source_row=index + 1,
    )


class ProductionCohortTests(unittest.TestCase):
    def _evidence(self, directory: Path) -> tuple[Path, Path, Path, list[PortalInventoryEntry]]:
        inventory_path = directory / "portals.txt"
        inventory_path.write_text(
            "https://portal-0.example/jobs\nhttps://portal-1.example/jobs\nhttps://portal-2.example/jobs\n",
            encoding="utf-8",
        )
        inventory = [_entry(index) for index in range(3)]
        inventory_hash = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
        records = []
        summary_records = []
        for index, entry in enumerate(inventory):
            success = index < 2
            records.append(
                {
                    "source_id": entry.source_id,
                    "source_row": entry.source_row,
                    "display_name": entry.display_name,
                    "provided_url": entry.listing_url,
                    "status": "success" if success else "failed",
                    "certification_status": "passed" if success else "needs_repair",
                    "classification": "certified" if success else "zero_discovery",
                    "detected_platform": "custom_listing",
                    "resolved_route_url": entry.listing_url,
                    "extracted_jobs": 2 if success else 0,
                    "record_run_id": "run-1",
                }
            )
            summary_records.append(
                {
                    "source_id": entry.source_id,
                    "status": "success" if success else "failed",
                    "extracted_jobs": 2 if success else 0,
                }
            )
        report = {
            "campaign_id": "campaign-1",
            "inventory_count": 3,
            "inventory_source_ids": [entry.source_id for entry in inventory],
            "inventory_duplicates": 0,
            "accounted_count": 3,
            "missing_count": 0,
            "complete": True,
            "certified_source_count": 2,
            "certified_source_ids": [entry.source_id for entry in inventory[:2]],
            "bounded_certification": True,
            "production_ingestion_performed": False,
            "safe_for_lifecycle_reconciliation": False,
            "records": records,
        }
        summary = {
            "run_id": "summary-run",
            "input_sha256": inventory_hash,
            "inventory_count": 3,
            "latest_result_count": 3,
            "records": summary_records,
        }
        report_path = directory / "fleet_campaign_report.json"
        summary_path = directory / "portal_certification_summary.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        return inventory_path, report_path, summary_path, inventory

    def test_freeze_cross_checks_evidence_and_disables_dangerous_actions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            inventory_path, report_path, summary_path, inventory = self._evidence(directory)
            with patch(
                "src.portals.production_cohort.read_portal_inventory",
                return_value=inventory,
            ):
                cohort = build_frozen_production_cohort(
                    input_path=inventory_path,
                    fleet_report_path=report_path,
                    certification_summary_path=summary_path,
                    expected_inventory=3,
                    expected_certified=2,
                    frozen_at="2026-07-17T00:00:00+00:00",
                )

        self.assertEqual(cohort["cohort_source_ids"], ["source_000", "source_001"])
        self.assertEqual(cohort["deferred_source_ids"], ["source_002"])
        self.assertFalse(cohort["safety"]["production_ingestion_enabled"])
        self.assertFalse(cohort["safety"]["lifecycle_reconciliation_enabled"])
        self.assertTrue(cohort["safety"]["phase_6_validation_required"])
        self.assertEqual(len(cohort["cohort_sha256"]), 64)

    def test_freeze_rejects_report_summary_disagreement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            inventory_path, report_path, summary_path, inventory = self._evidence(directory)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["records"][1]["status"] = "failed"
            summary["records"][1]["extracted_jobs"] = 0
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            with patch(
                "src.portals.production_cohort.read_portal_inventory",
                return_value=inventory,
            ):
                with self.assertRaisesRegex(ProductionCohortError, "disagree"):
                    build_frozen_production_cohort(
                        input_path=inventory_path,
                        fleet_report_path=report_path,
                        certification_summary_path=summary_path,
                        expected_inventory=3,
                        expected_certified=2,
                    )

    def test_written_cohort_validates_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            inventory_path, report_path, summary_path, inventory = self._evidence(directory)
            with patch(
                "src.portals.production_cohort.read_portal_inventory",
                return_value=inventory,
            ):
                cohort = build_frozen_production_cohort(
                    input_path=inventory_path,
                    fleet_report_path=report_path,
                    certification_summary_path=summary_path,
                    expected_inventory=3,
                    expected_certified=2,
                    frozen_at="2026-07-17T00:00:00+00:00",
                )
                artifacts = write_frozen_production_cohort(
                    output_dir=directory / "cohort",
                    cohort=cohort,
                )
                validated = validate_frozen_production_cohort(
                    cohort_path=Path(artifacts["cohort"]),
                    input_path=inventory_path,
                    fleet_report_path=report_path,
                    certification_summary_path=summary_path,
                    expected_inventory=3,
                    expected_certified=2,
                )
            self.assertEqual(validated, cohort)

            stored = json.loads(Path(artifacts["cohort"]).read_text(encoding="utf-8"))
            stored["cohort_source_ids"].append("source_002")
            Path(artifacts["cohort"]).write_text(json.dumps(stored), encoding="utf-8")
            with self.assertRaisesRegex(ProductionCohortError, "checksum"):
                validate_frozen_production_cohort(
                    cohort_path=Path(artifacts["cohort"]),
                    input_path=inventory_path,
                    fleet_report_path=report_path,
                    certification_summary_path=summary_path,
                    expected_inventory=3,
                    expected_certified=2,
                )

    def test_source_id_file_rejects_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "ids.txt"
            path.write_text("source_001\n# comment\nsource_002\n", encoding="utf-8")
            self.assertEqual(read_source_id_file(path), ["source_001", "source_002"])
            path.write_text("source_001\nsource_001\n", encoding="utf-8")
            with self.assertRaisesRegex(ProductionCohortError, "duplicate"):
                read_source_id_file(path)


if __name__ == "__main__":
    unittest.main()

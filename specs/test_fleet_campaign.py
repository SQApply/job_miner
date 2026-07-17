from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.portals.certification import PortalInventoryEntry
from src.portals.fleet_campaign import (
    build_fleet_campaign_report,
    build_fleet_repair_manifest,
    select_campaign_source_ids,
    write_fleet_campaign_artifacts,
)


def _entry(index: int) -> PortalInventoryEntry:
    return PortalInventoryEntry(
        source_id=f"source_{index:03d}",
        display_name=f"Portal {index}",
        listing_url=f"https://portal-{index}.example/jobs",
        source_row=index + 1,
    )


def _record(
    entry: PortalInventoryEntry,
    *,
    status: str = "failed",
    certification_status: str = "needs_repair",
    discovered: int = 0,
    attempted: int = 0,
    extracted: int = 0,
    error_type: str | None = "zero_discovery",
    surface_kind: str = "content",
    route: str | None = None,
) -> dict:
    return {
        "contract_version": "1.3",
        "run_id": "test-run",
        "attempt_number": 1,
        "source_id": entry.source_id,
        "display_name": entry.display_name,
        "provided_url": entry.listing_url,
        "effective_listing_url": route or entry.listing_url,
        "status": status,
        "certification_status": certification_status,
        "detected_platform": "custom_listing",
        "surface_kind": surface_kind,
        "resolved_route_url": route,
        "discovered_urls": discovered,
        "attempted_urls": attempted,
        "extracted_jobs": extracted,
        "error_type": error_type,
        "error_message": None,
    }


class FleetCampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inventory = [_entry(index) for index in range(8)]
        self.latest = {
            self.inventory[0].source_id: _record(
                self.inventory[0],
                status="success",
                certification_status="passed",
                discovered=12,
                attempted=2,
                extracted=2,
                error_type=None,
            ),
            # A success flag with zero extracted jobs must remain a failure.
            self.inventory[1].source_id: _record(
                self.inventory[1],
                status="success",
                certification_status="passed",
                error_type=None,
            ),
            self.inventory[2].source_id: _record(
                self.inventory[2],
                status="blocked",
                certification_status="access_blocked",
                error_type="access_blocked",
                surface_kind="confirmed_access_control",
            ),
            self.inventory[3].source_id: _record(
                self.inventory[3],
                error_type="javascript_shell",
                surface_kind="javascript_shell",
            ),
            self.inventory[4].source_id: _record(
                self.inventory[4],
                error_type="network_error",
            ),
            self.inventory[5].source_id: _record(
                self.inventory[5],
                discovered=5,
                attempted=2,
                error_type="zero_valid_jobs",
            ),
            self.inventory[6].source_id: _record(
                self.inventory[6],
                route="https://jobs.portal-6.example/openings",
            ),
            # inventory[7] intentionally has no record.
        }

    def report(self) -> dict:
        return build_fleet_campaign_report(
            inventory=self.inventory,
            latest_records=self.latest,
            campaign_id="campaign-test",
            target_successes=2,
            generated_at="2026-07-17T00:00:00+00:00",
        )

    def test_campaign_accounts_for_every_inventory_source_truthfully(self) -> None:
        report = self.report()

        self.assertEqual(report["inventory_count"], 8)
        self.assertEqual(report["accounted_count"], 7)
        self.assertEqual(report["missing_count"], 1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["certified_source_count"], 1)
        self.assertFalse(report["target_met"])
        self.assertEqual(report["blocked_source_count"], 1)
        self.assertEqual(report["gpu_eligible_source_ids"], ["source_005"])
        self.assertFalse(report["production_ingestion_performed"])
        self.assertFalse(report["safe_for_lifecycle_reconciliation"])

    def test_false_success_blocked_and_route_failures_are_separate(self) -> None:
        report = self.report()
        classifications = {
            item["source_id"]: item["classification"]
            for item in report["records"]
        }

        self.assertEqual(classifications["source_000"], "certified")
        self.assertEqual(classifications["source_001"], "zero_discovery")
        self.assertEqual(classifications["source_002"], "protected_access_control")
        self.assertEqual(classifications["source_003"], "javascript_application")
        self.assertEqual(classifications["source_004"], "transient_network_failure")
        self.assertEqual(classifications["source_005"], "detail_extraction_failure")
        self.assertEqual(
            classifications["source_006"],
            "route_resolved_zero_discovery",
        )
        self.assertEqual(classifications["source_007"], "not_executed")

    def test_retry_manifest_excludes_successes_and_protected_sources(self) -> None:
        report = self.report()
        manifest = build_fleet_repair_manifest(report)

        self.assertNotIn("source_000", manifest["retry_source_ids"])
        self.assertNotIn("source_002", manifest["retry_source_ids"])
        self.assertIn("source_007", manifest["retry_source_ids"])
        self.assertEqual(manifest["blocked_source_ids"], ["source_002"])
        self.assertEqual(manifest["gpu_eligible_source_ids"], ["source_005"])

    def test_campaign_modes_are_deterministic(self) -> None:
        report = self.report()

        self.assertEqual(len(select_campaign_source_ids(report, mode="full")), 8)
        self.assertEqual(
            select_campaign_source_ids(report, mode="resume"),
            ["source_007"],
        )
        self.assertEqual(
            select_campaign_source_ids(report, mode="analyze-only"),
            [],
        )
        retry = select_campaign_source_ids(report, mode="retry_actionable")
        self.assertNotIn("source_000", retry)
        self.assertNotIn("source_002", retry)
        self.assertIn("source_005", retry)
        self.assertEqual(
            select_campaign_source_ids(report, mode="retry-gpu-eligible"),
            ["source_005"],
        )

    def test_campaign_artifacts_are_atomic_and_machine_readable(self) -> None:
        report = self.report()
        with tempfile.TemporaryDirectory() as directory:
            paths = write_fleet_campaign_artifacts(
                output_dir=Path(directory),
                report=report,
            )
            stored = json.loads(Path(paths["report"]).read_text(encoding="utf-8"))
            retry_ids = Path(paths["retry_source_ids"]).read_text(encoding="utf-8")
            blocked_ids = Path(paths["blocked_source_ids"]).read_text(encoding="utf-8")
            csv_lines = Path(paths["csv"]).read_text(encoding="utf-8").splitlines()

        self.assertEqual(stored["campaign_id"], "campaign-test")
        self.assertIn("source_005", retry_ids)
        self.assertNotIn("source_002", retry_ids)
        self.assertEqual(blocked_ids.strip(), "source_002")
        self.assertEqual(len(csv_lines), 9)

    def test_target_is_met_only_after_complete_accounting(self) -> None:
        inventory = [_entry(20), _entry(21)]
        latest = {
            entry.source_id: _record(
                entry,
                status="success",
                certification_status="passed",
                discovered=1,
                attempted=1,
                extracted=1,
                error_type=None,
            )
            for entry in inventory
        }
        report = build_fleet_campaign_report(
            inventory=inventory,
            latest_records=latest,
            campaign_id="complete",
            target_successes=2,
        )

        self.assertTrue(report["complete"])
        self.assertTrue(report["target_met"])

    def test_unclassified_runtime_errors_remain_visible(self) -> None:
        entry = _entry(30)
        record = _record(entry, error_type="ValueError")
        report = build_fleet_campaign_report(
            inventory=[entry],
            latest_records={entry.source_id: record},
            campaign_id="unknown-error",
            target_successes=1,
        )

        self.assertEqual(
            report["records"][0]["classification"],
            "unknown_failure",
        )
        self.assertEqual(
            report["records"][0]["next_action"],
            "collect_surface_diagnostic",
        )


if __name__ == "__main__":
    unittest.main()

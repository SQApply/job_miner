from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from src.blueprint_hub import BlueprintHub
from src.portals.acquisition import AcquisitionRegistry
from src.portals.certification import (
    CertificationOptions,
    CertificationReportStore,
    PortalCertificationRecord,
    PortalFleetCertifier,
    certify_portal_inventory,
    read_portal_inventory,
)
from src.portals.detector import detect_portal
from src.portals.orchestrator import ScrapeExecutionOptions, ScrapeOrchestrator


ROOT = Path(__file__).resolve().parents[1]


class FakeJsonClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request_json(self, url, *, method="GET", payload=None, timeout_seconds=20.0):
        self.calls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FailAdapter:
    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None):
        raise AssertionError("Browser adapter must not run for a complete pre-extracted feed")


def _record(source_id: str, *, status: str, attempt: int = 1) -> PortalCertificationRecord:
    return PortalCertificationRecord(
        contract_version="1.0",
        run_id="test-run",
        attempt_number=attempt,
        source_id=source_id,
        display_name=source_id,
        provided_url=f"https://{source_id}.example/jobs",
        effective_listing_url=f"https://{source_id}.example/jobs",
        status=status,
        certification_status="passed" if status == "success" else "needs_repair",
        stage="complete",
        detected_platform="custom_listing",
        detected_profile="generic_listing",
        discovered_urls=1 if status == "success" else 0,
        attempted_urls=1 if status == "success" else 0,
        extracted_jobs=1 if status == "success" else 0,
        sample_jobs=[{"title": "Test Engineer", "job_url": "https://example.com/job/1"}]
        if status == "success"
        else [],
        acquisition={"selected": False},
        detail_failures=[],
        rejected_urls=0,
        event_counts={},
        gpu_before={},
        gpu_after={},
        started_at="2026-07-16T00:00:00+00:00",
        completed_at="2026-07-16T00:00:01+00:00",
        elapsed_seconds=1.0,
        error_type=None if status == "success" else "zero_discovery",
        error_message=None if status == "success" else "No jobs found",
    )


def _write_minimal_xlsx(path: Path) -> None:
    workbook = """<?xml version="1.0" encoding="UTF-8"?>
    <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets><sheet name="Portals" sheetId="1" r:id="rId1"/></sheets>
    </workbook>"""
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Target="worksheets/sheet1.xml"
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
    </Relationships>"""
    shared = """<?xml version="1.0" encoding="UTF-8"?>
    <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="3" uniqueCount="3">
      <si><t>Portal URL</t></si>
      <si><t>https://boards.greenhouse.io/acme</t></si>
      <si><t>https://jobs.lever.co/sample</t></si>
    </sst>"""
    sheet = """<?xml version="1.0" encoding="UTF-8"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData>
        <row r="1"><c r="A1" t="s"><v>0</v></c></row>
        <row r="2"><c r="A2" t="s"><v>1</v></c></row>
        <row r="3"><c r="A3" t="s"><v>2</v></c></row>
      </sheetData>
    </worksheet>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


class PortalInventoryTests(unittest.TestCase):
    def test_csv_inventory_is_deduplicated_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portals.csv"
            path.write_text(
                "name,url\nAcme,https://careers.example.com/jobs\nDuplicate,https://careers.example.com/jobs\n",
                encoding="utf-8",
            )
            first = read_portal_inventory(path)
            second = read_portal_inventory(path)

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].display_name, "Acme")
        self.assertEqual(first[0].source_id, second[0].source_id)
        self.assertTrue(first[0].source_id.startswith("cert_careers_example_com_"))

    def test_xlsx_inventory_uses_standard_library_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portals.xlsx"
            _write_minimal_xlsx(path)
            entries = read_portal_inventory(path)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].listing_url, "https://boards.greenhouse.io/acme")
        self.assertEqual(entries[1].listing_url, "https://jobs.lever.co/sample")

    def test_certification_limits_protect_local_gpu(self) -> None:
        self.assertEqual(CertificationOptions().max_jobs, 10)
        self.assertEqual(CertificationOptions().detail_concurrency, 1)
        with self.assertRaisesRegex(ValueError, "between 1 and 10"):
            CertificationOptions(max_jobs=11)
        with self.assertRaisesRegex(ValueError, "must be 1 or 2"):
            CertificationOptions(detail_concurrency=3)

    def test_detector_finds_browser_only_ats_links(self) -> None:
        workable = detect_portal(
            listing_url="https://company.example/careers",
            html='<a href="https://apply.workable.com/acme/">Open jobs</a>',
        )
        self.assertEqual(workable.source_platform, "workable")
        self.assertEqual(workable.acquisition_hints["listing_url"], "https://apply.workable.com/acme/")

        smart = detect_portal(
            listing_url="https://jobs.smartrecruiters.com/Acme",
            html="",
        )
        self.assertEqual(smart.source_platform, "smartrecruiters")
        self.assertEqual(smart.profile_name, "generic_listing")


class PortalCertificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_orchestrator_run_does_not_construct_browser_for_complete_api_feed(self) -> None:
        hub = BlueprintHub(ROOT)
        blueprint = hub.get_fleet_targets()[0]
        listing = blueprint.listing.model_copy(update={"page_url": "https://boards.greenhouse.io/acme"})
        blueprint = blueprint.model_copy(update={"listing": listing})
        registry = AcquisitionRegistry(
            client=FakeJsonClient(
                [
                    {
                        "jobs": [
                            {
                                "id": 1,
                                "title": "No Browser Engineer",
                                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                                "content": "API supplied content",
                            }
                        ]
                    }
                ]
            )
        )
        orchestrator = ScrapeOrchestrator(
            blueprint=blueprint,
            system_config=hub.system,
            run_session_id="phase4-no-browser",
            adapter=FailAdapter(),
            acquisition_registry=registry,
        )

        with patch("src.portals.orchestrator.build_browser_config") as browser_config:
            result = await orchestrator.run(options=ScrapeExecutionOptions(max_jobs=1))

        browser_config.assert_not_called()
        self.assertEqual([job.title for job in result.jobs], ["No Browser Engineer"])
        self.assertTrue(result.acquisition["selected"])

    async def test_report_checkpoint_and_only_failed_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "portals.csv"
            input_path.write_text(
                "name,url\nGood,https://good.example/jobs\nBad,https://bad.example/jobs\n",
                encoding="utf-8",
            )
            output_dir = root / "reports"

            async def first_certify(self, entry, *, run_id, attempt_number):
                return _record(
                    entry.source_id,
                    status="success" if "good.example" in entry.listing_url else "failed",
                    attempt=attempt_number,
                )

            with patch.object(PortalFleetCertifier, "certify", new=first_certify):
                first = await certify_portal_inventory(
                    input_path=input_path,
                    root=ROOT,
                    output_dir=output_dir,
                    options=CertificationOptions(),
                )
            self.assertEqual(len(first), 2)
            self.assertTrue((output_dir / "portal_certification.jsonl").exists())
            self.assertTrue((output_dir / "jobs" / f"{first[0].source_id}.json").exists())

            async def repaired_certify(self, entry, *, run_id, attempt_number):
                return _record(entry.source_id, status="success", attempt=attempt_number)

            with patch.object(PortalFleetCertifier, "certify", new=repaired_certify):
                repaired = await certify_portal_inventory(
                    input_path=input_path,
                    root=ROOT,
                    output_dir=output_dir,
                    options=CertificationOptions(),
                    only_failed=True,
                )

            self.assertEqual(len(repaired), 1)
            self.assertIn("bad_example", repaired[0].source_id)
            self.assertEqual(repaired[0].attempt_number, 2)
            summary = json.loads((output_dir / "portal_certification_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status_counts"], {"success": 2})

    def test_certification_module_has_no_catalog_or_lifecycle_writes(self) -> None:
        source = (ROOT / "src" / "portals" / "certification.py").read_text(encoding="utf-8")
        self.assertNotIn("get_mongo_database", source)
        self.assertNotIn("reconcile_missing_jobs_after_discovery", source)
        self.assertNotIn("upsert_job(", source)


if __name__ == "__main__":
    unittest.main()

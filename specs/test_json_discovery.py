from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.blueprint_hub import BlueprintHub
from src.crawl.browser_evidence import BrowserEvidenceReport, NetworkJsonEvidence
from src.crawl.dom_snapshot import FrameDomSnapshot, InlineJsonEvidence
from src.portals.adaptive_dom import AdaptiveDomDiscoveryService
from src.portals.contracts import DiscoveryCandidateKind
from src.portals.dom_discovery import preserve_evidence_backed_urls
from src.portals.json_discovery import JsonCandidateDiscoverer
from src.portals.orchestrator import (
    ScrapeExecutionOptions,
    ScrapeOrchestrator,
    ScrapeOrchestratorHooks,
)
from src.schemas import BrowserSettings


def evidence_report(
    *,
    network_payload=None,
    inline_payload=None,
) -> BrowserEvidenceReport:
    now = datetime.now(timezone.utc).isoformat()
    network_json = []
    if network_payload is not None:
        network_json.append(
            NetworkJsonEvidence(
                response_id="r0001",
                url="https://careers.example.com/api/graphql",
                status=200,
                resource_type="xhr",
                content_type="application/json",
                payload=network_payload,
            )
        )
    inline_json = []
    if inline_payload is not None:
        inline_json.append(
            InlineJsonEvidence(
                script_id="__NEXT_DATA__",
                script_type="application/json",
                text_length=100,
                sample_sha256="a" * 64,
                payload=inline_payload,
            )
        )
    return BrowserEvidenceReport(
        requested_url="https://careers.example.com/careers",
        final_url="https://careers.example.com/careers",
        success=True,
        status_code=200,
        title="Careers",
        started_at=now,
        completed_at=now,
        frames=[
            FrameDomSnapshot(
                frame_id="f0",
                frame_url="https://careers.example.com/careers",
                inline_json=inline_json,
            )
        ],
        network_json=network_json,
    )


class JsonCandidateDiscoveryTests(unittest.TestCase):
    def test_recursive_graphql_records_preserve_unfamiliar_relative_urls(self) -> None:
        report = evidence_report(
            network_payload={
                "data": {
                    "jobs": {
                        "edges": [
                            {
                                "node": {
                                    "positionTitle": "Distributed Systems Engineer",
                                    "detailUri": "/opening?opaque=alpha-7",
                                    "requisitionNumber": "REQ-7",
                                    "location": {"name": "Remote - US"},
                                    "shortDescription": "Build reliable distributed services.",
                                }
                            }
                        ]
                    }
                }
            }
        )

        batch = JsonCandidateDiscoverer().discover(report)

        self.assertEqual(len(batch.candidates), 1)
        candidate = batch.candidates[0]
        self.assertEqual(candidate.kind, DiscoveryCandidateKind.API_RECORD)
        self.assertEqual(
            candidate.detail_url,
            "https://careers.example.com/opening?opaque=alpha-7",
        )
        self.assertEqual(candidate.source_job_id, "REQ-7")
        self.assertEqual(candidate.preextracted_job.location_text, "Remote - US")
        self.assertTrue(candidate.evidence["structured_job_record"])
        self.assertTrue(candidate.evidence["evidence_preserving"])

        urls, metrics = preserve_evidence_backed_urls([], batch.candidates)
        self.assertEqual(urls, [candidate.detail_url])
        self.assertEqual(metrics["adaptive_urls_preserved"], 1)

    def test_workday_style_external_path_is_discovered_without_vendor_schema(self) -> None:
        report = evidence_report(
            network_payload={
                "jobPostings": [
                    {
                        "title": "Machine Learning Engineer",
                        "externalPath": "/job/Austin/Machine-Learning-Engineer_JR123",
                        "jobId": "JR123",
                        "locationsText": "Austin, TX",
                        "postedOn": "Posted Today",
                    }
                ]
            }
        )

        candidate = JsonCandidateDiscoverer().discover(report).candidates[0]

        self.assertEqual(
            candidate.detail_url,
            "https://careers.example.com/job/Austin/Machine-Learning-Engineer_JR123",
        )
        self.assertEqual(candidate.preextracted_job.posted_date, "Posted Today")

    def test_inline_schema_org_job_is_preextracted(self) -> None:
        report = evidence_report(
            inline_payload={
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Data Platform Lead",
                "url": "/roles/data-platform-lead",
                "description": "Lead the data platform team and build reliable analytics systems.",
                "identifier": {"value": "DP-44"},
                "hiringOrganization": {"name": "Example Labs"},
                "jobLocation": {
                    "address": {
                        "addressLocality": "Chicago",
                        "addressRegion": "IL",
                    }
                },
            }
        )

        batch = JsonCandidateDiscoverer().discover(report)

        self.assertEqual(len(batch.candidates), 1)
        job = batch.candidates[0].preextracted_job
        self.assertEqual(job.company, "Example Labs")
        self.assertIn("Chicago", job.location_text)
        self.assertIn("Lead the data platform", job.summary)

    def test_generic_marketing_name_url_cards_are_rejected(self) -> None:
        report = evidence_report(
            network_payload={
                "cards": [
                    {
                        "name": "Salary Guide",
                        "url": "/employers/salary-guide",
                        "description": "Download our annual report.",
                    },
                    {
                        "name": "Contact Us",
                        "url": "/contact",
                        "description": "Talk to a recruiter.",
                    },
                ]
            }
        )

        batch = JsonCandidateDiscoverer().discover(report)

        self.assertEqual(batch.candidates, [])
        self.assertGreaterEqual(batch.metrics["records_rejected"], 1)


class FakeCollector:
    def __init__(self, report: BrowserEvidenceReport) -> None:
        self.report = report

    async def capture(self, url: str, *, allowed_hosts):
        return self.report


class AdaptiveStructuredEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_adaptive_service_consumes_captured_json_when_dom_has_no_links(self) -> None:
        report = evidence_report(
            network_payload={
                "jobs": [
                    {
                        "jobTitle": "Site Reliability Engineer",
                        "jobUrl": "/opaque/detail?id=88",
                        "jobId": "88",
                        "locationText": "Denver, CO",
                    }
                ]
            }
        )
        report = report.model_copy(
            update={
                "success": False,
                "errors": ["navigation_failed: rendered shell timed out after JSON arrived"],
            }
        )
        service = AdaptiveDomDiscoveryService(
            BrowserSettings(),
            collector=FakeCollector(report),
        )

        batch = await service.discover(
            report.final_url,
            allowed_hosts=("careers.example.com",),
            max_candidates=10,
        )

        self.assertEqual(len(batch.candidates), 1)
        self.assertEqual(batch.candidates[0].kind, DiscoveryCandidateKind.API_RECORD)
        self.assertEqual(batch.metrics["structured_candidates"], 1)
        self.assertEqual(batch.metrics["browser_evidence"]["network_json"], 1)

    async def test_orchestrator_preserves_and_uses_grounded_json_preextraction(self) -> None:
        report = evidence_report(
            network_payload={
                "jobs": [
                    {
                        "jobTitle": "GPU Platform Engineer",
                        "jobUrl": "/opening?opaque=gpu-19",
                        "jobId": "GPU-19",
                        "locationText": "Remote",
                    }
                ]
            }
        )
        structured_batch = JsonCandidateDiscoverer().discover(report)

        class EmptyAdapter:
            async def discover_candidates(self, *args, **kwargs):
                from src.portals.contracts import DiscoveryBatch, ScrapeStrategy

                return DiscoveryBatch(strategy=ScrapeStrategy.BLUEPRINT_DOM)

        class FakeAdaptiveService:
            async def discover(self, *args, **kwargs):
                return structured_batch

        class NeverCrawler:
            def __init__(self):
                self.calls = 0

            async def arun(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("grounded structured job must skip detail browser")

        root = Path(__file__).resolve().parents[1]
        hub = BlueprintHub(root)
        crawler = NeverCrawler()
        orchestrator = ScrapeOrchestrator(
            blueprint=hub.get_fleet_targets()[0],
            system_config=hub.system,
            run_session_id="phase7c2-json-preextracted",
            adapter=EmptyAdapter(),
            adaptive_dom_service=FakeAdaptiveService(),
        )
        result = await orchestrator.run_with_crawler(
            crawler,
            options=ScrapeExecutionOptions(
                prefer_platform_api=False,
                enable_adaptive_dom_fallback=True,
                max_jobs=1,
            ),
            hooks=ScrapeOrchestratorHooks(
                rank_discovered_urls=lambda urls: (
                    [],
                    {
                        "strategy": "reject_unknown_shape_fixture",
                        "rejected_candidates": [],
                    },
                )
            ),
        )

        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0].job_reference, "GPU-19")
        self.assertEqual(crawler.calls, 0)
        self.assertEqual(result.acquisition["adaptive_dom"]["adaptive_urls_preserved"], 1)


if __name__ == "__main__":
    unittest.main()

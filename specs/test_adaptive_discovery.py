from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.blueprint_hub import BlueprintHub
from src.portals.acquisition import (
    AcquisitionContext,
    AcquisitionOutcome,
    AcquisitionRegistry,
    AcquisitionResult,
)
from src.portals.certification import (
    CertificationOptions,
    PortalFleetCertifier,
    PortalInventoryEntry,
    _ProbeResult,
)
from src.portals.detector import detect_portal
from src.portals.orchestrator import (
    OrchestratedScrapeResult,
    ScrapeExecutionOptions,
    ScrapeOrchestrator,
    ScrapeOrchestratorHooks,
)
from src.portals.url_intelligence import (
    assess_certification_job,
    assess_llm_eligibility,
    canonicalize_candidate_url,
    rank_job_candidate_urls,
)
from src.schemas import JobPosting


ROOT = Path(__file__).resolve().parents[1]


class FakeHttpClient:
    def __init__(self, documents):
        self.documents = list(documents)
        self.calls: list[str] = []

    async def request_json(self, url, *, method="GET", payload=None, timeout_seconds=20.0):
        raise AssertionError("iCIMS acquisition must not request JSON")

    async def request_text(self, url, *, timeout_seconds=20.0):
        self.calls.append(url)
        if not self.documents:
            raise AssertionError("No fake iCIMS document remains")
        return self.documents.pop(0)


class FakeAdapter:
    def __init__(self, urls):
        self.urls = list(urls)

    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None):
        return list(self.urls)


class FakeCrawler:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, str]] = []

    async def arun(self, *, url, config):
        self.calls.append((url, config.kind))
        return self.handler(url, config)


def fake_detail_config(settings, wait_for, extraction_strategy=None, session_id=None):
    return SimpleNamespace(kind="detail", session_id=session_id)


class AdaptiveUrlIntelligenceTests(unittest.TestCase):
    def test_tracking_variants_and_navigation_are_removed_before_gpu_work(self) -> None:
        listing = "https://www.judge.com/jobs/"
        ranked, metrics = rank_job_candidate_urls(
            [
                "https://www.judge.com/jobs/?_ga=tracking",
                "https://www.judge.com/resources",
                "https://www.judge.com/about-judge/job-seekers",
                "https://www.judge.com/jobs/details/1142387",
                "https://www.judge.com/jobs/details/1142369?utm_source=test",
            ],
            listing_url=listing,
            platform_hint="custom_listing",
        )

        self.assertEqual(
            ranked,
            [
                "https://www.judge.com/jobs/details/1142369",
                "https://www.judge.com/jobs/details/1142387",
            ],
        )
        self.assertEqual(metrics["selected_urls"], 2)
        self.assertEqual(metrics["high_confidence_urls"], 2)
        self.assertEqual(metrics["rejected_urls"], 3)
        self.assertFalse(metrics["fallback_preserved"])

    def test_spa_job_hash_identity_is_preserved(self) -> None:
        value = "https://careerportal.compri.com/?utm_source=test#/jobs/12345"
        self.assertEqual(
            canonicalize_candidate_url(value),
            "https://careerportal.compri.com/#/jobs/12345",
        )

    def test_navigation_words_inside_a_real_job_slug_are_not_rejected(self) -> None:
        ranked, metrics = rank_job_candidate_urls(
            ["https://careers-acme.icims.com/jobs/1001/privacy-engineer/job"],
            listing_url="https://careers-acme.icims.com/jobs",
            platform_hint="icims",
        )
        self.assertEqual(
            ranked,
            ["https://careers-acme.icims.com/jobs/1001/privacy-engineer/job"],
        )
        self.assertEqual(metrics["high_confidence_urls"], 1)

    def test_llm_gate_and_certification_validator_reject_navigation_pages(self) -> None:
        allowed, reason = assess_llm_eligibility(
            SimpleNamespace(html="<html><h1>Resources</h1><p>Read our latest news.</p></html>"),
            "https://www.judge.com/resources",
        )
        self.assertFalse(allowed)
        self.assertIn("navigation", reason)

        valid, reason = assess_certification_job(
            JobPosting(title="Resources", job_url="https://www.judge.com/resources"),
            "https://www.judge.com/resources",
        )
        self.assertFalse(valid)
        self.assertIn("navigation", reason)

        valid, reason = assess_certification_job(
            JobPosting(
                title="Senior Data Engineer",
                job_url="https://www.judge.com/jobs/details/1142387",
            ),
            "https://www.judge.com/jobs/details/1142387",
        )
        self.assertTrue(valid)
        self.assertIn("quality_score", reason)


class ICIMSAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_icims_public_search_pages_are_discovered_without_browser_or_llm(self) -> None:
        first = """
        <html><body>
          <div>Showing 1 - 2 of 3 jobs</div>
          <a href="/jobs/1001/data-engineer/job">Data Engineer</a>
          <a href="https://careers-acme.icims.com/jobs/1002/platform-engineer/job?mode=job">Platform</a>
          <a href="https://attacker.example/jobs/9999/unsafe/job">Unsafe</a>
        </body></html>
        """
        second = """
        <html><body>
          <div>Showing 3 - 3 of 3 jobs</div>
          <a href="/jobs/1003/security-engineer/job">Security Engineer</a>
        </body></html>
        """
        client = FakeHttpClient([first, second])
        outcome = await AcquisitionRegistry(client=client).acquire(
            AcquisitionContext(
                listing_url="https://careers-acme.icims.com/jobs",
                max_pages=3,
                require_complete=True,
            )
        )

        self.assertIsNotNone(outcome.selected)
        selected = outcome.selected
        assert selected is not None
        self.assertEqual(selected.platform, "icims")
        self.assertEqual(selected.strategy, "platform_html_discovery")
        self.assertEqual(len(selected.discovered_urls), 3)
        self.assertTrue(selected.complete)
        self.assertEqual(selected.preextracted_jobs, {})
        self.assertEqual(selected.metadata["reported_total"], 3)
        self.assertIn("pr=0", client.calls[0])
        self.assertIn("pr=1", client.calls[1])
        self.assertNotIn("attacker.example", " ".join(selected.discovered_urls))

    async def test_bounded_icims_result_is_not_reconciliation_safe(self) -> None:
        document = """
        <div>Showing 1 - 2 of 10 jobs</div>
        <a href="/jobs/1001/data-engineer/job">Data Engineer</a>
        <a href="/jobs/1002/platform-engineer/job">Platform Engineer</a>
        """
        bounded = await AcquisitionRegistry(client=FakeHttpClient([document])).acquire(
            AcquisitionContext(
                listing_url="https://careers-acme.icims.com/jobs",
                max_pages=1,
                require_complete=False,
            )
        )
        assert bounded.selected is not None
        self.assertFalse(bounded.selected.complete)
        self.assertFalse(bounded.metrics()["reconciliation_safe"])

        production = await AcquisitionRegistry(client=FakeHttpClient([document])).acquire(
            AcquisitionContext(
                listing_url="https://careers-acme.icims.com/jobs",
                max_pages=1,
                require_complete=True,
            )
        )
        self.assertIsNone(production.selected)
        self.assertEqual(production.attempts[0]["status"], "incomplete")


class AdaptiveOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        hub = BlueprintHub(ROOT)
        self.system = hub.system
        self.blueprint = hub.get_fleet_targets()[0]

    async def test_url_ranking_runs_before_ten_job_limit(self) -> None:
        listing = "https://www.judge.com/jobs/"
        listing_config = self.blueprint.listing.model_copy(update={"page_url": listing})
        blueprint = self.blueprint.model_copy(update={"listing": listing_config})
        real_url = "https://www.judge.com/jobs/details/1142387"
        adapter = FakeAdapter(
            [
                "https://www.judge.com/resources",
                "https://www.judge.com/about-judge/job-seekers",
                real_url,
            ]
        )
        job = JobPosting(title="Senior Data Engineer", job_url=real_url)
        crawler = FakeCrawler(
            lambda url, config: SimpleNamespace(
                success=True,
                url=url,
                html='<script type="application/ld+json">{"@type":"JobPosting"}</script>',
                deterministic_job=job,
                error_message=None,
            )
        )
        orchestrator = ScrapeOrchestrator(
            blueprint=blueprint,
            system_config=self.system,
            run_session_id="phase5-rank",
            adapter=adapter,
        )

        with patch("src.portals.orchestrator.detail_run_config", side_effect=fake_detail_config), patch(
            "src.portals.orchestrator.extract_job_from_result",
            side_effect=lambda result, url: result.deterministic_job,
        ), patch("src.portals.orchestrator.close_session", new_callable=AsyncMock):
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(max_jobs=1),
                hooks=ScrapeOrchestratorHooks(
                    rank_discovered_urls=lambda urls: rank_job_candidate_urls(
                        urls,
                        listing_url=listing,
                        platform_hint="custom_listing",
                    )
                ),
            )

        self.assertEqual(result.attempted_job_urls, [real_url])
        self.assertEqual(crawler.calls, [(real_url, "detail")])
        self.assertEqual(result.jobs, [job])
        self.assertEqual(result.rescrape_plan["url_ranking"]["rejected_urls"], 2)

    async def test_canonicalized_api_url_keeps_preextracted_job_and_skips_browser(self) -> None:
        raw_url = "https://job-boards.greenhouse.io/acme/jobs/101?utm_source=test"
        canonical_url = "https://job-boards.greenhouse.io/acme/jobs/101"
        job = JobPosting(title="API Engineer", job_url=raw_url)
        outcome = AcquisitionOutcome(
            selected=AcquisitionResult(
                platform="greenhouse",
                strategy="platform_api",
                discovered_urls=[raw_url],
                preextracted_jobs={raw_url: job},
                trusted_hosts=("job-boards.greenhouse.io",),
                complete=True,
                pages_visited=1,
                endpoint_requests=1,
            ),
            attempts=[{"platform": "greenhouse", "status": "selected"}],
        )
        orchestrator = ScrapeOrchestrator(
            blueprint=self.blueprint,
            system_config=self.system,
            run_session_id="phase5-api-canonical",
            adapter=FakeAdapter([]),
        )

        result = await orchestrator.run(
            options=ScrapeExecutionOptions(max_jobs=1),
            hooks=ScrapeOrchestratorHooks(
                normalize_acquired_urls=lambda urls, trusted: (
                    list(dict.fromkeys(canonicalize_candidate_url(url) for url in urls)),
                    0,
                )
            ),
            acquisition_outcome=outcome,
        )

        self.assertEqual(result.attempted_job_urls, [canonical_url])
        self.assertEqual(result.jobs[0].job_url, canonical_url)

    async def test_non_job_page_does_not_invoke_gpu_llm_or_retry(self) -> None:
        url = "https://example.com/resources"
        crawler = FakeCrawler(
            lambda request_url, config: SimpleNamespace(
                success=True,
                url=request_url,
                html="<html><h1>Resources</h1><p>Company news and events.</p></html>",
                error_message=None,
            )
        )
        orchestrator = ScrapeOrchestrator(
            blueprint=self.blueprint,
            system_config=self.system,
            run_session_id="phase5-llm-gate",
            adapter=FakeAdapter([url]),
        )
        events: list[str] = []

        with patch("src.portals.orchestrator.detail_run_config", side_effect=fake_detail_config), patch(
            "src.portals.orchestrator.extract_job_from_result",
            return_value=None,
        ), patch("src.portals.orchestrator.build_llm_strategy") as llm_builder, patch(
            "src.portals.orchestrator.close_session",
            new_callable=AsyncMock,
        ):
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(detail_retry_attempts=2),
                hooks=ScrapeOrchestratorHooks(
                    should_attempt_llm=assess_llm_eligibility,
                    on_event=lambda event, payload: events.append(event),
                ),
            )

        llm_builder.assert_not_called()
        self.assertEqual(crawler.calls, [(url, "detail")])
        self.assertEqual(result.jobs, [])
        self.assertEqual(result.detail_failures[0]["attempts"], 1)
        self.assertIn("LLM skipped", result.detail_failures[0]["error"])
        self.assertIn("llm_fallback_skipped_non_job", events)


class CertificationFailureTaxonomyTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_extraction_has_actionable_error_and_ranking_metrics(self) -> None:
        certifier = PortalFleetCertifier(
            root=ROOT,
            output_dir=ROOT / "data" / "test-certification",
            options=CertificationOptions(max_jobs=1),
        )
        detection = detect_portal(
            listing_url="https://careers.example.com/jobs",
            html="",
        )
        probe = _ProbeResult(
            effective_listing_url="https://careers.example.com/jobs",
            detection=detection,
            allowed_hosts=("careers.example.com",),
            acquisition_outcome=AcquisitionOutcome(selected=None, attempts=[]),
        )
        orchestration = OrchestratedScrapeResult(
            discovered_job_urls=["https://careers.example.com/jobs/123"],
            attempted_job_urls=["https://careers.example.com/jobs/123"],
            jobs=[],
            rejected_urls=0,
            detail_failures=[{"job_url": "https://careers.example.com/jobs/123", "attempts": 1}],
            artifacts=[],
            rescrape_plan={
                "url_ranking": {
                    "selected_urls": 1,
                    "rejected_urls": 4,
                }
            },
            elapsed_seconds=1.0,
        )
        entry = PortalInventoryEntry(
            source_id="cert_careers_example_com_test",
            display_name="Example",
            listing_url="https://careers.example.com/jobs",
            source_row=1,
        )

        with patch.object(certifier, "_probe", new=AsyncMock(return_value=probe)), patch(
            "src.portals.certification.ScrapeOrchestrator.run",
            new=AsyncMock(return_value=orchestration),
        ):
            record = await certifier.certify(entry, run_id="phase5-test", attempt_number=1)

        self.assertEqual(record.status, "failed")
        self.assertEqual(record.error_type, "zero_valid_jobs")
        self.assertIn("no certifiable jobs", record.error_message or "")
        self.assertEqual(record.ranked_candidates, 1)
        self.assertEqual(record.ranking_rejected_urls, 4)

    async def test_zero_discovery_retains_adaptive_diagnostics_without_reconciliation(self) -> None:
        certifier = PortalFleetCertifier(
            root=ROOT,
            output_dir=ROOT / "data" / "test-certification-zero",
            options=CertificationOptions(max_jobs=1),
        )
        detection = detect_portal(
            listing_url="https://careers.example.com/jobs",
            html="",
        )
        probe = _ProbeResult(
            effective_listing_url="https://careers.example.com/jobs",
            detection=detection,
            allowed_hosts=("careers.example.com",),
            acquisition_outcome=AcquisitionOutcome(selected=None, attempts=[]),
        )
        orchestration = OrchestratedScrapeResult(
            discovered_job_urls=[],
            attempted_job_urls=[],
            jobs=[],
            rejected_urls=0,
            detail_failures=[],
            artifacts=[],
            rescrape_plan={},
            elapsed_seconds=1.0,
            acquisition={
                "adaptive_dom": {
                    "attempted": True,
                    "discovery": {
                        "structured_discovery": {"network_documents": 3},
                        "linkless_interaction": {"attempted": 0, "resolved": 0},
                    },
                }
            },
        )
        entry = PortalInventoryEntry(
            source_id="cert_careers_example_com_zero",
            display_name="Example Zero",
            listing_url="https://careers.example.com/jobs",
            source_row=1,
        )
        run_mock = AsyncMock(return_value=orchestration)

        with patch.object(certifier, "_probe", new=AsyncMock(return_value=probe)), patch(
            "src.portals.certification.ScrapeOrchestrator.run",
            new=run_mock,
        ):
            record = await certifier.certify(entry, run_id="phase7c2-zero", attempt_number=1)

        options = run_mock.await_args.kwargs["options"]
        self.assertFalse(options.fail_on_zero_discovery)
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.error_type, "zero_discovery")
        self.assertEqual(
            record.acquisition["adaptive_dom"]["discovery"]["structured_discovery"][
                "network_documents"
            ],
            3,
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.blueprint_hub import BlueprintHub
from src.portals.acquisition import (
    AcquisitionContext,
    AcquisitionRegistry,
    ICIMSProvider,
    PublicHtmlProvider,
)
from src.portals.detector import detect_portal
from src.portals.orchestrator import (
    ScrapeExecutionOptions,
    ScrapeOrchestrator,
    ScrapeOrchestratorHooks,
)
from src.portals.page_quality import assess_page_quality
from src.portals.surface_diagnostics import build_surface_report
from src.portals.url_intelligence import (
    promote_trusted_detail_url,
    rank_job_candidate_urls,
)


ROOT = Path(__file__).resolve().parents[1]


def jobposting_html(*, url: str, title: str = "Platform Engineer") -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "url": url,
        "description": "Build reliable platforms and own production operations.",
        "hiringOrganization": {"@type": "Organization", "name": "Example Corp"},
    }
    return (
        "<html><body><script type='application/ld+json'>"
        f"{json.dumps(payload)}"
        "</script><footer>This site is protected by reCAPTCHA.</footer></body></html>"
    )


class FakeTextClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def request_text(self, url: str, *, timeout_seconds: float = 20.0) -> str:
        self.calls.append(url)
        if not self.responses:
            raise AssertionError(f"No fake HTML response remains for {url}")
        return self.responses.pop(0)

    async def request_json(self, *args, **kwargs):
        raise AssertionError("JSON acquisition was not expected")


class FakeAdapter:
    def __init__(self, urls: list[str]) -> None:
        self.urls = list(urls)

    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None):
        return list(self.urls)


class FakeCrawler:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str]] = []

    async def arun(self, *, url, config):
        self.calls.append((url, config.kind))
        return self.handler(url, config)


def fake_detail_config(settings, wait_for, extraction_strategy=None, session_id=None):
    return SimpleNamespace(kind="detail", session_id=session_id)


class ResilientSurfaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        hub = BlueprintHub(ROOT)
        self.system_config = hub.system
        self.blueprint = hub.get_fleet_targets()[0]

    def test_job_schema_overrides_generic_footer_captcha_wording(self) -> None:
        url = "https://www.judge.example/jobs/details/1141335"
        html = jobposting_html(url=url, title="Security Engineer")

        quality = assess_page_quality(html=html)
        detection = detect_portal(listing_url=url, html=html, text_content="")
        report = build_surface_report(
            SimpleNamespace(
                success=True,
                status_code=200,
                url=url,
                html=html,
                cleaned_html=html,
                markdown="",
                links={"internal": [], "external": []},
                error_message=None,
            ),
            requested_url=url,
            mode="detail",
        )

        self.assertFalse(quality.blocked)
        self.assertFalse(detection.blocked)
        self.assertEqual(report["failure_stage"], "deterministic_extraction_succeeded")

    def test_sparse_challenge_markup_is_not_treated_as_a_job_page(self) -> None:
        html = (
            "<html><head><title>Careers</title>"
            "<script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'></script>"
            "</head><body><iframe src='https://www.google.com/recaptcha/api2/bframe'></iframe>"
            "</body></html>"
        )
        quality = assess_page_quality(html=html)
        self.assertTrue(quality.blocked)
        self.assertIn("sparse", str(quality.reason).lower())

    def test_diagnostic_reports_evidence_bound_icims_transition(self) -> None:
        outer = "https://careers.example.icims.com/company/jobs/7420?lang=en-us"
        canonical_listing = "https://tenant.i.icims.com/company/jobs"
        html = f'<html><iframe src="{canonical_listing}"></iframe></html>'
        report = build_surface_report(
            SimpleNamespace(
                success=True,
                status_code=200,
                url=outer,
                html=html,
                cleaned_html=html,
                markdown="",
                links={"internal": [], "external": []},
                error_message=None,
            ),
            requested_url=outer,
            mode="detail",
        )
        self.assertEqual(
            report["promoted_detail_url"],
            "https://tenant.i.icims.com/company/jobs/7420?lang=en-us",
        )
        self.assertEqual(report["failure_stage"], "embedded_detail_transition_detected")

    def test_candidate_ranker_rejects_telemetry_and_non_http_links(self) -> None:
        selected, metrics = rank_job_candidate_urls(
            [
                "https://www.googletagmanager.com/ns.html?id=GTM-1",
                "https://www.google.com/recaptcha/api2/anchor",
                "tel:8554858853",
                "https://jobs.example.com/job/3041524_usa/platform-engineer",
            ],
            listing_url="https://jobs.example.com/search-results-usa",
        )
        self.assertEqual(
            selected,
            ["https://jobs.example.com/job/3041524_usa/platform-engineer"],
        )
        self.assertEqual(metrics["selected_urls"], 1)

    def test_icims_detail_promotion_is_evidence_bound(self) -> None:
        outer = (
            "https://careers-insightglobal.icims.com/insightglobal-careers/jobs/7420"
            "?lang=en-us&in_iframe=1"
        )
        canonical_listing = (
            "https://c-13769-careers-insightglobal-com.i.icims.com/"
            "insightglobal-careers/jobs"
        )
        promoted = promote_trusted_detail_url(
            outer,
            platform_hint="icims",
            acquisition_hints={"listing_url": canonical_listing},
        )
        self.assertEqual(
            promoted,
            "https://c-13769-careers-insightglobal-com.i.icims.com/"
            "insightglobal-careers/jobs/7420?lang=en-us",
        )
        self.assertEqual(
            promote_trusted_detail_url(
                outer,
                platform_hint="icims",
                acquisition_hints={"listing_url": "https://attacker.example/jobs"},
            ),
            outer,
        )
        ranked, _ = rank_job_candidate_urls(
            [canonical_listing, outer],
            listing_url="https://careers-insightglobal.icims.com/insightglobal-careers/jobs",
            platform_hint="icims",
        )
        self.assertEqual(ranked, [outer])

    async def test_icims_provider_promotes_an_explicit_tenant_iframe(self) -> None:
        outer = "https://careers.example.icims.com/company-careers/jobs"
        canonical = "https://c-123-careers-example-com.i.icims.com/company-careers/jobs"
        client = FakeTextClient(
            [
                f'<html><iframe src="{canonical}"></iframe></html>',
                (
                    '<html><div>2 results</div><a href="/company-careers/jobs/101">One</a>'
                    '<a href="/company-careers/jobs/102">Two</a></html>'
                ),
            ]
        )
        outcome = await AcquisitionRegistry(
            client=client,
            providers=(ICIMSProvider(),),
        ).acquire(
            AcquisitionContext(
                listing_url=outer,
                max_pages=3,
                require_complete=False,
            )
        )

        self.assertIsNotNone(outcome.selected)
        selected = outcome.selected
        assert selected is not None
        self.assertEqual(selected.metadata["listing_url"], canonical)
        self.assertEqual(len(selected.discovered_urls), 2)
        self.assertIn("c-123-careers-example-com.i.icims.com", selected.trusted_hosts)
        self.assertEqual(len(client.calls), 2)

    async def test_public_html_provider_follows_only_rendered_listing_and_pages(self) -> None:
        client = FakeTextClient(
            [
                '<html><a href="/search-results-usa">Search jobs</a></html>',
                (
                    '<html><a href="/job/100_usa/data-engineer">Data Engineer</a>'
                    '<a href="/job/101_usa/sre">SRE</a>'
                    '<a rel="next" href="/search-results-usa?page=1">Next</a></html>'
                ),
                '<html><a href="/job/102_usa/security-engineer">Security</a></html>',
            ]
        )
        outcome = await AcquisitionRegistry(
            client=client,
            providers=(PublicHtmlProvider(),),
        ).acquire(
            AcquisitionContext(
                listing_url="https://careers.example.com/consultant-careers",
                source_platform_hint="custom_listing",
                max_pages=3,
                require_complete=False,
            )
        )

        self.assertIsNotNone(outcome.selected)
        selected = outcome.selected
        assert selected is not None
        self.assertEqual(len(selected.discovered_urls), 3)
        self.assertTrue(selected.complete)
        self.assertEqual(
            selected.metadata["listing_url"],
            "https://careers.example.com/search-results-usa",
        )
        self.assertEqual(len(client.calls), 3)

    async def test_bounded_public_html_never_enables_lifecycle_reconciliation(self) -> None:
        client = FakeTextClient(
            [
                '<html><a href="/search-results-usa">Search jobs</a></html>',
                (
                    '<html><a href="/job/100_usa/data-engineer">Data Engineer</a>'
                    '<a href="/search-results-usa?page=1">Next</a></html>'
                ),
            ]
        )
        outcome = await AcquisitionRegistry(
            client=client,
            providers=(PublicHtmlProvider(),),
        ).acquire(
            AcquisitionContext(
                listing_url="https://careers.example.com/consultant-careers",
                source_platform_hint="custom_listing",
                max_pages=2,
                require_complete=True,
            )
        )

        self.assertIsNone(outcome.selected)
        self.assertEqual(outcome.attempts[0]["status"], "incomplete")
        self.assertEqual(outcome.attempts[0]["discovered_urls"], 1)

    async def test_static_job_schema_skips_browser_and_llm(self) -> None:
        url = "https://jobs.example.com/job/100/platform-engineer"
        client = FakeTextClient([jobposting_html(url=url)])
        registry = AcquisitionRegistry(client=client, providers=(PublicHtmlProvider(),))
        crawler = FakeCrawler(lambda url, config: self.fail("browser must not run"))
        events: list[tuple[str, dict]] = []
        orchestrator = ScrapeOrchestrator(
            blueprint=self.blueprint,
            system_config=self.system_config,
            run_session_id="phase54-static",
            adapter=FakeAdapter([url]),
            acquisition_registry=registry,
        )

        with patch(
            "src.portals.orchestrator.detail_run_config",
            side_effect=fake_detail_config,
        ), patch(
            "src.portals.orchestrator.close_session",
            new_callable=AsyncMock,
        ), patch("src.portals.orchestrator.build_llm_strategy") as llm_builder:
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(
                    prefer_platform_api=False,
                    prefer_static_detail_html=True,
                ),
                hooks=ScrapeOrchestratorHooks(
                    on_event=lambda event, payload: events.append((event, payload))
                ),
            )

        self.assertEqual([job.title for job in result.jobs], ["Platform Engineer"])
        self.assertEqual(crawler.calls, [])
        llm_builder.assert_not_called()
        saved = [payload for event, payload in events if event == "extract_saved"]
        self.assertEqual(saved[0]["extraction_method"], "static_deterministic")

    async def test_rendered_icims_wrapper_is_followed_before_llm(self) -> None:
        outer = "https://careers.example.icims.com/company-careers/jobs/7420?lang=en-us"
        canonical_listing = "https://c-123-careers-example-com.i.icims.com/company-careers/jobs"
        canonical_detail = (
            "https://c-123-careers-example-com.i.icims.com/company-careers/jobs/7420"
            "?lang=en-us"
        )

        def handler(url, config):
            if url == outer:
                return SimpleNamespace(
                    success=True,
                    url=url,
                    html=f'<html><iframe src="{canonical_listing}"></iframe></html>',
                    cleaned_html="",
                    markdown="",
                    error_message=None,
                )
            self.assertEqual(url, canonical_detail)
            html = jobposting_html(url=canonical_detail, title="Company Security Officer")
            return SimpleNamespace(
                success=True,
                url=url,
                html=html,
                cleaned_html=html,
                markdown="",
                error_message=None,
            )

        crawler = FakeCrawler(handler)
        events: list[tuple[str, dict]] = []
        orchestrator = ScrapeOrchestrator(
            blueprint=self.blueprint,
            system_config=self.system_config,
            run_session_id="phase54-icims",
            adapter=FakeAdapter([outer]),
            source_platform_hint="icims",
        )
        with patch(
            "src.portals.orchestrator.detail_run_config",
            side_effect=fake_detail_config,
        ), patch(
            "src.portals.orchestrator.close_session",
            new_callable=AsyncMock,
        ), patch("src.portals.orchestrator.build_llm_strategy") as llm_builder:
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(prefer_platform_api=False),
                hooks=ScrapeOrchestratorHooks(
                    on_event=lambda event, payload: events.append((event, payload))
                ),
            )

        self.assertEqual([job.title for job in result.jobs], ["Company Security Officer"])
        self.assertEqual(
            crawler.calls,
            [(outer, "detail"), (canonical_detail, "detail")],
        )
        llm_builder.assert_not_called()
        self.assertIn(
            "deterministic_embedded_document",
            [
                payload.get("extraction_method")
                for event, payload in events
                if event == "extract_saved"
            ],
        )


if __name__ == "__main__":
    unittest.main()

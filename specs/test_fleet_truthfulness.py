from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.blueprint_hub import BlueprintHub
from src.portals.certification import (
    CertificationOptions,
    PortalCertificationBlocked,
    PortalCertificationJavaScriptShell,
    _classify_error,
)
from src.portals.detector import detect_portal
from src.portals.orchestrator import (
    ScrapeExecutionOptions,
    ScrapeOrchestrator,
    ScrapeOrchestratorHooks,
)
from src.portals.page_quality import assess_page_quality, classify_crawler_failure
from src.portals.url_intelligence import (
    assess_llm_job_grounding,
    rank_job_candidate_urls,
)
from src.schemas import JobPosting


ROOT = Path(__file__).resolve().parents[1]


class FakeAdapter:
    def __init__(self, urls: list[str]) -> None:
        self.urls = urls

    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None):
        return list(self.urls)


class FakeCrawler:
    def __init__(self, detail_html: str, llm_payload: dict) -> None:
        self.detail_html = detail_html
        self.llm_payload = llm_payload
        self.calls: list[str] = []

    async def arun(self, *, url, config):
        self.calls.append(config.kind)
        if config.kind == "llm":
            return SimpleNamespace(
                success=True,
                url=url,
                extracted_content=json.dumps(self.llm_payload),
                error_message=None,
            )
        return SimpleNamespace(
            success=True,
            url=url,
            html=self.detail_html,
            cleaned_html="",
            markdown="",
            error_message=None,
        )


def fake_detail_config(*args, **kwargs):
    return SimpleNamespace(kind="detail")


def fake_llm_config(*args, **kwargs):
    return SimpleNamespace(kind="llm")


class EvidenceBoundDetectionTests(unittest.TestCase):
    def test_plain_vendor_words_do_not_select_ats_adapters(self) -> None:
        detection = detect_portal(
            listing_url="https://www.winterwyman.example/insights",
            html=(
                "<html><body><article>Our consultants support Workday, Greenhouse, "
                "Ashby and JobDiva transformations.</article></body></html>"
            ),
        )

        self.assertEqual(detection.source_platform, "custom_listing")
        self.assertEqual(detection.profile_name, "generic_listing")

    def test_embedded_workday_url_is_platform_evidence(self) -> None:
        workday = (
            "https://capgroup.wd1.myworkdayjobs.com/en-US/"
            "capitalgroupcareers"
        )
        detection = detect_portal(
            listing_url="https://www.capitalgroup.example/careers",
            html=f'<a href="{workday}">Search jobs</a>',
        )

        self.assertEqual(detection.source_platform, "workday")
        self.assertEqual(detection.acquisition_hints["listing_url"], workday)
        self.assertEqual(detection.acquisition_hints["tenant"], "capgroup")


class SurfaceDispositionTests(unittest.TestCase):
    def test_sparse_javascript_shell_is_repairable_not_blocked(self) -> None:
        html = (
            "<html><body><div id='career-root'></div>"
            "<script src='/runtime.js'></script><script src='/app.js'></script>"
            "</body></html>"
        )
        quality = assess_page_quality(html=html)
        detection = detect_portal(
            listing_url="https://careers.example/jobs",
            html=html,
        )

        self.assertFalse(quality.blocked)
        self.assertEqual(quality.surface_kind, "javascript_shell")
        self.assertEqual(detection.surface_kind, "javascript_shell")
        self.assertEqual(detection.source_platform, "custom_spa")

    def test_failed_crawler_messages_separate_shells_and_access_control(self) -> None:
        shell = "Blocked by anti-bot protection: Structural: no <body> tag"
        blocked = "Blocked by anti-bot protection: Cloudflare JS challenge"

        self.assertEqual(classify_crawler_failure(shell), "javascript_shell")
        self.assertEqual(classify_crawler_failure(blocked), "confirmed_access_control")
        self.assertEqual(
            _classify_error(PortalCertificationJavaScriptShell(shell)),
            ("javascript_shell", "needs_repair"),
        )
        self.assertEqual(
            _classify_error(PortalCertificationBlocked(blocked)),
            ("access_blocked", "access_blocked"),
        )


class StrictFleetRankingTests(unittest.TestCase):
    def test_marketing_pages_do_not_become_detail_candidates(self) -> None:
        selected, metrics = rank_job_candidate_urls(
            [
                "https://www.chiptonross.example/forms",
                "https://www.chiptonross.example/notify",
                "https://www.chiptonross.example/resume",
                "https://www.chiptonross.example/jobs",
            ],
            listing_url="https://www.chiptonross.example/",
            platform_hint="custom_listing",
        )

        self.assertEqual(selected, [])
        self.assertFalse(metrics["fallback_preserved"])
        self.assertFalse(metrics["low_confidence_fallback_enabled"])
        self.assertEqual(metrics["strategy"], "evidence_bound_url_ranking_v2")

    def test_high_confidence_detail_survives_strict_ranking(self) -> None:
        detail = "https://jobs.example.com/jobs/details/1142387"
        selected, metrics = rank_job_candidate_urls(
            ["https://jobs.example.com/resources", detail],
            listing_url="https://jobs.example.com/jobs",
        )

        self.assertEqual(selected, [detail])
        self.assertEqual(metrics["high_confidence_urls"], 1)

    def test_generic_job_board_slug_and_numeric_id_is_high_confidence(self) -> None:
        detail = (
            "https://jobs.example.com/jb/"
            "Accounts-Payable-Specialist-Jobs-in-Long-Beach-California/14148738"
        )
        selected, metrics = rank_job_candidate_urls(
            [detail],
            listing_url="https://jobs.example.com/search",
        )

        self.assertEqual(selected, [detail])
        self.assertEqual(metrics["high_confidence_urls"], 1)
        self.assertIn("job_board_detail_path", metrics["top_candidates"][0]["reasons"])


class GroundedLlmTests(unittest.IsolatedAsyncioTestCase):
    def test_grounding_accepts_page_backed_fields_and_rejects_invention(self) -> None:
        url = "https://jobs.example.com/job/JR-4242/data-engineer"
        result = SimpleNamespace(
            html=(
                "<html><body><h1>Senior Data Engineer</h1><p>Acme Analytics</p>"
                "<p>Location: New York, NY</p><p>Requisition ID JR-4242</p>"
                "<p>Responsibilities include building reliable data platforms.</p>"
                "</body></html>"
            )
        )
        grounded = JobPosting(
            title="Senior Data Engineer",
            job_url=url,
            company="Acme Analytics",
            location_text="New York, NY",
            job_reference="JR-4242",
        )
        invented = grounded.model_copy(update={"company": "Imaginary Corporation"})

        valid, reason = assess_llm_job_grounding(grounded, url, result)
        self.assertTrue(valid, reason)
        invalid, reason = assess_llm_job_grounding(invented, url, result)
        self.assertFalse(invalid)
        self.assertIn("company", reason)

    async def test_orchestrator_rejects_an_ungrounded_llm_payload(self) -> None:
        url = "https://jobs.example.com/job/JR-4242/data-engineer"
        detail_html = (
            "<html><body><h1>Senior Data Engineer</h1>"
            "<p>Responsibilities include building data platforms.</p>"
            "<p>Qualifications include Python and SQL.</p></body></html>"
        )
        crawler = FakeCrawler(
            detail_html,
            {
                "title": "Senior Data Engineer",
                "job_url": url,
                "company": "Imaginary Corporation",
                "summary": "Build data platforms with Python and SQL.",
            },
        )
        hub = BlueprintHub(ROOT)
        orchestrator = ScrapeOrchestrator(
            blueprint=hub.get_fleet_targets()[0],
            system_config=hub.system,
            run_session_id="phase55-grounding",
            adapter=FakeAdapter([url]),
        )
        events: list[str] = []

        with patch("src.portals.orchestrator.detail_run_config", side_effect=fake_detail_config), patch(
            "src.portals.orchestrator.detail_llm_run_config",
            side_effect=fake_llm_config,
        ), patch(
            "src.portals.orchestrator.extract_job_from_result",
            return_value=None,
        ), patch(
            "src.portals.orchestrator.build_llm_strategy",
            return_value=object(),
        ), patch(
            "src.portals.orchestrator.close_session",
            new_callable=AsyncMock,
        ):
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(max_jobs=1),
                hooks=ScrapeOrchestratorHooks(
                    rank_discovered_urls=lambda urls: rank_job_candidate_urls(
                        urls,
                        listing_url="https://jobs.example.com/jobs",
                    ),
                    should_attempt_llm=lambda result, source_url: (True, "test"),
                    validate_llm_extracted_job=assess_llm_job_grounding,
                    on_event=lambda event, payload: events.append(event),
                ),
            )

        self.assertEqual(result.jobs, [])
        self.assertIn("llm_grounding_failed", events)
        self.assertEqual(crawler.calls, ["detail", "llm"])

    def test_fleet_certification_disables_llm_by_default(self) -> None:
        self.assertFalse(CertificationOptions().allow_llm_fallback)
        self.assertTrue(CertificationOptions(allow_llm_fallback=True).allow_llm_fallback)


if __name__ == "__main__":
    unittest.main()

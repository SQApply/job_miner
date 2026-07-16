from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.blueprint_hub import BlueprintHub
from src.portals.acquisition import (
    AcquisitionContext,
    AcquisitionRegistry,
)
from src.portals.detector import detect_portal
from src.portals.orchestrator import ScrapeExecutionOptions, ScrapeOrchestrator
from src.schemas import JobPosting


ROOT = Path(__file__).resolve().parents[1]


class FakeJsonClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def request_json(self, url, *, method="GET", payload=None, timeout_seconds=20.0):
        self.calls.append(
            {
                "url": url,
                "method": method,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("No fake response remains")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FailAdapter:
    def __init__(self):
        self.called = False

    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None):
        self.called = True
        raise AssertionError("Browser adapter must not run when an API result is selected")


class BrowserFallbackAdapter:
    def __init__(self, urls):
        self.urls = list(urls)
        self.called = False

    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None):
        self.called = True
        return list(self.urls)


class FailCrawler:
    def __init__(self):
        self.calls = []

    async def arun(self, *, url, config):
        self.calls.append((url, config))
        raise AssertionError("Browser detail extraction must not run for pre-extracted API jobs")


class FakeCrawler:
    def __init__(self, job):
        self.job = job
        self.calls = []

    async def arun(self, *, url, config):
        self.calls.append((url, config.kind))
        return SimpleNamespace(success=True, url=url, deterministic_job=self.job, error_message=None)


def fake_detail_config(settings, wait_for, extraction_strategy=None, session_id=None):
    return SimpleNamespace(kind="detail", session_id=session_id)


class PlatformAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_greenhouse_feed_returns_validated_preextracted_jobs(self) -> None:
        client = FakeJsonClient(
            [
                {
                    "jobs": [
                        {
                            "id": 101,
                            "title": "Senior Data Engineer",
                            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/101",
                            "location": {"name": "Remote"},
                            "updated_at": "2026-07-16T10:00:00Z",
                            "content": "<p>Build reliable data products.</p>",
                        },
                        {
                            "id": 102,
                            "title": "Unsafe redirect",
                            "absolute_url": "https://attacker.example/jobs/102",
                        },
                    ]
                }
            ]
        )
        outcome = await AcquisitionRegistry(client=client).acquire(
            AcquisitionContext(listing_url="https://boards.greenhouse.io/acme")
        )

        self.assertIsNotNone(outcome.selected)
        selected = outcome.selected
        assert selected is not None
        self.assertEqual(selected.platform, "greenhouse")
        self.assertEqual(selected.discovered_urls, ["https://job-boards.greenhouse.io/acme/jobs/101"])
        self.assertEqual(selected.preextracted_jobs[selected.discovered_urls[0]].summary, "Build reliable data products.")
        self.assertTrue(selected.complete)
        self.assertIn("/boards/acme/jobs?content=true", client.calls[0]["url"])

    async def test_lever_and_ashby_payloads_are_normalized_without_llm(self) -> None:
        lever_client = FakeJsonClient(
            [
                [
                    {
                        "id": "lev-1",
                        "text": "Platform Engineer",
                        "hostedUrl": "https://jobs.lever.co/acme/lev-1",
                        "applyUrl": "https://jobs.lever.co/acme/lev-1/apply",
                        "categories": {"location": "Chicago", "commitment": "Full-time"},
                        "descriptionPlain": "Own the platform.",
                        "createdAt": 1784192400000,
                    }
                ]
            ]
        )
        lever = await AcquisitionRegistry(client=lever_client).acquire(
            AcquisitionContext(listing_url="https://jobs.lever.co/acme")
        )
        assert lever.selected is not None
        self.assertEqual(lever.selected.preextracted_jobs[lever.selected.discovered_urls[0]].employment_type, "Full-time")

        ashby_client = FakeJsonClient(
            [
                {
                    "organizationName": "Acme",
                    "jobs": [
                        {
                            "id": "ash-1",
                            "title": "ML Engineer",
                            "jobUrl": "https://jobs.ashbyhq.com/acme/ash-1",
                            "applyUrl": "https://jobs.ashbyhq.com/acme/ash-1/application",
                            "location": "New York",
                            "employmentType": "FullTime",
                            "descriptionPlain": "Ship ML systems.",
                        }
                    ],
                }
            ]
        )
        ashby = await AcquisitionRegistry(client=ashby_client).acquire(
            AcquisitionContext(listing_url="https://jobs.ashbyhq.com/acme")
        )
        assert ashby.selected is not None
        job = ashby.selected.preextracted_jobs[ashby.selected.discovered_urls[0]]
        self.assertEqual(job.company, "Acme")
        self.assertEqual(job.summary, "Ship ML systems.")

    async def test_workday_paginates_and_requires_completeness_for_production(self) -> None:
        first_page = {
            "total": 21,
            "jobPostings": [{"externalPath": f"/job/location/job-{index}"} for index in range(20)],
        }
        final_page = {"total": 21, "jobPostings": [{"externalPath": "/job/location/job-20"}]}
        listing_url = "https://acme.wd5.myworkdayjobs.com/en-US/External"

        complete = await AcquisitionRegistry(client=FakeJsonClient([first_page, final_page])).acquire(
            AcquisitionContext(listing_url=listing_url, max_pages=2, require_complete=True)
        )
        assert complete.selected is not None
        self.assertTrue(complete.selected.complete)
        self.assertEqual(len(complete.selected.discovered_urls), 21)
        self.assertEqual(complete.selected.preextracted_jobs, {})
        self.assertEqual(complete.selected.pages_visited, 2)

        incomplete = await AcquisitionRegistry(client=FakeJsonClient([first_page])).acquire(
            AcquisitionContext(listing_url=listing_url, max_pages=1, require_complete=True)
        )
        self.assertIsNone(incomplete.selected)
        self.assertEqual(incomplete.attempts[0]["status"], "incomplete")

        bounded_test = await AcquisitionRegistry(client=FakeJsonClient([first_page])).acquire(
            AcquisitionContext(listing_url=listing_url, max_pages=1, require_complete=False)
        )
        assert bounded_test.selected is not None
        self.assertFalse(bounded_test.selected.complete)
        self.assertFalse(bounded_test.metrics()["reconciliation_safe"])

    async def test_workday_zero_total_cannot_complete_a_full_page(self) -> None:
        full_page = {
            "total": 0,
            "jobPostings": [{"externalPath": f"/job/location/job-{index}"} for index in range(20)],
        }
        listing_url = "https://acme.wd5.myworkdayjobs.com/en-US/External"

        bounded = await AcquisitionRegistry(client=FakeJsonClient([full_page])).acquire(
            AcquisitionContext(listing_url=listing_url, max_pages=1, require_complete=False)
        )
        assert bounded.selected is not None
        self.assertFalse(bounded.selected.complete)
        self.assertFalse(bounded.metrics()["reconciliation_safe"])
        self.assertIsNone(bounded.selected.metadata["reported_total"])

        production = await AcquisitionRegistry(client=FakeJsonClient([full_page])).acquire(
            AcquisitionContext(listing_url=listing_url, max_pages=1, require_complete=True)
        )
        self.assertIsNone(production.selected)
        self.assertEqual(production.attempts[0]["status"], "incomplete")

    async def test_orchestrator_skips_browser_and_llm_for_preextracted_feed_jobs(self) -> None:
        hub = BlueprintHub(ROOT)
        blueprint = hub.get_fleet_targets()[0]
        listing = blueprint.listing.model_copy(update={"page_url": "https://boards.greenhouse.io/acme"})
        blueprint = blueprint.model_copy(update={"listing": listing})
        client = FakeJsonClient(
            [
                {
                    "jobs": [
                        {
                            "id": 201,
                            "title": "GPU Platform Engineer",
                            "absolute_url": "https://boards.greenhouse.io/acme/jobs/201",
                            "content": "Build inference infrastructure.",
                        }
                    ]
                }
            ]
        )
        adapter = FailAdapter()
        crawler = FailCrawler()
        orchestrator = ScrapeOrchestrator(
            blueprint=blueprint,
            system_config=hub.system,
            run_session_id="phase3-api",
            adapter=adapter,
            acquisition_registry=AcquisitionRegistry(client=client),
        )

        with patch("src.portals.orchestrator.build_llm_strategy") as llm_builder:
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(max_jobs=1),
            )

        self.assertFalse(adapter.called)
        self.assertEqual(crawler.calls, [])
        llm_builder.assert_not_called()
        self.assertEqual([job.title for job in result.jobs], ["GPU Platform Engineer"])
        self.assertTrue(result.acquisition["selected"])
        self.assertEqual(result.acquisition["preextracted_jobs"], 1)

    async def test_provider_failure_transparently_uses_existing_browser_adapter(self) -> None:
        hub = BlueprintHub(ROOT)
        blueprint = hub.get_fleet_targets()[0]
        listing = blueprint.listing.model_copy(update={"page_url": "https://boards.greenhouse.io/acme"})
        blueprint = blueprint.model_copy(update={"listing": listing})
        url = "https://example.com/jobs/fallback"
        job = JobPosting(title="Fallback Engineer", job_url=url)
        adapter = BrowserFallbackAdapter([url])
        crawler = FakeCrawler(job)
        orchestrator = ScrapeOrchestrator(
            blueprint=blueprint,
            system_config=hub.system,
            run_session_id="phase3-fallback",
            adapter=adapter,
            acquisition_registry=AcquisitionRegistry(client=FakeJsonClient([RuntimeError("API unavailable")])),
        )

        with patch("src.portals.orchestrator.detail_run_config", side_effect=fake_detail_config), patch(
            "src.portals.orchestrator.extract_job_from_result",
            side_effect=lambda result, fallback_url: result.deterministic_job,
        ), patch("src.portals.orchestrator.close_session", new_callable=AsyncMock):
            result = await orchestrator.run_with_crawler(crawler, options=ScrapeExecutionOptions())

        self.assertTrue(adapter.called)
        self.assertEqual(result.jobs, [job])
        self.assertFalse(result.acquisition["selected"])
        self.assertEqual(result.acquisition["attempts"][0]["status"], "failed")

    def test_detector_finds_embedded_ats_without_xpath_or_yaml(self) -> None:
        greenhouse = detect_portal(
            listing_url="https://www.acme.example/careers",
            html='<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>',
        )
        self.assertEqual(greenhouse.source_platform, "greenhouse")
        self.assertEqual(greenhouse.acquisition_hints["board_token"], "acme")

        lever = detect_portal(
            listing_url="https://www.example.org/jobs",
            html=r'<script>window.jobs = "https:\/\/jobs.lever.co\/sampleco"</script>',
        )
        self.assertEqual(lever.source_platform, "lever")
        self.assertEqual(lever.acquisition_hints["site_token"], "sampleco")

    async def test_unsafe_or_malformed_tokens_do_not_create_api_requests(self) -> None:
        client = FakeJsonClient([])
        outcome = await AcquisitionRegistry(client=client).acquire(
            AcquisitionContext(
                listing_url="https://careers.example.com/jobs",
                source_platform_hint="greenhouse",
                acquisition_hints={"board_token": "../../internal"},
            )
        )
        self.assertIsNone(outcome.selected)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.blueprint_hub import BlueprintHub
from src.portals.orchestrator import (
    ScrapeExecutionOptions,
    ScrapeOrchestrator,
    ScrapeOrchestratorHooks,
)
from src.schemas import JobPosting


ROOT = Path(__file__).resolve().parents[1]


class FakeAdapter:
    def __init__(self, urls: list[str]):
        self.urls = urls
        self.session_logger = None

    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
        self.session_logger = session_logger
        return list(self.urls)


class FakeCrawler:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, str]] = []

    async def arun(self, *, url: str, config):
        self.calls.append((url, config.kind))
        return self.handler(url, config)


def fake_detail_config(settings, wait_for, extraction_strategy=None, session_id=None):
    return SimpleNamespace(kind="detail", session_id=session_id, wait_for=wait_for)


def fake_llm_config(settings, extraction_strategy, session_id):
    return SimpleNamespace(kind="llm", session_id=session_id)


class ScrapeOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        hub = BlueprintHub(ROOT)
        self.system_config = hub.system
        self.blueprint = hub.get_fleet_targets()[0]
        self.url_a = "https://example.com/jobs/a"
        self.url_b = "https://example.com/jobs/b"

    def patches(self):
        return (
            patch("src.portals.orchestrator.build_llm_strategy", return_value=object()),
            patch("src.portals.orchestrator.detail_run_config", side_effect=fake_detail_config),
            patch("src.portals.orchestrator.detail_llm_run_config", side_effect=fake_llm_config),
            patch("src.portals.orchestrator.close_session", new_callable=AsyncMock),
        )

    async def test_deterministic_lane_deduplicates_plans_and_honors_ten_job_limit(self) -> None:
        adapter = FakeAdapter([self.url_a, self.url_a, "", self.url_b])
        deterministic_job = JobPosting(title="Platform Engineer", job_url=self.url_a)
        crawler = FakeCrawler(
            lambda url, config: SimpleNamespace(
                success=True,
                url=url,
                error_message=None,
                deterministic_job=deterministic_job,
            )
        )
        orchestrator = ScrapeOrchestrator(
            blueprint=self.blueprint,
            system_config=self.system_config,
            run_session_id="phase2-test",
            adapter=adapter,
        )
        events: list[str] = []

        p1, p2, p3, p4 = self.patches()
        with p1, p2, p3, p4, patch(
            "src.portals.orchestrator.extract_job_from_result",
            side_effect=lambda result, url: result.deterministic_job,
        ):
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(detail_concurrency=3, max_jobs=1),
                hooks=ScrapeOrchestratorHooks(
                    plan_detail_urls=lambda urls: (
                        [*urls, "https://not-in-discovery.example/jobs/ignored"],
                        {"status": "planned", "known_skipped": 7},
                    ),
                    on_event=lambda event, payload: events.append(event),
                    adapter_session_logger="logger-sentinel",
                ),
            )

        self.assertEqual(result.discovered_job_urls, [self.url_a, self.url_b])
        self.assertEqual(result.attempted_job_urls, [self.url_a])
        self.assertEqual(result.jobs, [deterministic_job])
        self.assertEqual(result.skipped_existing, 7)
        self.assertEqual(result.rescrape_plan["bounded_max_jobs"], 1)
        self.assertEqual(crawler.calls, [(self.url_a, "detail")])
        self.assertEqual(adapter.session_logger, "logger-sentinel")
        self.assertIn("rescrape_plan_invalid_urls_ignored", events)

    async def test_llm_fallback_runs_only_after_deterministic_miss(self) -> None:
        adapter = FakeAdapter([self.url_a])
        llm_job = JobPosting(title="Data Engineer", job_url=self.url_a)

        def handler(url, config):
            if config.kind == "detail":
                return SimpleNamespace(success=True, url=url, deterministic_job=None, error_message=None)
            return SimpleNamespace(success=True, url=url, extracted_content={"title": "Data Engineer"})

        crawler = FakeCrawler(handler)
        orchestrator = ScrapeOrchestrator(
            blueprint=self.blueprint,
            system_config=self.system_config,
            run_session_id="phase2-llm",
            adapter=adapter,
        )
        events: list[tuple[str, dict]] = []

        p1, p2, p3, p4 = self.patches()
        with p1, p2, p3, p4, patch(
            "src.portals.orchestrator.extract_job_from_result",
            side_effect=lambda result, url: result.deterministic_job,
        ), patch("src.portals.orchestrator.parse_extracted_jobs", return_value=llm_job):
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(),
                hooks=ScrapeOrchestratorHooks(on_event=lambda event, payload: events.append((event, payload))),
            )

        self.assertEqual(result.jobs, [llm_job])
        self.assertEqual(crawler.calls, [(self.url_a, "detail"), (self.url_a, "llm")])
        saved = [payload for event, payload in events if event == "extract_saved"]
        self.assertEqual(saved[0]["extraction_method"], "llm_fallback")

    async def test_transient_failure_retries_with_backoff_and_recovers(self) -> None:
        adapter = FakeAdapter([self.url_a])
        job = JobPosting(title="SRE", job_url=self.url_a)
        detail_calls = 0

        def handler(url, config):
            nonlocal detail_calls
            detail_calls += 1
            if detail_calls == 1:
                return SimpleNamespace(success=False, url=url, error_message="temporary timeout")
            return SimpleNamespace(success=True, url=url, deterministic_job=job, error_message=None)

        crawler = FakeCrawler(handler)
        orchestrator = ScrapeOrchestrator(
            blueprint=self.blueprint,
            system_config=self.system_config,
            run_session_id="phase2-retry",
            adapter=adapter,
        )
        p1, p2, p3, p4 = self.patches()
        with p1, p2, p3, p4, patch(
            "src.portals.orchestrator.extract_job_from_result",
            side_effect=lambda result, url: result.deterministic_job,
        ), patch("src.portals.orchestrator.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(detail_retry_attempts=1),
            )

        self.assertEqual(result.jobs, [job])
        self.assertEqual(result.detail_failures, [])
        self.assertEqual(detail_calls, 2)
        sleep.assert_awaited_once_with(1)

    async def test_final_failure_is_auditable_and_reports_actual_attempts(self) -> None:
        adapter = FakeAdapter([self.url_a])
        crawler = FakeCrawler(
            lambda url, config: SimpleNamespace(success=False, url=url, error_message="blocked")
        )
        orchestrator = ScrapeOrchestrator(
            blueprint=self.blueprint,
            system_config=self.system_config,
            run_session_id="phase2-failure",
            adapter=adapter,
        )
        artifact_calls: list[tuple[int, str, int]] = []
        p1, p2, p3, p4 = self.patches()
        with p1, p2, p3, p4, patch(
            "src.portals.orchestrator.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(detail_retry_attempts=2),
                hooks=ScrapeOrchestratorHooks(
                    on_failure_artifacts=lambda index, url, attempt, response: (
                        artifact_calls.append((index, url, attempt))
                        or [{"artifact_type": "failed_detail"}]
                    ),
                ),
            )

        self.assertEqual(result.jobs, [])
        self.assertEqual(result.detail_failures[0]["attempts"], 3)
        self.assertEqual(artifact_calls, [(1, self.url_a, 3)])
        self.assertEqual(result.artifacts, [{"artifact_type": "failed_detail"}])

    async def test_rejected_detail_url_is_not_retried(self) -> None:
        class RejectedUrl(ValueError):
            pass

        adapter = FakeAdapter([self.url_a])
        crawler = FakeCrawler(lambda url, config: self.fail("crawler must not be called"))
        orchestrator = ScrapeOrchestrator(
            blueprint=self.blueprint,
            system_config=self.system_config,
            run_session_id="phase2-safety",
            adapter=adapter,
        )
        p1, p2, p3, p4 = self.patches()
        with p1, p2, p3, p4:
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(detail_retry_attempts=5),
                hooks=ScrapeOrchestratorHooks(
                    validate_detail_url=lambda url: (_ for _ in ()).throw(RejectedUrl("unsafe URL")),
                    is_rejected_error=lambda exc: isinstance(exc, RejectedUrl),
                ),
            )

        self.assertEqual(result.rejected_urls, 1)
        self.assertEqual(result.detail_failures[0]["attempts"], 1)
        self.assertEqual(crawler.calls, [])

    async def test_zero_discovery_is_a_failed_cycle(self) -> None:
        orchestrator = ScrapeOrchestrator(
            blueprint=self.blueprint,
            system_config=self.system_config,
            run_session_id="phase2-empty",
            adapter=FakeAdapter([]),
        )
        with self.assertRaisesRegex(RuntimeError, "zero valid job URLs"):
            await orchestrator.run_with_crawler(
                FakeCrawler(lambda url, config: None),
                options=ScrapeExecutionOptions(),
            )

    def test_both_production_entry_points_use_the_shared_orchestrator(self) -> None:
        mission_control = (ROOT / "src" / "mission_control.py").read_text(encoding="utf-8")
        portal_runner = (ROOT / "src" / "portals" / "runner.py").read_text(encoding="utf-8")
        self.assertIn("from .portals.orchestrator import (", mission_control)
        self.assertIn("from .orchestrator import (", portal_runner)
        self.assertIn("orchestrator = ScrapeOrchestrator(", mission_control)
        self.assertIn("orchestrator = ScrapeOrchestrator(", portal_runner)


if __name__ == "__main__":
    unittest.main()

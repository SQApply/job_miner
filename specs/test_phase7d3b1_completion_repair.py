from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.blueprint_hub import BlueprintHub
from src.crawl.browser_evidence import BrowserEvidenceReport
from src.portals.adaptive_dom import AdaptiveDomDiscoveryService
from src.portals.contracts import CompletenessState, DiscoveryBatch, ScrapeStrategy
from src.portals.dom_pagination import (
    GeneralizedDomPaginationDriver,
    GeneralizedDomPaginationStep,
)
from src.portals.orchestrator import (
    ScrapeExecutionOptions,
    ScrapeOrchestrator,
    ScrapeOrchestratorHooks,
)
from src.portals.url_intelligence import (
    assess_job_candidate_url,
    rank_job_candidate_urls,
)
from src.schemas import BrowserSettings, JobPosting


ROOT = Path(__file__).resolve().parents[1]


def _report(url: str) -> BrowserEvidenceReport:
    now = datetime.now(timezone.utc).isoformat()
    return BrowserEvidenceReport(
        requested_url=url,
        final_url=url,
        success=True,
        status_code=200,
        started_at=now,
        completed_at=now,
    )


class _SequencePaginationDriver:
    def __init__(self, steps: list[GeneralizedDomPaginationStep]) -> None:
        self.steps = list(steps)
        self.calls = 0

    async def advance(self, session, *, requested_url: str):
        step = self.steps[self.calls]
        self.calls += 1
        return step


class GeneralizedDomPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_driver_selects_an_approved_iframe_pagination_control(self) -> None:
        listing = "https://8.8.8.8/jobs"

        class Frame:
            def __init__(self, url: str, *, control: bool) -> None:
                self.url = url
                self.control = control
                self.clicked = False

            async def evaluate(self, script, payload=None):
                if payload is None:
                    return {
                        "height": 2000,
                        "viewport": 800,
                        "scroll": 1200,
                        "at_bottom": True,
                        "enabled_controls": 0,
                        "disabled_controls": 1,
                    }
                if payload.get("probeOnly"):
                    return {
                        "action": "probe_click" if self.control else "probe_scroll",
                        "score": 100 if self.control else 10,
                        "enabled_controls": 1 if self.control else 0,
                    }
                self.clicked = True
                return {"action": "click", "label": "Load more jobs"}

        main = Frame(listing, control=False)
        embedded = Frame("https://8.8.8.8/jobs/frame", control=True)
        main.frames = [main, embedded]

        async def wait_for_load_state(*args, **kwargs):
            return None

        async def wait_for_timeout(*args, **kwargs):
            return None

        main.wait_for_load_state = wait_for_load_state
        main.wait_for_timeout = wait_for_timeout

        class Collector:
            async def snapshot_current_page(self, page, requested_url, *, allowed_hosts):
                return _report(requested_url)

        session = SimpleNamespace(
            page=main,
            collector=Collector(),
            allowed_hosts=("8.8.8.8",),
        )
        step = await GeneralizedDomPaginationDriver().advance(
            session,
            requested_url=listing,
        )

        self.assertFalse(main.clicked)
        self.assertTrue(embedded.clicked)
        self.assertEqual(step.action, "click")

    async def test_disabled_terminal_control_proves_complete_merged_catalog(self) -> None:
        listing = "https://careers.example.com/jobs"
        first = DiscoveryBatch.from_urls(
            ["https://careers.example.com/jobs/1001"],
            strategy=ScrapeStrategy.BLUEPRINT_DOM,
        )
        second = DiscoveryBatch.from_urls(
            ["https://careers.example.com/jobs/1002"],
            strategy=ScrapeStrategy.BLUEPRINT_DOM,
        )
        driver = _SequencePaginationDriver(
            [
                GeneralizedDomPaginationStep(
                    report=_report(listing),
                    action="click",
                    label="Load more jobs",
                    state={
                        "height": 2000,
                        "at_bottom": True,
                        "enabled_controls": 0,
                        "disabled_controls": 1,
                    },
                )
            ]
        )
        service = AdaptiveDomDiscoveryService(
            BrowserSettings(),
            pagination_driver=driver,
        )
        service._discover_report = AsyncMock(return_value=second)

        result = await service._exhaust_live_listing(
            SimpleNamespace(),
            first,
            listing_url=listing,
            max_candidates=100,
            max_pages=10,
        )

        self.assertEqual(result.completeness, CompletenessState.COMPLETE)
        self.assertTrue(result.pagination_complete)
        self.assertEqual(len(result.discovered_urls), 2)
        self.assertEqual(
            result.metrics["generalized_pagination"]["terminal_reason"],
            "terminal_pagination_control_disabled",
        )

    async def test_page_safety_cap_remains_partial(self) -> None:
        listing = "https://careers.example.com/jobs"
        first = DiscoveryBatch.from_urls(["https://careers.example.com/jobs/1001"])
        second = DiscoveryBatch.from_urls(["https://careers.example.com/jobs/1002"])
        driver = _SequencePaginationDriver(
            [
                GeneralizedDomPaginationStep(
                    report=_report(listing),
                    action="scroll",
                    label=None,
                    state={
                        "height": 3000,
                        "at_bottom": False,
                        "enabled_controls": 0,
                        "disabled_controls": 0,
                    },
                )
            ]
        )
        service = AdaptiveDomDiscoveryService(
            BrowserSettings(),
            pagination_driver=driver,
        )
        service._discover_report = AsyncMock(return_value=second)

        result = await service._exhaust_live_listing(
            SimpleNamespace(),
            first,
            listing_url=listing,
            max_candidates=100,
            max_pages=2,
        )

        self.assertEqual(result.completeness, CompletenessState.PARTIAL)
        self.assertFalse(result.pagination_complete)
        self.assertEqual(
            result.metrics["generalized_pagination"]["terminal_reason"],
            "page_safety_cap_reached",
        )

    async def test_two_stable_bottom_rounds_prove_linkless_pagination_exhausted(self) -> None:
        listing = "https://careers.example.com/opportunities"
        catalog = DiscoveryBatch.from_urls(
            ["https://careers.example.com/opportunities/role-1001"],
            strategy=ScrapeStrategy.BLUEPRINT_DOM,
        )
        stable_step = GeneralizedDomPaginationStep(
            report=_report(listing),
            action="scroll",
            label=None,
            state={
                "height": 3000,
                "at_bottom": True,
                "enabled_controls": 0,
                "disabled_controls": 0,
            },
        )
        service = AdaptiveDomDiscoveryService(
            BrowserSettings(),
            pagination_driver=_SequencePaginationDriver([stable_step, stable_step]),
        )
        service._discover_report = AsyncMock(side_effect=[catalog, catalog])

        result = await service._exhaust_live_listing(
            SimpleNamespace(),
            catalog,
            listing_url=listing,
            max_candidates=100,
            max_pages=5,
        )

        self.assertEqual(result.completeness, CompletenessState.COMPLETE)
        self.assertTrue(result.pagination_complete)
        self.assertEqual(result.pages_visited, 3)
        self.assertEqual(
            result.metrics["generalized_pagination"]["terminal_reason"],
            "stable_bottom_without_pagination_control",
        )


class ActionUrlTests(unittest.TestCase):
    def test_icims_login_is_rejected_and_retained_only_as_apply_url(self) -> None:
        listing = "https://careers-example.icims.com/jobs/search"
        detail = "https://careers-example.icims.com/company/jobs/7107?lang=en-us"
        login = "https://careers-example.icims.com/jobs/7107/login"
        ranked, metrics = rank_job_candidate_urls(
            [login, detail],
            listing_url=listing,
            platform_hint="icims",
        )
        batch = DiscoveryBatch.from_urls([login, detail])
        projected = ScrapeOrchestrator._rank_candidate_batch(batch, ranked)

        self.assertEqual(ranked, [detail])
        self.assertTrue(assess_job_candidate_url(login).hard_reject)
        self.assertIn("job_action_route:login", assess_job_candidate_url(login).reasons)
        self.assertEqual(projected.discovered_urls, [detail])
        self.assertEqual(projected.candidates[0].apply_url, login)
        self.assertEqual(metrics["selected_urls"], 1)


class _PartialAdapter:
    async def discover_candidates(self, *args, **kwargs) -> DiscoveryBatch:
        return DiscoveryBatch.from_urls(
            ["https://example.com/jobs/1001"],
            completeness=CompletenessState.PARTIAL,
            pages_visited=1,
        )


class _CompleteAdaptiveService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def discover(
        self,
        listing_url: str,
        *,
        allowed_hosts,
        max_candidates: int,
        max_pages: int,
        require_complete: bool,
    ) -> DiscoveryBatch:
        self.calls.append(
            {
                "max_candidates": max_candidates,
                "max_pages": max_pages,
                "require_complete": require_complete,
            }
        )
        return DiscoveryBatch.from_urls(
            [
                "https://example.com/jobs/1001",
                "https://example.com/jobs/1002",
            ],
            completeness=CompletenessState.COMPLETE,
            pages_visited=3,
            pagination_complete=True,
        )


class _DetailCrawler:
    async def arun(self, *, url: str, config):
        return SimpleNamespace(
            success=True,
            url=url,
            deterministic_job=JobPosting(title="Recovered Engineer", job_url=url),
            error_message=None,
        )


class CompleteRepairOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_adapter_is_repaired_even_when_it_already_has_urls(self) -> None:
        hub = BlueprintHub(ROOT)
        adaptive = _CompleteAdaptiveService()
        orchestrator = ScrapeOrchestrator(
            blueprint=hub.get_fleet_targets()[0],
            system_config=hub.system,
            run_session_id="phase7d3b1-completion-repair",
            adapter=_PartialAdapter(),
            adaptive_dom_service=adaptive,
        )
        with patch(
            "src.portals.orchestrator.detail_run_config",
            return_value=SimpleNamespace(kind="detail"),
        ), patch(
            "src.portals.orchestrator.extract_job_from_result",
            side_effect=lambda result, url: result.deterministic_job,
        ), patch("src.portals.orchestrator.close_session", new_callable=AsyncMock):
            result = await orchestrator.run_with_crawler(
                _DetailCrawler(),
                options=ScrapeExecutionOptions(
                    prefer_platform_api=False,
                    enable_adaptive_dom_fallback=True,
                    require_complete_acquisition=True,
                    max_acquisition_pages=17,
                ),
                hooks=ScrapeOrchestratorHooks(),
            )

        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(result.discovery_batch["completeness"], "complete")
        self.assertTrue(result.discovery_batch["pagination_complete"])
        self.assertEqual(adaptive.calls[0]["max_pages"], 17)
        self.assertTrue(adaptive.calls[0]["require_complete"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from src.blueprint_hub import BlueprintHub
from src.portals.contracts import (
    CompletenessState,
    DiscoveryBatch,
    DiscoveryCandidate,
    DiscoveryCandidateKind,
    ScrapeStrategy,
)
from src.portals.orchestrator import (
    ScrapeExecutionOptions,
    ScrapeOrchestrator,
    ScrapeOrchestratorHooks,
)
from src.schemas import JobPosting


ROOT = Path(__file__).resolve().parents[1]


class CandidateAdapter:
    def __init__(self, batch: DiscoveryBatch) -> None:
        self.batch = batch
        self.legacy_calls = 0

    async def discover_candidates(
        self,
        crawler,
        blueprint,
        system_config,
        session_logger=None,
    ) -> DiscoveryBatch:
        return self.batch

    async def discover_job_urls(self, *args, **kwargs):
        self.legacy_calls += 1
        raise AssertionError("candidate-native adapters must not use the legacy URL method")


class FakeCrawler:
    def __init__(self, handler=None) -> None:
        self.handler = handler
        self.calls: list[str] = []

    async def arun(self, *, url: str, config):
        self.calls.append(url)
        if self.handler is None:
            raise AssertionError("crawler must not be called")
        return self.handler(url, config)


class DiscoveryCandidateContractTests(unittest.TestCase):
    def test_url_candidate_identity_is_stable(self) -> None:
        first = DiscoveryCandidate.from_url("https://example.com/jobs/123")
        second = DiscoveryCandidate.from_url("https://example.com/jobs/123")

        self.assertEqual(first.candidate_id, second.candidate_id)
        self.assertEqual(first.identity_key, "detail_url:https://example.com/jobs/123")

    def test_batch_carries_url_and_linkless_candidates_without_xpath(self) -> None:
        url_candidate = DiscoveryCandidate.from_url("https://example.com/jobs/123")
        click_candidate = DiscoveryCandidate(
            candidate_id="dom_card_structural_signature_1",
            kind=DiscoveryCandidateKind.DOM_CLICK,
            node_token="session-node-17",
            title_hint="Senior Platform Engineer",
            confidence=0.91,
            evidence={"structural_signature": "article>h2+div+button"},
        )
        batch = DiscoveryBatch(
            strategy=ScrapeStrategy.BLUEPRINT_DOM,
            candidates=[url_candidate, click_candidate],
        )

        self.assertEqual(batch.discovered_urls, ["https://example.com/jobs/123"])
        self.assertEqual(batch.linkless_candidates, [click_candidate])

    def test_legacy_url_projection_deduplicates_and_audits_invalid_values(self) -> None:
        batch = DiscoveryBatch.from_urls(
            [
                "https://example.com/jobs/1",
                "https://example.com/jobs/1",
                "javascript:void(0)",
                "",
            ]
        )

        self.assertEqual(batch.discovered_urls, ["https://example.com/jobs/1"])
        self.assertEqual(batch.metrics["raw_url_candidates"], 3)
        self.assertEqual(batch.metrics["deduplicated_url_candidates"], 1)
        self.assertEqual(batch.metrics["invalid_url_candidates"], 1)

    def test_candidate_contract_rejects_unusable_evidence(self) -> None:
        with self.assertRaises(ValidationError):
            DiscoveryCandidate(
                candidate_id="missing-url",
                kind=DiscoveryCandidateKind.URL,
            )
        with self.assertRaises(ValidationError):
            DiscoveryCandidate(
                candidate_id="missing-node",
                kind=DiscoveryCandidateKind.DOM_CLICK,
                title_hint="Engineer",
            )
        with self.assertRaises(ValidationError):
            DiscoveryBatch(
                strategy=ScrapeStrategy.BLUEPRINT_DOM,
                completeness=CompletenessState.COMPLETE,
                pagination_complete=True,
            )


class CandidateAwareOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        hub = BlueprintHub(ROOT)
        self.system = hub.system
        self.blueprint = hub.get_fleet_targets()[0]

    async def test_orchestrator_extracts_url_candidate_and_retains_linkless_candidate(self) -> None:
        detail_url = "https://example.com/jobs/123"
        job = JobPosting(title="Platform Engineer", job_url=detail_url)
        url_candidate = DiscoveryCandidate.from_url(detail_url)
        click_candidate = DiscoveryCandidate(
            candidate_id="dom_click_candidate_123",
            kind=DiscoveryCandidateKind.DOM_CLICK,
            node_token="node-123",
            title_hint="Data Engineer",
            confidence=0.88,
        )
        adapter = CandidateAdapter(
            DiscoveryBatch(
                strategy=ScrapeStrategy.BLUEPRINT_DOM,
                candidates=[url_candidate, click_candidate],
            )
        )
        crawler = FakeCrawler(
            lambda url, config: SimpleNamespace(
                success=True,
                url=url,
                deterministic_job=job,
                error_message=None,
            )
        )
        events: list[str] = []
        orchestrator = ScrapeOrchestrator(
            blueprint=self.blueprint,
            system_config=self.system,
            run_session_id="phase7a-candidate-test",
            adapter=adapter,
        )

        with patch(
            "src.portals.orchestrator.detail_run_config",
            return_value=SimpleNamespace(kind="detail"),
        ), patch(
            "src.portals.orchestrator.extract_job_from_result",
            side_effect=lambda result, url: result.deterministic_job,
        ), patch("src.portals.orchestrator.close_session", new_callable=AsyncMock):
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(prefer_platform_api=False),
                hooks=ScrapeOrchestratorHooks(
                    on_event=lambda event, payload: events.append(event)
                ),
            )

        self.assertEqual(result.discovered_job_urls, [detail_url])
        self.assertEqual(result.jobs, [job])
        self.assertEqual(result.linkless_candidate_count, 1)
        self.assertEqual(result.attempted_candidate_ids, [url_candidate.candidate_id])
        self.assertEqual(adapter.legacy_calls, 0)
        self.assertIn("linkless_candidates_deferred", events)

    async def test_linkless_only_discovery_is_not_reported_as_zero_discovery(self) -> None:
        candidate = DiscoveryCandidate(
            candidate_id="dom_click_only_1",
            kind=DiscoveryCandidateKind.DOM_CLICK,
            node_token="node-only-1",
            title_hint="Cloud Engineer",
            confidence=0.8,
        )
        adapter = CandidateAdapter(
            DiscoveryBatch(
                strategy=ScrapeStrategy.BLUEPRINT_DOM,
                candidates=[candidate],
            )
        )
        crawler = FakeCrawler()
        orchestrator = ScrapeOrchestrator(
            blueprint=self.blueprint,
            system_config=self.system,
            run_session_id="phase7a-linkless-test",
            adapter=adapter,
        )

        result = await orchestrator.run_with_crawler(
            crawler,
            options=ScrapeExecutionOptions(prefer_platform_api=False),
        )

        self.assertEqual(result.discovered_job_urls, [])
        self.assertEqual(result.discovered_candidates, [candidate])
        self.assertEqual(result.linkless_candidate_count, 1)
        self.assertEqual(result.jobs, [])
        self.assertEqual(crawler.calls, [])


if __name__ == "__main__":
    unittest.main()

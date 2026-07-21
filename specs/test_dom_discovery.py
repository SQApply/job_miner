from __future__ import annotations

import unittest
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.blueprint_hub import BlueprintHub
from src.crawl.browser_evidence import BrowserEvidenceReport
from src.crawl.dom_snapshot import DomNodeEvidence, FrameDomSnapshot
from src.portals.adaptive_dom import AdaptiveDomDiscoveryService
from src.portals.acquisition import AcquisitionOutcome
from src.portals.certification import (
    CertificationOptions,
    PortalFleetCertifier,
    PortalInventoryEntry,
)
from src.portals.contracts import (
    CompletenessState,
    DiscoveryBatch,
    DiscoveryCandidate,
    DiscoveryCandidateKind,
    ScrapeStrategy,
)
from src.portals.dom_discovery import (
    DomCandidateDiscoverer,
    preserve_evidence_backed_urls,
)
from src.portals.orchestrator import (
    ScrapeExecutionOptions,
    ScrapeOrchestrator,
    ScrapeOrchestratorHooks,
)
from src.schemas import BrowserSettings, JobPosting


ROOT = Path(__file__).resolve().parents[1]


def dom_node(
    token: str,
    parent: str | None,
    tag: str,
    signature: str,
    *,
    text: str | None = None,
    href: str | None = None,
    role: str | None = None,
    clickable: bool = False,
    attributes: dict[str, str] | None = None,
) -> DomNodeEvidence:
    return DomNodeEvidence(
        node_token=token,
        parent_token=parent,
        depth=token.count(":"),
        tag=tag,
        role=role,
        text=text,
        href=href,
        attributes=attributes or {},
        clickable=clickable,
        structural_signature=signature,
    )


def evidence_report(nodes: list[DomNodeEvidence], *, title: str = "Open Jobs") -> BrowserEvidenceReport:
    now = datetime.now(timezone.utc).isoformat()
    return BrowserEvidenceReport(
        requested_url="https://careers.example.com/careers",
        final_url="https://careers.example.com/careers",
        success=True,
        status_code=200,
        title=title,
        started_at=now,
        completed_at=now,
        frames=[
            FrameDomSnapshot(
                frame_id="f0",
                frame_url="https://careers.example.com/careers",
                nodes=nodes,
            )
        ],
    )


def repeated_table_nodes() -> list[DomNodeEvidence]:
    nodes = [
        dom_node("n:body", None, "body", "body|||main,nav"),
        dom_node("n:main", "n:body", "main", "main|main||table,a,a", role="main"),
        dom_node("n:table", "n:main", "table", "table|||tbody"),
        dom_node("n:tbody", "n:table", "tbody", "tbody|||tr,tr,tr"),
        dom_node(
            "n:sort1",
            "n:main",
            "a",
            "a|link|c|",
            text="Job Title",
            href="https://careers.example.com/careers?sort=title",
            role="link",
            clickable=True,
        ),
        dom_node(
            "n:sort2",
            "n:main",
            "a",
            "a|link|c|",
            text="Date Posted",
            href="https://careers.example.com/careers?sort=date",
            role="link",
            clickable=True,
        ),
    ]
    for index, title in enumerate(("Cloud Engineer", "Data Analyst", "QA Lead"), start=1):
        row = f"n:r{index}"
        url = f"https://careers.example.com/opening?record={index}"
        nodes.append(dom_node(row, "n:tbody", "tr", "tr|||td,td,td,td"))
        for field, value in enumerate((title, f"City {index}", "CA", "Today"), start=1):
            cell = f"{row}:c{field}"
            nodes.append(dom_node(cell, row, "td", "td|||a"))
            nodes.append(
                dom_node(
                    f"{cell}:a",
                    cell,
                    "a",
                    "a|link|c|",
                    text=value,
                    href=url,
                    role="link",
                    clickable=True,
                )
            )
    return nodes


class StructuralDomDiscoveryTests(unittest.TestCase):
    def test_repeated_table_yields_one_job_per_row_without_url_pattern_matching(self) -> None:
        batch = DomCandidateDiscoverer().discover(evidence_report(repeated_table_nodes()))

        self.assertEqual(len(batch.candidates), 3)
        self.assertEqual(
            [candidate.title_hint for candidate in batch.candidates],
            ["Cloud Engineer", "Data Analyst", "QA Lead"],
        )
        self.assertEqual(
            batch.discovered_urls,
            [
                "https://careers.example.com/opening?record=1",
                "https://careers.example.com/opening?record=2",
                "https://careers.example.com/opening?record=3",
            ],
        )
        self.assertEqual(batch.metrics["individual_link_candidates"], 0)
        self.assertEqual(batch.completeness, CompletenessState.PARTIAL)
        self.assertTrue(
            all(
                candidate.evidence["structural_job_grounding"]
                and candidate.evidence["evidence_preserving"]
                for candidate in batch.candidates
            )
        )

    def test_repeated_linkless_cards_emit_click_tokens_but_ignore_form_controls(self) -> None:
        nodes = [
            dom_node("n:body", None, "body", "body|||main,nav"),
            dom_node("n:main", "n:body", "main", "main|main||div,input", role="main"),
            dom_node("n:list", "n:main", "div", "div|||article,article,article"),
            dom_node(
                "n:filter",
                "n:main",
                "input",
                "input|textbox|c|",
                text="Search jobs",
                role="textbox",
                clickable=True,
            ),
        ]
        for index, title in enumerate(("Engineer I", "Engineer II", "Engineer III"), start=1):
            card = f"n:card{index}"
            nodes.extend(
                [
                    dom_node(card, "n:list", "article", "article|article||h2,div,button", role="article"),
                    dom_node(f"{card}:h", card, "h2", "h2|heading||", text=title, role="heading"),
                    dom_node(f"{card}:l", card, "div", "div|||", text=f"City {index}"),
                    dom_node(
                        f"{card}:b",
                        card,
                        "button",
                        "button|button|c|",
                        text="View role",
                        role="button",
                        clickable=True,
                    ),
                ]
            )

        batch = DomCandidateDiscoverer().discover(evidence_report(nodes))

        self.assertEqual(len(batch.linkless_candidates), 3)
        self.assertTrue(
            all(candidate.kind == DiscoveryCandidateKind.DOM_CLICK for candidate in batch.candidates)
        )
        self.assertTrue(
            all(candidate.evidence["evidence_preserving"] for candidate in batch.candidates)
        )
        self.assertNotIn("n:filter", [candidate.node_token for candidate in batch.candidates])

    def test_high_confidence_dom_evidence_preserves_unfamiliar_safe_url_shapes(self) -> None:
        high = DiscoveryCandidate.from_url(
            "https://careers.example.com/opening?opaque=alpha",
            confidence=0.92,
            evidence={
                "origin": "adaptive_dom_repeated_cluster",
                "evidence_preserving": True,
                "structural_job_grounding": True,
            },
        )
        low = DiscoveryCandidate.from_url(
            "https://careers.example.com/opening?opaque=beta",
            confidence=0.60,
            evidence={
                "origin": "adaptive_dom_repeated_cluster",
                "evidence_preserving": True,
                "structural_job_grounding": True,
            },
        )

        urls, metrics = preserve_evidence_backed_urls([], [high, low])

        self.assertEqual(urls, [high.detail_url])
        self.assertEqual(metrics["adaptive_urls_preserved"], 1)

    def test_individual_navigation_link_never_overrides_url_ranker(self) -> None:
        candidate = DiscoveryCandidate.from_url(
            "https://careers.example.com/employers/salary-guide",
            confidence=0.98,
            evidence={
                "origin": "adaptive_dom_individual_link",
                "evidence_preserving": True,
            },
        )

        urls, metrics = preserve_evidence_backed_urls([], [candidate])

        self.assertEqual(urls, [])
        self.assertEqual(metrics["adaptive_candidates_considered"], 0)

    def test_hard_ranker_rejection_wins_over_structural_confidence(self) -> None:
        candidate = DiscoveryCandidate.from_url(
            "https://careers.example.com/contact",
            confidence=0.98,
            evidence={
                "origin": "adaptive_dom_repeated_cluster",
                "evidence_preserving": True,
                "structural_job_grounding": True,
            },
        )

        urls, metrics = preserve_evidence_backed_urls(
            [],
            [candidate],
            ranking_metrics={
                "rejected_candidates": [
                    {
                        "url": candidate.detail_url,
                        "hard_reject": True,
                        "reasons": ["navigation:contact"],
                    }
                ]
            },
        )

        self.assertEqual(urls, [])
        self.assertEqual(metrics["adaptive_urls_rejected_hard"], 1)

    def test_page_level_jobs_word_cannot_preserve_repeated_navigation_cards(self) -> None:
        nodes = [
            dom_node("n:body", None, "body", "body|||main"),
            dom_node("n:main", "n:body", "main", "main|main||div", role="main"),
            dom_node("n:grid", "n:main", "div", "div|||article,article,article"),
        ]
        for index, (label, slug) in enumerate(
            (
                ("Salary guide", "salary-guide"),
                ("Professional services", "professional"),
                ("Permanent recruitment", "permanent-recruitment"),
            ),
            start=1,
        ):
            root = f"n:category{index}"
            nodes.extend(
                [
                    dom_node(root, "n:grid", "article", "article|article||h2,p,a", role="article"),
                    dom_node(f"{root}:h", root, "h2", "h2|heading||", text=label, role="heading"),
                    dom_node(
                        f"{root}:p",
                        root,
                        "p",
                        "p|||",
                        text="Browse workforce insights and employer services.",
                    ),
                    dom_node(
                        f"{root}:a",
                        root,
                        "a",
                        "a|link|c|",
                        text="Learn more",
                        href=f"https://careers.example.com/employers/{slug}",
                        role="link",
                        clickable=True,
                    ),
                ]
            )

        batch = DomCandidateDiscoverer().discover(
            evidence_report(nodes, title="Find Jobs and Careers")
        )
        self.assertEqual(len(batch.candidates), 3)
        self.assertTrue(
            all(
                not candidate.evidence["candidate_local_job_grounding"]
                and not candidate.evidence["evidence_preserving"]
                for candidate in batch.candidates
            )
        )

        urls, metrics = preserve_evidence_backed_urls(
            [],
            batch.candidates,
            ranking_metrics={
                "rejected_candidates": [
                    {"url": candidate.detail_url, "hard_reject": False}
                    for candidate in batch.candidates
                ]
            },
        )
        self.assertEqual(urls, [])
        self.assertEqual(metrics["adaptive_urls_rejected_weak_evidence"], 3)


class FakeCollector:
    def __init__(self, report: BrowserEvidenceReport) -> None:
        self.report = report

    async def capture(self, url: str, *, allowed_hosts):
        return self.report


class AdaptiveDomServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_access_control_status_is_reported_as_blocked_without_bypass(self) -> None:
        report = evidence_report([])
        report = report.model_copy(update={"success": False, "status_code": 403})
        service = AdaptiveDomDiscoveryService(
            BrowserSettings(),
            collector=FakeCollector(report),
        )

        batch = await service.discover(
            report.final_url,
            allowed_hosts=("careers.example.com",),
            max_candidates=10,
        )

        self.assertEqual(batch.completeness, CompletenessState.BLOCKED)
        self.assertEqual(batch.candidates, [])
        self.assertIn("no anti-bot bypass", batch.reasons[0])


class EmptyCandidateAdapter:
    async def discover_candidates(self, *args, **kwargs) -> DiscoveryBatch:
        return DiscoveryBatch(
            strategy=ScrapeStrategy.BLUEPRINT_DOM,
            completeness=CompletenessState.PARTIAL,
        )


class FailingCandidateAdapter:
    async def discover_candidates(self, *args, **kwargs) -> DiscoveryBatch:
        raise TimeoutError("listing browser timed out")


class FakeAdaptiveService:
    def __init__(self, batch: DiscoveryBatch) -> None:
        self.batch = batch
        self.calls: list[dict] = []

    async def discover(self, listing_url: str, *, allowed_hosts, max_candidates: int):
        self.calls.append(
            {
                "listing_url": listing_url,
                "allowed_hosts": tuple(allowed_hosts),
                "max_candidates": max_candidates,
            }
        )
        return self.batch


class FakeCrawler:
    async def arun(self, *, url: str, config):
        return SimpleNamespace(
            success=True,
            url=url,
            deterministic_job=JobPosting(title="Recovered Engineer", job_url=url),
            error_message=None,
        )


class AdaptiveDomOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_adapter_discovery_uses_evidence_backed_dom_fallback(self) -> None:
        hub = BlueprintHub(ROOT)
        detail_url = "https://example.com/opening?opaque=abc123"
        candidate = DiscoveryCandidate.from_url(
            detail_url,
            confidence=0.94,
            evidence={
                "origin": "adaptive_dom_repeated_cluster",
                "evidence_preserving": True,
                "structural_job_grounding": True,
            },
        )
        service = FakeAdaptiveService(
            DiscoveryBatch(
                strategy=ScrapeStrategy.BLUEPRINT_DOM,
                completeness=CompletenessState.PARTIAL,
                candidates=[candidate],
                pages_visited=1,
            )
        )
        events: list[str] = []
        orchestrator = ScrapeOrchestrator(
            blueprint=hub.get_fleet_targets()[0],
            system_config=hub.system,
            run_session_id="phase7c1-fallback-test",
            adapter=EmptyCandidateAdapter(),
            adaptive_dom_service=service,
        )

        with patch(
            "src.portals.orchestrator.detail_run_config",
            return_value=SimpleNamespace(kind="detail"),
        ), patch(
            "src.portals.orchestrator.extract_job_from_result",
            side_effect=lambda result, url: result.deterministic_job,
        ), patch("src.portals.orchestrator.close_session", new_callable=AsyncMock):
            result = await orchestrator.run_with_crawler(
                FakeCrawler(),
                options=ScrapeExecutionOptions(
                    prefer_platform_api=False,
                    enable_adaptive_dom_fallback=True,
                    adaptive_dom_max_candidates=25,
                ),
                hooks=ScrapeOrchestratorHooks(
                    rank_discovered_urls=lambda urls: (
                        [],
                        {
                            "strategy": "fixture_rejects_unfamiliar_shapes",
                            "input_urls": len(urls),
                            "selected_urls": 0,
                            "rejected_urls": len(urls),
                        },
                    ),
                    on_event=lambda event, payload: events.append(event),
                ),
            )

        self.assertEqual(result.discovered_job_urls, [detail_url])
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0].title, "Recovered Engineer")
        self.assertEqual(result.acquisition["adaptive_dom"]["adaptive_urls_preserved"], 1)
        self.assertEqual(service.calls[0]["max_candidates"], 25)
        self.assertIn("adaptive_dom_discovery_complete", events)

    async def test_adapter_timeout_also_reaches_adaptive_dom_fallback(self) -> None:
        hub = BlueprintHub(ROOT)
        detail_url = "https://example.com/opening?opaque=timeout-recovery"
        candidate = DiscoveryCandidate.from_url(
            detail_url,
            confidence=0.94,
            evidence={
                "origin": "adaptive_dom_repeated_cluster",
                "evidence_preserving": True,
                "structural_job_grounding": True,
            },
        )
        service = FakeAdaptiveService(
            DiscoveryBatch(
                strategy=ScrapeStrategy.BLUEPRINT_DOM,
                candidates=[candidate],
            )
        )
        events: list[str] = []
        orchestrator = ScrapeOrchestrator(
            blueprint=hub.get_fleet_targets()[0],
            system_config=hub.system,
            run_session_id="phase7c1-timeout-fallback-test",
            adapter=FailingCandidateAdapter(),
            adaptive_dom_service=service,
        )

        with patch(
            "src.portals.orchestrator.detail_run_config",
            return_value=SimpleNamespace(kind="detail"),
        ), patch(
            "src.portals.orchestrator.extract_job_from_result",
            side_effect=lambda result, url: result.deterministic_job,
        ), patch("src.portals.orchestrator.close_session", new_callable=AsyncMock):
            result = await orchestrator.run_with_crawler(
                FakeCrawler(),
                options=ScrapeExecutionOptions(
                    prefer_platform_api=False,
                    enable_adaptive_dom_fallback=True,
                ),
                hooks=ScrapeOrchestratorHooks(
                    on_event=lambda event, payload: events.append(event)
                ),
            )

        self.assertEqual(result.discovered_job_urls, [detail_url])
        self.assertEqual(len(result.jobs), 1)
        self.assertIn("adapter_discovery_failed_adaptive_fallback", events)


class FakeAsyncWebCrawler:
    def __init__(self, *, config) -> None:
        self.config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def arun(self, *, url: str, config):
        html = (
            "<html><body><div id='career-root'></div>"
            "<script src='/runtime.js'></script><script src='/app.js'></script>"
            "</body></html>"
        )
        return SimpleNamespace(
            success=True,
            url=url,
            html=html,
            cleaned_html=html,
            markdown="",
            text="",
            links={},
            error_message=None,
        )


class JavaScriptShellProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_shell_without_api_hint_is_forwarded_to_adaptive_dom_lane(self) -> None:
        certifier = PortalFleetCertifier(
            root=ROOT,
            output_dir=ROOT / "data" / "phase7c1-probe-test",
            options=CertificationOptions(max_jobs=1),
        )
        entry = PortalInventoryEntry(
            source_id="cert_careers_example_com_phase7c1",
            display_name="Example Careers",
            listing_url="https://careers.example.com/careers",
            source_row=1,
        )
        fake_crawl4ai = types.ModuleType("crawl4ai")
        fake_crawl4ai.AsyncWebCrawler = FakeAsyncWebCrawler
        empty_outcome = AcquisitionOutcome(selected=None, attempts=[])

        with patch.dict(sys.modules, {"crawl4ai": fake_crawl4ai}), patch(
            "src.portals.safety._validate_host_is_public",
            return_value=None,
        ), patch(
            "src.portals.certification.build_browser_config",
            return_value=SimpleNamespace(kind="browser"),
        ), patch(
            "src.portals.certification.listing_run_config",
            return_value=SimpleNamespace(kind="listing"),
        ), patch.object(
            certifier,
            "_acquire",
            new=AsyncMock(side_effect=[empty_outcome, empty_outcome]),
        ):
            probe = await certifier._probe(entry)

        self.assertEqual(probe.effective_listing_url, entry.listing_url)
        self.assertEqual(probe.detection.surface_kind, "javascript_shell")
        self.assertIsNone(probe.acquisition_outcome.selected)


if __name__ == "__main__":
    unittest.main()

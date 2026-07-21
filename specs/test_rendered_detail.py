from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.blueprint_hub import BlueprintHub
from src.crawl.browser_evidence import BrowserEvidenceReport
from src.crawl.dom_snapshot import DomNodeEvidence, FrameDomSnapshot
from src.portals.contracts import DiscoveryBatch, ScrapeStrategy
from src.portals.orchestrator import (
    ScrapeExecutionOptions,
    ScrapeOrchestrator,
    ScrapeOrchestratorHooks,
)
from src.portals.rendered_detail import RenderedDetailExtractor, RenderedDetailResult
from src.schemas import JobPosting


def node(
    token: str,
    parent: str | None,
    tag: str,
    *,
    text: str | None = None,
    role: str | None = None,
    href: str | None = None,
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
        clickable=clickable,
        attributes=attributes or {},
        structural_signature=f"{tag}|{role or ''}|{'c' if clickable else ''}|",
    )


def report(nodes: list[DomNodeEvidence], *, title: str = "Careers") -> BrowserEvidenceReport:
    now = datetime.now(timezone.utc).isoformat()
    url = "https://careers.example.com/opening?opaque=42"
    return BrowserEvidenceReport(
        requested_url=url,
        final_url=url,
        success=True,
        status_code=200,
        title=title,
        started_at=now,
        completed_at=now,
        frames=[FrameDomSnapshot(frame_id="f0", frame_url=url, nodes=nodes)],
    )


def listing_nodes() -> list[DomNodeEvidence]:
    values = [
        node("n:body", None, "body"),
        node("n:main", "n:body", "main", role="main"),
        node("n:list", "n:main", "div"),
    ]
    for index, title in enumerate(("Platform Engineer", "Data Analyst", "QA Lead"), start=1):
        root = f"n:card{index}"
        values.extend(
            [
                node(root, "n:list", "article", role="article"),
                node(f"{root}:h", root, "h2", text=title, role="heading"),
                node(f"{root}:l", root, "span", text=f"City {index}, CA"),
                node(
                    f"{root}:b",
                    root,
                    "button",
                    text="View role",
                    role="button",
                    clickable=True,
                ),
            ]
        )
    return values


class RenderedDetailExtractorTests(unittest.TestCase):
    def test_semantic_detail_extracts_without_xpath_or_json_ld(self) -> None:
        nodes = [
            node("n:body", None, "body"),
            node("n:main", "n:body", "main", role="main"),
            node(
                "n:title",
                "n:main",
                "h1",
                text="Senior Platform Engineer",
                role="heading",
            ),
            node("n:location", "n:main", "div", text="Location: Denver, CO"),
            node("n:ref", "n:main", "div", text="Requisition: ENG-1042"),
            node("n:r-heading", "n:main", "h2", text="Responsibilities", role="heading"),
            node(
                "n:r-body",
                "n:main",
                "p",
                text=(
                    "Design reliable distributed services, improve production observability, "
                    "lead incident reviews, and partner with application teams to deliver "
                    "resilient infrastructure across multiple cloud environments."
                ),
            ),
            node("n:q-heading", "n:main", "h2", text="Required experience", role="heading"),
            node(
                "n:q-body",
                "n:main",
                "p",
                text="Five years of Python, Kubernetes, networking, and public cloud experience.",
            ),
            node(
                "n:apply",
                "n:main",
                "a",
                text="Apply now",
                role="link",
                href="https://careers.example.com/apply/ENG-1042",
                clickable=True,
            ),
        ]

        result = RenderedDetailExtractor().extract(
            report(nodes, title="Senior Platform Engineer"),
            fallback_url="https://careers.example.com/opening?opaque=42",
        )

        self.assertIsNotNone(result.job)
        assert result.job is not None
        self.assertEqual(result.job.title, "Senior Platform Engineer")
        self.assertEqual(result.job.location_text, "Denver, CO")
        self.assertEqual(result.job.job_reference, "ENG-1042")
        self.assertIn("distributed services", result.job.summary)
        self.assertEqual(result.reason, "semantic_rendered_detail")

    def test_listing_cards_alone_are_not_misclassified_as_a_detail(self) -> None:
        result = RenderedDetailExtractor().extract(
            report(listing_nodes(), title="Search Jobs"),
            fallback_url="https://careers.example.com/jobs",
        )

        self.assertIsNone(result.job)
        self.assertEqual(result.reason, "missing_job_detail_signals")

    def test_same_url_modal_uses_only_post_click_detail_delta(self) -> None:
        before = report(listing_nodes(), title="Search Jobs")
        after_nodes = [*listing_nodes()]
        after_nodes.extend(
            [
                node("n:dialog", "n:body", "section", role="dialog"),
                # The title already existed on the listing. The extractor must
                # ground it in the rendered page while taking summary evidence
                # only from the newly exposed modal.
                node(
                    "n:dialog:title",
                    "n:dialog",
                    "h2",
                    text="Platform Engineer",
                    role="heading",
                ),
                node("n:dialog:location", "n:dialog", "div", text="Location: Austin, TX"),
                node("n:dialog:ref", "n:dialog", "div", text="Job ID: PE-77"),
                node(
                    "n:dialog:description",
                    "n:dialog",
                    "p",
                    text=(
                        "Job description: Build and operate the internal developer platform, "
                        "automate delivery workflows, own reliability improvements, and work "
                        "with engineering teams to define secure deployment standards."
                    ),
                ),
                node(
                    "n:dialog:requirements",
                    "n:dialog",
                    "p",
                    text="Requirements: production Kubernetes and infrastructure automation experience.",
                ),
            ]
        )
        after = report(after_nodes, title="Search Jobs")

        result = RenderedDetailExtractor().extract(
            after,
            fallback_url="https://careers.example.com/jobs",
            title_hint="Platform Engineer",
            baseline_report=before,
        )

        self.assertIsNotNone(result.job)
        assert result.job is not None
        self.assertEqual(result.job.title, "Platform Engineer")
        self.assertEqual(result.job.job_reference, "PE-77")
        self.assertTrue(result.metrics["modal_delta"])
        self.assertGreater(result.metrics["delta_chars"], 80)


class _OneUrlAdapter:
    def __init__(self, url: str) -> None:
        self.url = url

    async def discover_candidates(self, *args, **kwargs):
        return DiscoveryBatch.from_urls([self.url], strategy=ScrapeStrategy.BLUEPRINT_DOM)


class _CrawlerWithoutStructuredJob:
    def __init__(self) -> None:
        self.calls = 0

    async def arun(self, *, url: str, config):
        self.calls += 1
        return SimpleNamespace(
            success=True,
            url=url,
            html="<html><body><div id='app'></div></body></html>",
            cleaned_html="",
            markdown="",
            error_message=None,
        )


class _RenderedService:
    def __init__(self, job: JobPosting) -> None:
        self.job = job
        self.calls: list[dict] = []

    async def extract(self, url, *, allowed_hosts, title_hint=None, location_hint=None):
        self.calls.append(
            {
                "url": url,
                "allowed_hosts": tuple(allowed_hosts),
                "title_hint": title_hint,
                "location_hint": location_hint,
            }
        )
        return RenderedDetailResult(
            job=self.job,
            reason="semantic_rendered_detail",
            confidence=0.9,
            metrics={"detail_signals": 2},
        )


class RenderedDetailOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_rendered_fallback_runs_before_disabled_llm(self) -> None:
        root = Path(__file__).resolve().parents[1]
        hub = BlueprintHub(root)
        url = "https://example.com/opening?opaque=rendered-1"
        job = JobPosting(
            title="Rendered Platform Engineer",
            job_url=url,
            location_text="Remote",
            summary=(
                "Build reliable platform services and own production automation across "
                "cloud infrastructure and application delivery systems."
            ),
        )
        service = _RenderedService(job)
        crawler = _CrawlerWithoutStructuredJob()
        events: list[str] = []
        orchestrator = ScrapeOrchestrator(
            blueprint=hub.get_fleet_targets()[0],
            system_config=hub.system,
            run_session_id="phase7c3a-rendered-fallback",
            adapter=_OneUrlAdapter(url),
            rendered_detail_service=service,
        )

        with patch(
            "src.portals.orchestrator.detail_run_config",
            return_value=SimpleNamespace(kind="detail"),
        ), patch(
            "src.portals.orchestrator.close_session",
            new_callable=AsyncMock,
        ):
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(
                    prefer_platform_api=False,
                    enable_rendered_detail_fallback=True,
                    max_jobs=1,
                ),
                hooks=ScrapeOrchestratorHooks(
                    should_attempt_llm=lambda result, source_url: (
                        False,
                        "LLM disabled in certification",
                    ),
                    on_event=lambda event, payload: events.append(event),
                ),
            )

        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0].title, "Rendered Platform Engineer")
        self.assertEqual(len(service.calls), 1)
        self.assertIn("rendered_detail_start", events)
        self.assertIn("extract_saved", events)
        self.assertNotIn("llm_fallback_start", events)


if __name__ == "__main__":
    unittest.main()

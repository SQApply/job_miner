from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from src.crawl.browser_evidence import BrowserEvidenceReport, BrowserEvidenceSession
from src.crawl.dom_snapshot import DomNodeEvidence, FrameDomSnapshot
from src.portals.dom_discovery import DomCandidateDiscoverer
from src.portals.adaptive_dom import AdaptiveDomDiscoveryService
from src.portals.live_interaction import LinklessInteractionOptions, LiveLinklessResolver
from src.schemas import BrowserSettings


def node(
    token: str,
    parent: str | None,
    tag: str,
    signature: str,
    *,
    text: str | None = None,
    role: str | None = None,
    clickable: bool = False,
) -> DomNodeEvidence:
    return DomNodeEvidence(
        node_token=token,
        parent_token=parent,
        depth=token.count(":"),
        tag=tag,
        role=role,
        text=text,
        clickable=clickable,
        structural_signature=signature,
    )


def linkless_report() -> BrowserEvidenceReport:
    nodes = [
        node("f0:n1", None, "body", "body|||main"),
        node("f0:n2", "f0:n1", "main", "main|main||div", role="main"),
        node("f0:n3", "f0:n2", "div", "div|||article,article,article"),
    ]
    for index, title in enumerate(("Engineer I", "Engineer II", "Engineer III"), start=1):
        root = f"f0:r{index}"
        nodes.extend(
            [
                node(root, "f0:n3", "article", "article|article||h2,div,button", role="article"),
                node(f"{root}:h", root, "h2", "h2|heading||", text=title, role="heading"),
                node(f"{root}:l", root, "div", "div|||", text=f"City {index}"),
                node(
                    f"{root}:b",
                    root,
                    "button",
                    "button|button|c|",
                    text="View role",
                    role="button",
                    clickable=True,
                ),
            ]
        )
    now = datetime.now(timezone.utc).isoformat()
    return BrowserEvidenceReport(
        requested_url="https://careers.example.com/careers",
        final_url="https://careers.example.com/careers",
        success=True,
        status_code=200,
        title="Open Jobs",
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


class FakeFrame:
    def __init__(self, page, target_url: str) -> None:
        self.page = page
        self.url = "https://careers.example.com/careers"
        self.target_url = target_url
        self.clicked_tokens: list[str] = []

    async def evaluate(self, script, token):
        self.clicked_tokens.append(token)
        self.page.url = self.target_url
        return {"clicked": True, "reason": "element_click"}


class FakePage:
    def __init__(self, target_url: str) -> None:
        self.url = "https://careers.example.com/careers"
        self.frames = [FakeFrame(self, target_url)]

    async def wait_for_timeout(self, milliseconds):
        return None

    async def wait_for_load_state(self, *args, **kwargs):
        return None


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]


class FakeCollector:
    def __init__(self, report: BrowserEvidenceReport) -> None:
        self.report = report

    async def capture_page(self, page, url, *, allowed_hosts):
        page.url = self.report.final_url
        return self.report


class SessionCollector(FakeCollector):
    def __init__(self, report: BrowserEvidenceReport, page: FakePage) -> None:
        super().__init__(report)
        self.page = page
        self.context = FakeContext(page)

    @asynccontextmanager
    async def capture_session(self, url, *, allowed_hosts):
        yield BrowserEvidenceSession(
            collector=self,
            context=self.context,
            page=self.page,
            report=self.report,
            allowed_hosts=tuple(allowed_hosts),
        )


class LiveLinklessResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_node_navigation_becomes_a_url_candidate_without_selector(self) -> None:
        report = linkless_report()
        original = DomCandidateDiscoverer().discover(report).linkless_candidates[0]
        page = FakePage("https://careers.example.com/opening?opaque=engineer-1")
        collector = FakeCollector(report)
        session = BrowserEvidenceSession(
            collector=collector,
            context=FakeContext(page),
            page=page,
            report=report,
            allowed_hosts=("careers.example.com",),
        )

        with patch("src.portals.safety._validate_host_is_public", return_value=None):
            batch = await LiveLinklessResolver(
                options=LinklessInteractionOptions(settle_time_ms=0)
            ).resolve(
                session,
                [original],
                listing_url=report.final_url,
            )

        self.assertEqual(batch.discovered_urls, [page.frames[0].target_url])
        self.assertEqual(batch.metrics["resolved"], 1)
        self.assertEqual(
            batch.candidates[0].evidence["origin"],
            "adaptive_dom_linkless_interaction",
        )
        self.assertTrue(batch.candidates[0].evidence["evidence_preserving"])
        self.assertEqual(len(page.frames[0].clicked_tokens), 1)

    def test_query_only_filter_navigation_is_not_a_detail_identity(self) -> None:
        self.assertFalse(
            LiveLinklessResolver._is_openable_detail_route(
                "https://careers.example.com/jobs",
                "https://careers.example.com/jobs?page=2&sort=date",
                source_job_id=None,
            )
        )
        self.assertTrue(
            LiveLinklessResolver._is_openable_detail_route(
                "https://careers.example.com/jobs",
                "https://careers.example.com/jobs?requisition=REQ-9",
                source_job_id="REQ-9",
            )
        )

    async def test_adaptive_service_resolves_linkless_candidate_before_context_closes(self) -> None:
        report = linkless_report()
        page = FakePage("https://careers.example.com/opening/engineer-1")
        service = AdaptiveDomDiscoveryService(
            BrowserSettings(),
            collector=SessionCollector(report, page),
            interaction_options=LinklessInteractionOptions(
                max_interactions=1,
                settle_time_ms=0,
            ),
        )

        with patch("src.portals.safety._validate_host_is_public", return_value=None):
            batch = await service.discover(
                report.final_url,
                allowed_hosts=("careers.example.com",),
                max_candidates=10,
            )

        self.assertEqual(batch.discovered_urls, [page.frames[0].target_url])
        self.assertEqual(batch.metrics["linkless_interaction"]["resolved"], 1)
        self.assertEqual(len(batch.linkless_candidates), 2)


if __name__ == "__main__":
    unittest.main()

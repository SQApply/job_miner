from __future__ import annotations

import unittest
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.crawl.browser_evidence import BrowserEvidenceReport
from src.crawl.dom_snapshot import DomNodeEvidence, FrameDomSnapshot
from src.portals.acquisition import AcquisitionOutcome
from src.portals.adaptive_dom import AdaptiveDomDiscoveryService
from src.portals.certification import (
    CertificationOptions,
    PortalFleetCertifier,
    PortalInventoryEntry,
    _empty_acquisition_listing_handoff,
    _owned_sibling_hosts,
)
from src.portals.contracts import DiscoveryCandidate, DiscoveryCandidateKind
from src.portals.dom_discovery import preserve_evidence_backed_urls
from src.portals.job_evidence import job_title_rejection_reason
from src.portals.live_interaction import LinklessInteractionOptions, LiveLinklessResolver
from src.portals.rendered_detail import RenderedDetailExtractor
from src.portals.url_intelligence import (
    assess_certification_job,
    assess_job_candidate_url,
)
from src.schemas import BrowserSettings, JobPosting


def node(
    token: str,
    parent: str | None,
    tag: str,
    signature: str,
    *,
    text: str | None = None,
    role: str | None = None,
    href: str | None = None,
    clickable: bool = False,
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
        structural_signature=signature,
    )


def report(url: str, nodes: list[DomNodeEvidence], *, title: str) -> BrowserEvidenceReport:
    now = datetime.now(timezone.utc).isoformat()
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


class Phase7C3BUrlAndTruthTests(unittest.TestCase):
    def test_spa_hash_detail_is_high_confidence_but_hash_listing_is_not(self) -> None:
        listing = "http://careerportal.compri.com/#/jobs"
        detail = "http://careerportal.compri.com/#/jobs/26431"

        detail_assessment = assess_job_candidate_url(detail, listing_url=listing)
        listing_assessment = assess_job_candidate_url(listing)

        self.assertGreaterEqual(detail_assessment.score, 8)
        self.assertIn("numeric_job_path", detail_assessment.reasons)
        self.assertLess(listing_assessment.score, 8)

    def test_requisition_slug_detail_is_ranked_without_site_specific_rules(self) -> None:
        assessment = assess_job_candidate_url(
            "https://careers.example.com/jobs/cloud-engineer-247325",
            listing_url="https://careers.example.com/jobs",
        )
        self.assertGreaterEqual(assessment.score, 8)
        self.assertIn("requisition_slug_detail_path", assessment.reasons)

    def test_date_section_and_marketing_titles_are_rejected(self) -> None:
        self.assertEqual(
            job_title_rejection_reason("Added - 07/17/26"),
            "date_label_title",
        )
        self.assertEqual(
            job_title_rejection_reason("Must-Haves"),
            "section_heading_title",
        )
        valid, reason = assess_certification_job(
            JobPosting(
                title=(
                    "randstad connects you with reliable, job-ready talent across "
                    "key operational areas"
                ),
                job_url="https://www.randstadusa.com/employers/operational/",
                summary="Learn more about workforce solutions for employers.",
            ),
            "https://www.randstadusa.com/employers/operational/",
        )
        self.assertFalse(valid)
        self.assertIn("non-role title", reason)

    def test_certification_still_accepts_a_grounded_high_confidence_role(self) -> None:
        valid, reason = assess_certification_job(
            JobPosting(
                title="Senior Data Engineer",
                job_url="https://careers.example.com/jobs/247325",
            ),
            "https://careers.example.com/jobs/247325",
        )
        self.assertTrue(valid, reason)
        self.assertIn("quality_score", reason)

    def test_navigation_route_cannot_override_ranker_with_cluster_confidence(self) -> None:
        url = "https://www.randstadusa.com/employers/operational"
        candidate = DiscoveryCandidate.from_url(
            url,
            confidence=0.96,
            evidence={
                "origin": "adaptive_dom_repeated_cluster",
                "evidence_preserving": True,
                "structural_job_grounding": True,
            },
        )
        selected, metrics = preserve_evidence_backed_urls(
            [],
            [candidate],
            ranking_metrics={
                "rejected_candidates": [
                    {
                        "url": url,
                        "hard_reject": False,
                        "reasons": ["navigation_tokens:employers"],
                    }
                ]
            },
        )
        self.assertEqual(selected, [])
        self.assertEqual(metrics["adaptive_urls_rejected_navigation"], 1)


class Phase7C3BRenderedDetailTests(unittest.TestCase):
    def test_role_title_beats_date_and_section_headings(self) -> None:
        url = "http://careerportal.compri.com/#/jobs/26431"
        nodes = [
            node("n:body", None, "body", "body|||main"),
            node("n:main", "n:body", "main", "main|main||", role="main"),
            node(
                "n:date",
                "n:main",
                "h2",
                "h2|heading||",
                text="Added - 07/17/26",
                role="heading",
            ),
            node(
                "n:title",
                "n:main",
                "h1",
                "h1|heading||",
                text="Senior Project Manager",
                role="heading",
            ),
            node("n:loc", "n:main", "div", "div|||", text="Location: Denver, CO"),
            node(
                "n:must",
                "n:main",
                "h2",
                "h2|heading||",
                text="Must-Haves",
                role="heading",
            ),
            node(
                "n:bodytext",
                "n:main",
                "p",
                "p|||",
                text=(
                    "Must-Haves: Lead complex technology programs, manage delivery risks, "
                    "coordinate engineering teams, maintain executive status reporting, and "
                    "drive reliable outcomes across several concurrent client initiatives."
                ),
            ),
        ]
        result = RenderedDetailExtractor().extract(
            report(url, nodes, title="Senior Project Manager"),
            fallback_url=url,
            title_hint="Added - 07/17/26",
        )
        self.assertIsNotNone(result.job)
        assert result.job is not None
        self.assertEqual(result.job.title, "Senior Project Manager")
        self.assertEqual(result.job.job_reference, "26431")
        self.assertEqual(result.job.location_text, "Denver, CO")

    def test_job_ready_phrase_is_not_invented_as_a_reference(self) -> None:
        url = "https://careers.example.com/opening/opaque"
        nodes = [
            node("n:body", None, "body", "body|||main"),
            node("n:main", "n:body", "main", "main|main||", role="main"),
            node(
                "n:title",
                "n:main",
                "h1",
                "h1|heading||",
                text="Operations Specialist",
                role="heading",
            ),
            node(
                "n:summary",
                "n:main",
                "p",
                "p|||",
                text=(
                    "Responsibilities: Coordinate daily production activity, document quality "
                    "controls, resolve scheduling issues, and support job-ready teams while "
                    "maintaining accurate operational records and safety standards."
                ),
            ),
        ]
        result = RenderedDetailExtractor().extract(
            report(url, nodes, title="Operations Specialist"),
            fallback_url=url,
        )
        self.assertIsNotNone(result.job)
        assert result.job is not None
        self.assertIsNone(result.job.job_reference)


class Phase7C3BRouteTests(unittest.TestCase):
    def test_functional_jobs_host_allows_only_its_branded_sibling_scope(self) -> None:
        self.assertEqual(
            _owned_sibling_hosts("jobs.insightglobal.com"),
            [
                "insightglobal.com",
                "www.insightglobal.com",
                "career.insightglobal.com",
                "careers.insightglobal.com",
                "apply.insightglobal.com",
                "hiring.insightglobal.com",
                "recruiting.insightglobal.com",
            ],
        )
        self.assertEqual(_owned_sibling_hosts("unrelated.example.com"), [])
        self.assertEqual(_owned_sibling_hosts("jobs.co.uk"), [])
        self.assertEqual(_owned_sibling_hosts("jobs.github.io"), [])

    def test_empty_acquisition_route_is_handed_to_the_browser_lane(self) -> None:
        outcome = AcquisitionOutcome(
            selected=None,
            attempts=[
                {
                    "platform": "public_html",
                    "status": "empty",
                    "trusted_hosts": ["www.belcan.com"],
                    "metadata": {
                        "listing_url": "https://www.belcan.com/employment/",
                        "route_resolution_chain": [],
                    },
                }
            ],
        )
        with patch("src.portals.safety._validate_host_is_public", return_value=None):
            handoff = _empty_acquisition_listing_handoff(
                outcome,
                source_url="http://www.belcan.com/",
            )
        self.assertIsNotNone(handoff)
        assert handoff is not None
        self.assertEqual(handoff[0], "https://www.belcan.com/employment/")
        self.assertEqual(
            handoff[2]["strategy"],
            "empty_acquisition_listing_handoff",
        )


class _EmptyRouteRegistry:
    def __init__(self, outcome: AcquisitionOutcome) -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    async def acquire(self, context):
        self.calls.append(context.listing_url)
        return self.outcome


class _RouteProbeCrawler:
    calls: list[str] = []

    def __init__(self, *, config) -> None:
        self.config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def arun(self, *, url: str, config):
        self.calls.append(url)
        html = (
            "<html><body><main><h1>Employment Opportunities</h1>"
            "<p>Browse current positions and join our engineering teams.</p>"
            "</main></body></html>"
        )
        return SimpleNamespace(
            success=True,
            url=url,
            html=html,
            cleaned_html=html,
            markdown="Employment Opportunities Current positions",
            text="Employment Opportunities Current positions",
            links={},
            error_message=None,
        )


class Phase7C3BProbeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_crawls_the_empty_acquisition_handoff_not_the_homepage(self) -> None:
        outcome = AcquisitionOutcome(
            selected=None,
            attempts=[
                {
                    "platform": "public_html",
                    "status": "empty",
                    "trusted_hosts": ["www.belcan.com"],
                    "metadata": {
                        "listing_url": "https://www.belcan.com/employment/",
                        "route_resolution_chain": [],
                    },
                }
            ],
        )
        registry = _EmptyRouteRegistry(outcome)
        root = Path(__file__).resolve().parents[1]
        certifier = PortalFleetCertifier(
            root=root,
            output_dir=root / "data" / "phase7c3b-probe-test",
            options=CertificationOptions(max_jobs=1),
            acquisition_registry=registry,
        )
        entry = PortalInventoryEntry(
            source_id="cert_www_belcan_com_phase7c3b",
            display_name="Belcan",
            listing_url="http://www.belcan.com/",
            source_row=1,
        )
        fake_crawl4ai = types.ModuleType("crawl4ai")
        fake_crawl4ai.AsyncWebCrawler = _RouteProbeCrawler
        _RouteProbeCrawler.calls = []
        with patch.dict(sys.modules, {"crawl4ai": fake_crawl4ai}), patch(
            "src.portals.safety._validate_host_is_public",
            return_value=None,
        ), patch(
            "src.portals.certification.build_browser_config",
            return_value=SimpleNamespace(kind="browser"),
        ), patch(
            "src.portals.certification.listing_run_config",
            return_value=SimpleNamespace(kind="listing"),
        ):
            probe = await certifier._probe(entry)

        self.assertEqual(
            _RouteProbeCrawler.calls[0],
            "https://www.belcan.com/employment/",
        )
        self.assertEqual(
            probe.effective_listing_url,
            "https://www.belcan.com/employment/",
        )
        self.assertEqual(
            probe.route_resolution["selected"]["url"],
            "https://www.belcan.com/employment/",
        )


class _ExpansionCollector:
    def __init__(
        self,
        initial: BrowserEvidenceReport,
        expanded: BrowserEvidenceReport,
    ) -> None:
        self.initial = initial
        self.expanded = expanded
        self.calls: list[str] = []

    async def capture(self, url: str, *, allowed_hosts):
        self.calls.append(url)
        return self.initial if len(self.calls) == 1 else self.expanded


class Phase7C3BExpansionTests(unittest.IsolatedAsyncioTestCase):
    def test_weak_click_threshold_matches_dom_discovery_floor(self) -> None:
        self.assertEqual(
            LinklessInteractionOptions().verification_minimum_confidence,
            0.62,
        )

    async def test_bounded_second_level_listing_expansion_finds_detail_rows(self) -> None:
        listing = "https://careers.example.com/jobs"
        category = "https://careers.example.com/jobs/q-engineering"
        initial_nodes = [
            node("i:body", None, "body", "body|||main"),
            node("i:main", "i:body", "main", "main|main||", role="main"),
            node(
                "i:category",
                "i:main",
                "a",
                "a|link|c|",
                text="Engineering jobs",
                role="link",
                href=category,
                clickable=True,
            ),
        ]
        expanded_nodes = [
            node("e:body", None, "body", "body|||main"),
            node("e:main", "e:body", "main", "main|main||", role="main"),
            node("e:list", "e:main", "div", "div|||article,article,article"),
        ]
        for index, title in enumerate(
            ("Cloud Engineer", "Data Engineer", "QA Engineer"),
            start=1,
        ):
            root = f"e:r{index}"
            expanded_nodes.extend(
                [
                    node(root, "e:list", "article", "article|article||h2,p,a", role="article"),
                    node(
                        f"{root}:h",
                        root,
                        "h2",
                        "h2|heading||",
                        text=title,
                        role="heading",
                    ),
                    node(f"{root}:l", root, "p", "p|||", text="Denver, CO"),
                    node(
                        f"{root}:a",
                        root,
                        "a",
                        "a|link|c|",
                        text="View job",
                        role="link",
                        href=f"https://careers.example.com/jobs/{10000 + index}",
                        clickable=True,
                    ),
                ]
            )
        collector = _ExpansionCollector(
            report(listing, initial_nodes, title="Search Jobs"),
            report(category, expanded_nodes, title="Engineering Jobs"),
        )
        service = AdaptiveDomDiscoveryService(
            BrowserSettings(),
            collector=collector,
        )
        batch = await service.discover(
            listing,
            allowed_hosts=("careers.example.com",),
            max_candidates=10,
        )
        self.assertEqual(len(collector.calls), 2)
        self.assertEqual(batch.metrics["listing_expansion"]["captured_routes"], 1)
        self.assertEqual(
            [url for url in batch.discovered_urls if url != category],
            [
                "https://careers.example.com/jobs/10001",
                "https://careers.example.com/jobs/10002",
                "https://careers.example.com/jobs/10003",
            ],
        )

    def test_direct_urls_are_bounded_before_synthetic_modal_identity(self) -> None:
        direct = DiscoveryCandidate.from_url(
            "https://careers.example.com/jobs/10001",
            confidence=0.8,
            evidence={"origin": "adaptive_dom_repeated_cluster"},
        )
        modal_url = "https://careers.example.com/jobs#job/website"
        modal_job = JobPosting(title="Website", job_url=modal_url)
        modal = DiscoveryCandidate(
            candidate_id="modal_false",
            kind=DiscoveryCandidateKind.MODAL,
            detail_url=modal_url,
            title_hint="Website",
            confidence=0.99,
            evidence={
                "origin": "adaptive_dom_linkless_interaction",
                "synthetic_detail_identity": True,
                "rendered_detail_verified": True,
            },
            preextracted_job=modal_job,
        )
        selected = AdaptiveDomDiscoveryService._select_candidates(
            [modal, direct],
            max_candidates=1,
        )
        self.assertEqual(selected, [direct])

    def test_verified_weak_click_becomes_evidence_preserving(self) -> None:
        original = DiscoveryCandidate(
            candidate_id="weak",
            kind=DiscoveryCandidateKind.DOM_CLICK,
            title_hint="Cloud Engineer",
            node_token="f0:n1",
            confidence=0.8,
            evidence={
                "origin": "adaptive_dom_repeated_cluster",
                "structural_job_grounding": False,
                "evidence_preserving": False,
            },
        )
        job = JobPosting(
            title="Cloud Engineer",
            job_url="https://careers.example.com/jobs/10001",
        )
        resolved = LiveLinklessResolver._resolved_candidate(
            original,
            original,
            detail_url="https://careers.example.com/jobs/10001",
            interaction_kind="navigation",
            preextracted_job=job,
        )
        self.assertTrue(resolved.evidence["rendered_detail_verified"])
        self.assertTrue(resolved.evidence["evidence_preserving"])


if __name__ == "__main__":
    unittest.main()

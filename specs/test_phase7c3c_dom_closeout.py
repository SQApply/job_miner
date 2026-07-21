from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from src.crawl.browser_evidence import (
    BrowserEvidenceCollector,
    BrowserEvidenceReport,
    BrowserEvidenceSession,
    NetworkJsonEvidence,
)
from src.crawl.dom_snapshot import DomNodeEvidence, FrameDomSnapshot
from src.portals.dom_discovery import DomCandidateDiscoverer
from src.portals.job_evidence import job_title_context_rejection_reason
from src.portals.json_discovery import JsonCandidateDiscoverer, JsonDiscoveryOptions
from src.portals.live_interaction import LinklessInteractionOptions, LiveLinklessResolver
from src.portals.orchestrator import deduplicate_extracted_jobs
from src.portals.rendered_detail import RenderedDetailExtractor
from src.portals.route_resolver import resolve_listing_route
from src.portals.url_intelligence import assess_certification_job
from src.schemas import BrowserSettings, JobPosting


def _node(
    token: str,
    parent: str | None,
    tag: str,
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
        structural_signature=f"{tag}|{role or ''}|{'c' if clickable else ''}|",
    )


def _report(
    url: str,
    *,
    nodes: list[DomNodeEvidence] | None = None,
    network_json: list[NetworkJsonEvidence] | None = None,
    title: str = "Open jobs",
) -> BrowserEvidenceReport:
    now = datetime.now(timezone.utc).isoformat()
    return BrowserEvidenceReport(
        requested_url=url,
        final_url=url,
        success=True,
        status_code=200,
        title=title,
        started_at=now,
        completed_at=now,
        frames=[
            FrameDomSnapshot(
                frame_id="f0",
                frame_url=url,
                nodes=nodes or [],
            )
        ],
        network_json=network_json or [],
    )


def _network_job(
    *,
    title: str = "Engineer I",
    job_id: str = "REQ-1",
) -> NetworkJsonEvidence:
    payload = {
        "jobs": [
            {
                "jobTitle": title,
                "jobId": job_id,
                "location": "Remote",
                "company": "Example Engineering",
                "description": (
                    "Job description: Design reliable systems, investigate production "
                    "issues, automate delivery workflows, document technical decisions, "
                    "and collaborate with engineering teams on secure releases."
                ),
            }
        ]
    }
    body = json.dumps(payload).encode("utf-8")
    return NetworkJsonEvidence(
        response_id="r0001",
        url="https://careers.example.com/api/opening/REQ-1",
        method="GET",
        status=200,
        resource_type="fetch",
        content_type="application/json",
        captured_body_bytes=len(body),
        body_sha256="a" * 64,
        payload=payload,
    )


class Phase7C3CTitleTruthTests(unittest.TestCase):
    def test_related_jobs_context_proves_a_category_is_not_the_role_title(self) -> None:
        reason = job_title_context_rejection_reason(
            "Accounting / Finance",
            "AP/AR Specialist Related Accounting / Finance Jobs Plant Controller",
        )
        self.assertEqual(reason, "related_jobs_taxonomy_title")
        self.assertIsNone(
            job_title_context_rejection_reason(
                "Python / Salesforce Developer",
                "Job Description Build and maintain integration services.",
            )
        )

    def test_rendered_extractor_selects_role_instead_of_related_category(self) -> None:
        url = "https://careers.example.com/opening/26312"
        nodes = [
            _node("n:body", None, "body"),
            _node("n:main", "n:body", "main", role="main"),
            _node("n:date", "n:main", "h2", text="Added - 05/05/26", role="heading"),
            _node("n:title", "n:main", "h2", text="AP/AR Specialist", role="heading"),
            _node("n:type", "n:main", "div", text="Contract to Hire"),
            _node(
                "n:category",
                "n:main",
                "h1",
                text="Accounting / Finance",
                role="heading",
            ),
            _node(
                "n:related",
                "n:main",
                "div",
                text="Related Accounting / Finance Jobs",
            ),
            _node("n:description", "n:main", "h2", text="Job Description", role="heading"),
            _node(
                "n:summary",
                "n:main",
                "p",
                text=(
                    "Job description: Process vendor invoices, reconcile statements, "
                    "prepare customer billings, research discrepancies, support month-end "
                    "closing, and communicate with staff and vendors while maintaining "
                    "accurate accounting records and confidentiality."
                ),
            ),
        ]
        extracted = RenderedDetailExtractor().extract(
            _report(url, nodes=nodes, title="Accounting / Finance"),
            fallback_url=url,
            title_hint="Accounting / Finance",
        )
        self.assertIsNotNone(extracted.job)
        assert extracted.job is not None
        self.assertEqual(extracted.job.title, "AP/AR Specialist")

    def test_certification_rejects_taxonomy_even_when_other_fields_are_rich(self) -> None:
        valid, reason = assess_certification_job(
            JobPosting(
                title="Machine Learning / Artificial Intelligence",
                job_url="https://careers.example.com/jobs/26317",
                location_text="Remote",
                summary=(
                    "Senior AI Developer / Engineer. Related Machine Learning / Artificial "
                    "Intelligence Jobs. Job description: build cloud AI services and lead "
                    "production engineering delivery."
                ),
            ),
            "https://careers.example.com/jobs/26317",
        )
        self.assertFalse(valid)
        self.assertIn("related_jobs_taxonomy_title", reason)


class Phase7C3CStructuredInteractionTests(unittest.IsolatedAsyncioTestCase):
    def test_url_less_json_is_detail_only_and_requires_strong_identity(self) -> None:
        report = _report(
            "https://careers.example.com/jobs",
            network_json=[_network_job(title="Platform Engineer", job_id="REQ-9")],
        )
        listing_batch = JsonCandidateDiscoverer().discover(report)
        detail_batch = JsonCandidateDiscoverer(
            JsonDiscoveryOptions(allow_url_less_records=True)
        ).discover(report)

        self.assertEqual(listing_batch.candidates, [])
        self.assertEqual(len(detail_batch.candidates), 1)
        candidate = detail_batch.candidates[0]
        self.assertIsNone(candidate.detail_url)
        self.assertEqual(candidate.source_job_id, "REQ-9")
        self.assertIsNotNone(candidate.preextracted_job)
        self.assertTrue(candidate.evidence["url_less_detail_record"])

    async def test_network_capture_window_applies_existing_safety_and_redaction(self) -> None:
        class EventSource:
            def __init__(self) -> None:
                self.handlers: list = []

            def on(self, event, callback) -> None:
                if event == "response":
                    self.handlers.append(callback)

            def off(self, event, callback) -> None:
                if callback in self.handlers:
                    self.handlers.remove(callback)

            def emit(self, response) -> None:
                for handler in list(self.handlers):
                    handler(response)

        class Response:
            url = "https://careers.example.com/api/opening/REQ-9"
            status = 200
            request = SimpleNamespace(resource_type="fetch", method="GET")

            async def all_headers(self):
                return {"content-type": "application/json"}

            async def body(self):
                return json.dumps(
                    {
                        "jobs": [{"jobId": "REQ-9", "jobTitle": "Platform Engineer"}],
                        "sessionToken": "must-not-survive",
                    }
                ).encode("utf-8")

        source = EventSource()

        async def operation():
            source.emit(Response())
            return "clicked"

        collector = BrowserEvidenceCollector(BrowserSettings())
        with patch("src.portals.safety._validate_host_is_public", return_value=None):
            result, evidence, errors = await collector.capture_interaction_network(
                source,
                operation,
                allowed_hosts=("careers.example.com",),
            )
        self.assertEqual(result, "clicked")
        self.assertEqual(errors, [])
        self.assertEqual(len(evidence), 1)
        self.assertNotIn("sessionToken", evidence[0].payload)
        self.assertNotIn("must-not-survive", evidence[0].model_dump_json())

    async def test_click_only_json_resolves_a_same_url_job_without_a_selector(self) -> None:
        listing_url = "https://careers.example.com/jobs"
        nodes = [
            _node("f0:body", None, "body"),
            _node("f0:main", "f0:body", "main", role="main"),
            _node("f0:list", "f0:main", "div"),
        ]
        for index in range(1, 4):
            root = f"f0:row{index}"
            nodes.extend(
                [
                    _node(root, "f0:list", "article", role="article"),
                    _node(
                        f"{root}:title",
                        root,
                        "h2",
                        text=f"Engineer {['I', 'II', 'III'][index - 1]}",
                        role="heading",
                    ),
                    _node(f"{root}:location", root, "div", text=f"City {index}"),
                    _node(
                        f"{root}:button",
                        root,
                        "button",
                        text="View role",
                        role="button",
                        clickable=True,
                    ),
                ]
            )
        baseline = _report(listing_url, nodes=nodes)
        candidate = DomCandidateDiscoverer().discover(
            baseline,
            listing_url=listing_url,
        ).linkless_candidates[0]

        class Frame:
            url = listing_url

            async def evaluate(self, script, token):
                return {"clicked": True, "reason": "element_click"}

        class Page:
            url = listing_url
            frames = [Frame()]

            async def wait_for_load_state(self, *args, **kwargs):
                return None

        page = Page()

        class Context:
            pages = [page]

        class Collector:
            async def capture_interaction_network(self, source, operation, *, allowed_hosts):
                result = await operation()
                return result, [_network_job(title="Engineer I", job_id="REQ-1")], []

            async def snapshot_current_page(self, page, requested_url, *, allowed_hosts):
                return baseline.model_copy(
                    update={"requested_url": requested_url, "final_url": page.url}
                )

        session = BrowserEvidenceSession(
            collector=Collector(),
            context=Context(),
            page=page,
            report=baseline,
            allowed_hosts=("careers.example.com",),
        )
        with patch("src.portals.safety._validate_host_is_public", return_value=None):
            resolved = await LiveLinklessResolver(
                options=LinklessInteractionOptions(max_interactions=1, settle_time_ms=0)
            ).resolve(session, [candidate], listing_url=listing_url)

        self.assertEqual(len(resolved.candidates), 1)
        self.assertEqual(resolved.candidates[0].preextracted_job.title, "Engineer I")
        self.assertEqual(resolved.metrics["structured_network_extractions"], 1)
        self.assertTrue(resolved.candidates[0].evidence["synthetic_detail_identity"])


class Phase7C3CRouteAndIdentityTests(unittest.TestCase):
    def test_strong_ats_search_route_is_not_replaced_by_provider_navigation(self) -> None:
        source = "https://careers-example.icims.com/jobs/search?in_iframe=1"
        html = """
        <a href="https://careers-example.icims.com/">Join our in-house team</a>
        <a href="https://careers-example.icims.com/example-careers/categories">Categories</a>
        <a href="https://careers-example.icims.com/example-careers/locations">Locations</a>
        <a href="https://careers-example.icims.com/example-careers">Revoke consent</a>
        """
        resolution = resolve_listing_route(source_url=source, html=html)
        self.assertIsNone(resolution.selected)
        self.assertFalse(
            any(
                candidate.url.endswith(("/categories", "/locations"))
                for candidate in resolution.candidates
            )
        )

    def test_duplicate_final_job_urls_collapse_and_keep_richer_evidence(self) -> None:
        jobs = [
            JobPosting(
                title="Platform Engineer",
                job_url="https://careers.example.com/jobs/42?utm_source=listing",
            ),
            JobPosting(
                title="Senior Platform Engineer",
                job_url="https://careers.example.com/jobs/42",
                location_text="Remote",
                job_reference="REQ-42",
                summary="Build and operate reliable cloud platform services.",
            ),
            JobPosting(
                title="Different SPA Job",
                job_url="https://careers.example.com/#/jobs/43",
            ),
        ]
        selected, duplicates = deduplicate_extracted_jobs(jobs)
        self.assertEqual(len(selected), 2)
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(selected[0].title, "Senior Platform Engineer")
        self.assertTrue(duplicates[0]["replaced_existing"])
        self.assertEqual(
            duplicates[0]["canonical_job_url"],
            "https://careers.example.com/jobs/42",
        )


if __name__ == "__main__":
    unittest.main()

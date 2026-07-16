from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.portals.surface_diagnostics import (
    build_surface_report,
    inspect_html_surface,
    normalize_link_buckets,
)


class _LinkBuckets:
    def __init__(self) -> None:
        self.internal = [SimpleNamespace(href="/search-results-usa", text="Search Jobs", title="")]
        self.external = []


class SurfaceDiagnosticsTests(unittest.TestCase):
    def test_non_dict_link_container_is_preserved(self) -> None:
        links = normalize_link_buckets(_LinkBuckets())

        self.assertEqual(links["internal"][0]["href"], "/search-results-usa")

    def test_listing_report_identifies_route_transition(self) -> None:
        result = SimpleNamespace(
            success=True,
            status_code=200,
            html='<html><a href="/search-results-usa">Search Jobs</a></html>',
            cleaned_html='<main><a href="/search-results-usa">Search Jobs</a></main>',
            markdown="Consultant careers",
            links={
                "internal": [{"href": "/search-results-usa", "text": "Search Jobs"}],
                "external": [],
            },
        )

        report = build_surface_report(
            result,
            requested_url="https://www.apexsystems.com/consultant-careers",
            mode="listing",
        )

        self.assertEqual(report["failure_stage"], "listing_route_transition_detected")
        self.assertEqual(
            report["inferred_listing_url"],
            "https://www.apexsystems.com/search-results-usa",
        )

    def test_report_exposes_pipeline_link_container_gap(self) -> None:
        result = SimpleNamespace(
            success=True,
            status_code=200,
            html="<html><main>Consultant careers</main></html>",
            cleaned_html="<main>Consultant careers</main>",
            markdown="Consultant careers",
            links=_LinkBuckets(),
        )

        report = build_surface_report(
            result,
            requested_url="https://www.apexsystems.com/consultant-careers",
            mode="listing",
        )

        self.assertEqual(report["failure_stage"], "pipeline_evidence_gap")
        self.assertTrue(report["evidence_gap"]["present"])
        self.assertIsNone(report["pipeline_inferred_listing_url"])

    def test_iframe_only_detail_is_classified_before_llm(self) -> None:
        result = SimpleNamespace(
            success=True,
            status_code=200,
            html='<html><iframe src="https://tenant.i.icims.com/company/jobs/42"></iframe></html>',
            cleaned_html="<main>Career Portal</main>",
            markdown="Career Portal",
            links={"internal": [], "external": []},
        )

        report = build_surface_report(
            result,
            requested_url="https://careers-company.icims.com/company/jobs/42",
            mode="detail",
        )

        self.assertEqual(report["failure_stage"], "embedded_detail_document_suspected")
        self.assertFalse(report["llm_gate"]["invoked"])

    def test_html_inspector_records_non_selector_evidence(self) -> None:
        evidence = inspect_html_surface(
            """
            <html><head><title>Jobs</title><link rel="canonical" href="https://x.test/jobs"></head>
            <body><form action="/search"><iframe src="/embedded"></iframe></form></body></html>
            """
        )

        self.assertEqual(evidence["title"], "Jobs")
        self.assertEqual(evidence["canonical_urls"], ["https://x.test/jobs"])
        self.assertEqual(evidence["forms"][0]["action"], "/search")
        self.assertEqual(evidence["iframes"][0]["src"], "/embedded")


if __name__ == "__main__":
    unittest.main()

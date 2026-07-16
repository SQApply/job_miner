from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.portals.certification import _result_html as certification_result_html
from src.portals.detector import detect_portal
from src.portals.result_evidence import detection_html, structured_link_evidence
from src.portals.runner import _result_html as runner_result_html


class StructuredLinkEvidenceTests(unittest.TestCase):
    def test_apex_search_route_survives_cleaned_html_removal(self) -> None:
        result = SimpleNamespace(
            cleaned_html="<main><h1>Consultant Careers</h1></main>",
            links={
                "internal": [
                    {"href": "/careers/benefits", "text": "Benefits"},
                    {"href": "/search-results-usa", "text": "Search Jobs"},
                ],
                "external": [],
            },
        )

        for combined in (certification_result_html(result), runner_result_html(result)):
            detected = detect_portal(
                listing_url="https://www.apexsystems.com/consultant-careers",
                html=combined,
            )
            self.assertEqual(
                detected.acquisition_hints["listing_url"],
                "https://www.apexsystems.com/search-results-usa",
            )

    def test_icims_canonical_tenant_link_survives_shell_html_removal(self) -> None:
        canonical = (
            "https://c-13769-20240415-careers-insightglobal-com.i.icims.com/"
            "insightglobal-careers/jobs"
        )
        result = SimpleNamespace(
            cleaned_html="<main><h1>Career Portal</h1></main>",
            links={
                "internal": [],
                "external": [{"href": canonical, "text": "Search Jobs"}],
            },
        )
        detected = detect_portal(
            listing_url="https://careers-insightglobal.icims.com/jobs",
            html=detection_html(result, result.cleaned_html),
        )

        self.assertEqual(detected.source_platform, "icims")
        self.assertEqual(detected.acquisition_hints["listing_url"], canonical)

    def test_evidence_is_bounded_deduplicated_and_rejects_active_schemes(self) -> None:
        result = SimpleNamespace(
            links={
                "internal": [
                    {"href": "/jobs", "text": "Search Jobs"},
                    {"href": "/jobs", "text": "Search Jobs"},
                    {"href": "javascript:alert(1)", "text": "Unsafe"},
                    SimpleNamespace(href="/openings", text="View Jobs", title=""),
                ],
                "external": [
                    {"href": "https://user:pass@example.com/jobs", "text": "Credentials"},
                    {"href": "https://boards.greenhouse.io/acme", "title": "Open positions"},
                ],
            }
        )

        evidence = structured_link_evidence(result, max_links=3, max_chars=10_000)

        self.assertEqual(evidence.count('href="/jobs"'), 1)
        self.assertIn('href="/openings"', evidence)
        self.assertIn("boards.greenhouse.io", evidence)
        self.assertNotIn("javascript:", evidence)
        self.assertNotIn("user:pass", evidence)

    def test_missing_or_malformed_link_buckets_are_safe(self) -> None:
        self.assertEqual(structured_link_evidence(SimpleNamespace()), "")
        self.assertEqual(structured_link_evidence(SimpleNamespace(links=[])), "")
        self.assertEqual(
            structured_link_evidence(SimpleNamespace(links={"internal": "not-a-list"})),
            "",
        )


if __name__ == "__main__":
    unittest.main()

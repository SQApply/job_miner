from __future__ import annotations

import unittest

from src.portals.acquisition import AcquisitionContext, AcquisitionRegistry
from src.portals.detector import detect_portal, infer_listing_url
from src.portals.url_intelligence import rank_job_candidate_urls


class FakeHtmlClient:
    def __init__(self, documents: list[str]) -> None:
        self.documents = list(documents)
        self.calls: list[str] = []

    async def request_json(self, url, *, method="GET", payload=None, timeout_seconds=20.0):
        raise AssertionError("The iCIMS HTML provider must not request JSON")

    async def request_text(self, url, *, timeout_seconds=20.0):
        self.calls.append(url)
        if not self.documents:
            raise AssertionError("No fake HTML response remains")
        return self.documents.pop(0)


class ListingRouteDetectionTests(unittest.TestCase):
    def test_branded_shell_selects_rendered_same_site_search_results_route(self) -> None:
        source = "https://www.apexsystems.com/consultant-careers"
        shell = """
        <main><h1>Consultant Careers</h1>
          <a href="/search-results-usa">Search Jobs</a>
          <a href="/careers/benefits">Benefits</a>
        </main>
        """

        self.assertEqual(
            infer_listing_url(source, shell),
            "https://www.apexsystems.com/search-results-usa",
        )
        detected = detect_portal(listing_url=source, html=shell)
        self.assertEqual(
            detected.acquisition_hints["listing_url"],
            "https://www.apexsystems.com/search-results-usa",
        )

        results = detect_portal(
            listing_url=detected.acquisition_hints["listing_url"],
            html='<a href="/job/3042542_usa/risk-manager">Risk Manager</a>'
            '<a aria-label="Next" href="/search-results-usa?page=2">Next</a>',
        )
        self.assertEqual(results.profile_name, "paginated_anchor")

    def test_icims_shell_prefers_canonical_tenant_route_over_outer_shell(self) -> None:
        source = "https://careers-insightglobal.icims.com/jobs"
        canonical = (
            "https://c-13769-20240415-careers-insightglobal-com.i.icims.com/"
            "insightglobal-careers/jobs"
        )
        shell = f"""
        <iframe src="/insightglobal-careers/jobs?in_iframe=1"></iframe>
        <a href="{canonical}">Search Jobs</a>
        """

        detected = detect_portal(listing_url=source, html=shell)
        self.assertEqual(detected.source_platform, "icims")
        self.assertEqual(detected.acquisition_hints["listing_url"], canonical)

    def test_unknown_cross_site_search_link_is_not_promoted(self) -> None:
        source = "https://careers.example.com/careers"
        html = '<a href="https://unrelated.example.net/jobs">Search Jobs</a>'
        self.assertIsNone(infer_listing_url(source, html))


class TenantRoutedICIMSTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefixed_icims_route_discovers_jobs_and_stays_incomplete_when_bounded(self) -> None:
        listing = (
            "https://c-13769-20240415-careers-insightglobal-com.i.icims.com/"
            "insightglobal-careers/jobs"
        )
        document = """
        <h2>88 results</h2>
        <a href="/insightglobal-careers/jobs/7420?lang=en-us">Company Security Officer</a>
        <a href="/insightglobal-careers/jobs/7511?lang=en-us">AR Billing Specialist</a>
        <a href="https://attacker.example/insightglobal-careers/jobs/9999">Unsafe</a>
        """
        client = FakeHtmlClient([document])

        outcome = await AcquisitionRegistry(client=client).acquire(
            AcquisitionContext(listing_url=listing, max_pages=1, require_complete=False)
        )

        self.assertIsNotNone(outcome.selected)
        selected = outcome.selected
        assert selected is not None
        self.assertEqual(
            selected.discovered_urls,
            [
                f"{listing}/7420",
                f"{listing}/7511",
            ],
        )
        self.assertFalse(selected.complete)
        self.assertEqual(selected.metadata["reported_total"], 88)
        self.assertEqual(selected.metadata["listing_path"], "/insightglobal-careers/jobs")
        self.assertIn("/insightglobal-careers/jobs?page=1", client.calls[0])

    async def test_repeated_tenant_page_cannot_claim_full_inventory(self) -> None:
        listing = "https://tenant.i.icims.com/company-careers/jobs"
        document = """
        <div>1 - 1 of 20 Total Jobs</div>
        <a href="/company-careers/jobs/1001?lang=en-us">Data Engineer</a>
        """
        client = FakeHtmlClient([document, document])
        outcome = await AcquisitionRegistry(client=client).acquire(
            AcquisitionContext(listing_url=listing, max_pages=2, require_complete=True)
        )

        self.assertIsNone(outcome.selected)
        self.assertEqual(outcome.attempts[0]["status"], "incomplete")
        self.assertEqual(outcome.attempts[0]["metadata"]["reported_total"], 20)

    async def test_empty_attempt_records_endpoint_evidence(self) -> None:
        listing = "https://tenant.i.icims.com/company-careers/jobs"
        outcome = await AcquisitionRegistry(client=FakeHtmlClient(["<h1>Search Jobs</h1>"])).acquire(
            AcquisitionContext(listing_url=listing, max_pages=1, require_complete=False)
        )

        self.assertIsNone(outcome.selected)
        attempt = outcome.attempts[0]
        self.assertEqual(attempt["status"], "empty")
        self.assertEqual(attempt["endpoint_requests"], 1)
        self.assertEqual(attempt["metadata"]["listing_path"], "/company-careers/jobs")


class KnownAtsFallbackTests(unittest.TestCase):
    def test_icims_listing_roots_do_not_reach_browser_or_gpu_detail_work(self) -> None:
        ranked, metrics = rank_job_candidate_urls(
            [
                "https://jobs.insightglobal.com/",
                "https://jobs.insightglobal.com/?utm_source=tracking",
            ],
            listing_url="https://careers-insightglobal.icims.com/jobs",
            platform_hint="icims",
        )

        self.assertEqual(ranked, [])
        self.assertTrue(metrics["strict_platform_filter"])
        self.assertFalse(metrics["fallback_preserved"])
        self.assertEqual(metrics["rejected_urls"], 1)


if __name__ == "__main__":
    unittest.main()

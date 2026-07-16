from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.portals.acquisition import AcquisitionContext, AcquisitionRegistry, PublicHtmlProvider
from src.portals.route_resolver import (
    hosts_are_related,
    resolve_listing_route,
    route_acquisition_hints,
    route_host_is_trusted,
)
from src.portals.runner import _runtime_portal_allowed_hosts


class ListingRouteResolverTests(unittest.TestCase):
    def test_same_host_search_route_is_selected_without_a_selector(self) -> None:
        source = "https://www.apex.example/consultant-careers"
        resolution = resolve_listing_route(
            source_url=source,
            html=(
                '<a href="/search-results-usa">Search Jobs</a>'
                '<a href="/careers/benefits">Benefits</a>'
            ),
        )

        self.assertIsNotNone(resolution.selected)
        selected = resolution.selected
        assert selected is not None
        self.assertEqual(selected.url, "https://www.apex.example/search-results-usa")
        self.assertEqual(selected.route_kind, "same_host_listing")
        self.assertTrue(selected.trusted)

    def test_jobs_subdomain_is_promoted_with_exact_host_trust(self) -> None:
        resolution = resolve_listing_route(
            source_url="https://www.protingent.example/",
            html='<a href="https://jobs.protingent.example/">View All Jobs</a>',
            structured_links={
                "external": [
                    {
                        "href": "https://jobs.protingent.example/",
                        "text": "View All Jobs",
                    }
                ]
            },
        )

        selected = resolution.selected
        assert selected is not None
        self.assertEqual(selected.hostname, "jobs.protingent.example")
        self.assertEqual(selected.route_kind, "related_host_listing")
        self.assertEqual(selected.trusted_hosts, ("jobs.protingent.example",))
        self.assertTrue(route_host_is_trusted(resolution, "jobs.protingent.example"))
        self.assertFalse(route_host_is_trusted(resolution, "attacker.example"))

    def test_known_workday_configuration_is_evidence_bound(self) -> None:
        workday = "https://acme.wd5.myworkdayjobs.com/en-US/External"
        resolution = resolve_listing_route(
            source_url="https://www.acme.example/careers",
            html=f'<script>window.careersUrl = "{workday}";</script>',
        )

        selected = resolution.selected
        assert selected is not None
        self.assertEqual(selected.url, workday)
        self.assertEqual(selected.platform, "workday")
        self.assertEqual(selected.route_kind, "known_ats")
        self.assertEqual(route_acquisition_hints(resolution)["listing_url"], workday)

    def test_arbitrary_navigation_and_detail_urls_are_not_listing_routes(self) -> None:
        resolution = resolve_listing_route(
            source_url="https://careers.example.com/",
            html=(
                '<a href="https://www.linkedin.com/company/acme/">LinkedIn</a>'
                '<a href="/privacy">Privacy</a>'
                '<a href="/jobs/12345/platform-engineer">Platform Engineer</a>'
            ),
        )

        self.assertIsNone(resolution.selected)
        self.assertEqual(resolution.candidates, ())

    def test_careers_benefits_page_is_not_promoted_as_a_listing(self) -> None:
        resolution = resolve_listing_route(
            source_url="https://company.example/careers",
            html='<a href="/careers/benefits">Benefits</a>',
        )

        self.assertIsNone(resolution.selected)

    def test_known_ats_detail_link_is_not_promoted_as_a_board(self) -> None:
        resolution = resolve_listing_route(
            source_url="https://company.example/careers",
            html=(
                '<a href="https://jobs.lever.co/acme/12345678-abcd-1234-abcd-123456789012">'
                "Senior Engineer</a>"
            ),
        )

        self.assertIsNone(resolution.selected)

    def test_later_job_configuration_attribute_is_not_skipped(self) -> None:
        route = "https://jobs.company.example/openings"
        resolution = resolve_listing_route(
            source_url="https://company.example/careers",
            html=f'<div title="" data-careers-url="{route}"></div>',
        )

        self.assertIsNotNone(resolution.selected)
        assert resolution.selected is not None
        self.assertEqual(resolution.selected.url, route)

    def test_weak_unknown_external_link_does_not_expand_trust(self) -> None:
        resolution = resolve_listing_route(
            source_url="https://careers.example.com/",
            html='<a href="https://unrelated.example.net/opportunities">Partner</a>',
        )

        self.assertIsNone(resolution.selected)
        self.assertTrue(resolution.candidates)
        self.assertFalse(resolution.candidates[0].trusted)

    def test_explicit_external_search_jobs_link_can_be_trusted_exactly(self) -> None:
        route = "https://hiring.vendor.example/openings"
        resolution = resolve_listing_route(
            source_url="https://www.company.example/careers",
            html=f'<a href="{route}">Search Jobs</a>',
        )

        selected = resolution.selected
        assert selected is not None
        self.assertEqual(selected.url, route)
        self.assertEqual(selected.route_kind, "evidence_bound_external_listing")
        self.assertEqual(resolution.trusted_hosts, ("hiring.vendor.example",))

    def test_redirect_requires_listing_or_page_evidence(self) -> None:
        rejected = resolve_listing_route(
            source_url="https://old.example.com/",
            final_url="https://new.example.net/",
            html="<h1>Welcome</h1>",
        )
        accepted = resolve_listing_route(
            source_url="https://old.example.com/",
            final_url="https://new.example.net/careers/jobs",
            html="<h1>Job Openings</h1><p>Search jobs and open positions.</p>",
        )

        self.assertIsNone(rejected.selected)
        self.assertIsNotNone(accepted.selected)
        assert accepted.selected is not None
        self.assertIn("http_redirect", accepted.selected.reasons)

    def test_pagination_link_is_not_mistaken_for_a_new_listing_route(self) -> None:
        source = "https://jobs.example.com/search-results?page=1"
        resolution = resolve_listing_route(
            source_url=source,
            html='<a href="/search-results?page=2">Next</a>',
        )

        self.assertIsNone(resolution.selected)

    def test_equivalent_unrelated_destinations_fail_ambiguous(self) -> None:
        resolution = resolve_listing_route(
            source_url="https://company.example/careers",
            html=(
                '<a href="https://first.vendor.example/jobs">Search Jobs</a>'
                '<a href="https://second.vendor.example/jobs">Search Jobs</a>'
            ),
        )

        self.assertIsNone(resolution.selected)
        self.assertTrue(resolution.ambiguous)

    def test_related_host_check_does_not_collapse_public_suffixes(self) -> None:
        self.assertTrue(hosts_are_related("www.example.com", "jobs.example.com"))
        self.assertFalse(hosts_are_related("company-a.co.uk", "company-b.co.uk"))


class PublicHtmlRouteHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_lane_follows_evidence_bound_jobs_subdomain(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def request_text(self, url, *, timeout_seconds=20.0):
                self.calls.append(url)
                if url == "https://www.company.example/careers":
                    return (
                        '<a href="https://jobs.company.example/search-results">'
                        "Search Jobs</a>"
                    )
                if url == "https://jobs.company.example/search-results":
                    return (
                        '<a href="/job/10001/platform-engineer">Platform Engineer</a>'
                        '<a href="/job/10002/data-engineer">Data Engineer</a>'
                    )
                raise AssertionError(f"Unexpected route {url}")

            async def request_json(self, *args, **kwargs):
                raise AssertionError("JSON acquisition was not expected")

        client = FakeClient()
        with patch(
            "src.portals.acquisition.validate_public_http_url",
            return_value=SimpleNamespace(),
        ):
            outcome = await AcquisitionRegistry(
                client=client,
                providers=(PublicHtmlProvider(),),
            ).acquire(
                AcquisitionContext(
                    listing_url="https://www.company.example/careers",
                    source_platform_hint="custom_listing",
                    max_pages=2,
                    require_complete=False,
                )
            )

        selected = outcome.selected
        assert selected is not None
        self.assertEqual(
            selected.discovered_urls,
            [
                "https://jobs.company.example/job/10001/platform-engineer",
                "https://jobs.company.example/job/10002/data-engineer",
            ],
        )
        self.assertEqual(
            selected.trusted_hosts,
            ("jobs.company.example", "www.company.example"),
        )
        self.assertEqual(
            selected.metadata["listing_url"],
            "https://jobs.company.example/search-results",
        )
        self.assertEqual(len(selected.metadata["route_resolution_chain"]), 1)
        self.assertEqual(client.calls, [
            "https://www.company.example/careers",
            "https://jobs.company.example/search-results",
        ])


class PersistedRouteTrustTests(unittest.TestCase):
    def test_runtime_reuses_only_the_exact_evidence_bound_host(self) -> None:
        portal = {
            "listing_url": "https://company.example/careers",
            "canonical_listing_url": "https://hiring.vendor.example/openings",
            "allowed_hosts": ["company.example"],
            "metadata": {
                "last_probe": {
                    "route_resolution": {
                        "trusted_hosts": ["hiring.vendor.example"],
                    }
                }
            },
        }

        with patch(
            "src.portals.safety._resolve_public_addresses",
            return_value=("93.184.216.34",),
        ):
            allowed = _runtime_portal_allowed_hosts(portal)

        self.assertIn("hiring.vendor.example", allowed)
        self.assertNotIn("www.hiring.vendor.example", allowed)
        self.assertNotIn("vendor.example", allowed)


if __name__ == "__main__":
    unittest.main()

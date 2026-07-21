from __future__ import annotations

import unittest
from pathlib import Path

from src.portals.acquisition import AcquisitionContext, AcquisitionRegistry
from src.portals.job_evidence import (
    infer_job_title_from_url,
    job_title_source_rejection_reason,
)
from src.portals.repair_campaign import audit_repair_records, load_repair_cohort
from src.portals.url_intelligence import assess_certification_job
from src.schemas import JobPosting


ROOT = Path(__file__).resolve().parents[1]


class FakeJsonClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def request_json(self, url, *, method="GET", payload=None, timeout_seconds=20.0):
        self.calls.append({"url": url, "method": method, "payload": payload})
        if not self.responses:
            raise AssertionError("No fake response remains")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class Phase7C3DTitleTruthTests(unittest.TestCase):
    def test_three_observed_website_schema_title_shapes_are_rejected_generically(self) -> None:
        cases = [
            (
                "Creative, Digital, Visual, AI, and Marketing Jobs - Artisan Talent",
                "https://artisantalent.com/jobs/49893",
                "https://artisantalent.com/jobs/",
                "See our open creative and marketing jobs.",
            ),
            (
                "OpTech",
                "https://optechus.com/jobs/application-developer-ii-121240/",
                "https://optechus.com/#website",
                "Recruiting, Staffing and Solutions",
            ),
            (
                "TheBest Claims Solutions",
                "https://www.thebestclaims.com/job-posting/?jobId=006Nw1",
                "https://www.thebestclaims.com/#website",
                "Powered by Partnerships, Sustained by Solutions.",
            ),
        ]
        for title, url, reference, summary in cases:
            with self.subTest(title=title):
                reason = job_title_source_rejection_reason(
                    title,
                    job_url=url,
                    job_reference=reference,
                    summary=summary,
                )
                self.assertIn(reason, {"site_brand_title", "website_schema_title"})
                valid, validation_reason = assess_certification_job(
                    JobPosting(
                        title=title,
                        job_url=url,
                        summary=summary,
                        job_reference=reference,
                    ),
                    url,
                )
                self.assertFalse(valid)
                self.assertIn("site/non-role title rejected", validation_reason)

    def test_real_role_on_branded_host_is_not_rejected(self) -> None:
        self.assertIsNone(
            job_title_source_rejection_reason(
                "Application Developer II",
                job_url="https://optechus.com/jobs/application-developer-ii-121240/",
                job_reference="121240",
                summary="Job description: build and support application services.",
            )
        )

    def test_unfamiliar_requisition_slug_supplies_only_a_title_hint(self) -> None:
        self.assertEqual(
            infer_job_title_from_url(
                "https://careers.example.com/openings/senior-data-platform-engineer-121240/"
            ),
            "senior data platform engineer",
        )
        self.assertIsNone(
            infer_job_title_from_url("https://careers.example.com/job-posting/?jobId=123")
        )


class Phase7C3DWorkdayHydrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_workday_hydrates_only_the_bounded_number_of_detail_records(self) -> None:
        client = FakeJsonClient(
            [
                {
                    "total": 2,
                    "jobPostings": [
                        {
                            "title": "Platform Engineer",
                            "externalPath": "/job/Remote/platform-engineer_JR100",
                            "locationsText": "Remote",
                            "postedOn": "Posted 2 Days Ago",
                        },
                        {
                            "title": "Data Engineer",
                            "externalPath": "/job/Chicago/data-engineer_JR101",
                        },
                    ],
                },
                {
                    "jobPostingInfo": {
                        "title": "Platform Engineer",
                        "jobDescription": (
                            "Job description: build resilient services, automate delivery, "
                            "review designs, respond to incidents, and mentor engineers."
                        ),
                        "location": "Remote",
                        "timeType": "Full time",
                        "jobReqId": "JR100",
                    }
                },
            ]
        )
        outcome = await AcquisitionRegistry(client=client).acquire(
            AcquisitionContext(
                listing_url="https://acme.wd5.myworkdayjobs.com/en-US/External",
                max_pages=1,
                max_records=1,
                require_complete=False,
            )
        )
        selected = outcome.selected
        assert selected is not None
        self.assertEqual(len(selected.discovered_urls), 2)
        self.assertEqual(len(selected.preextracted_jobs), 1)
        self.assertEqual(selected.strategy, "platform_api")
        job = next(iter(selected.preextracted_jobs.values()))
        self.assertEqual(job.title, "Platform Engineer")
        self.assertEqual(job.job_reference, "JR100")
        self.assertIn("/wday/cxs/acme/External/job/Remote/", client.calls[1]["url"])
        self.assertEqual(selected.metadata["detail_records_requested"], 1)
        self.assertEqual(selected.pages_visited, 1)
        self.assertEqual(selected.endpoint_requests, 2)


class Phase7C3DBoundedCampaignTests(unittest.TestCase):
    def test_manifest_freezes_exactly_fifteen_high_yield_sources(self) -> None:
        cohort = load_repair_cohort(
            ROOT / "configs/portal_cohorts/phase7c3d_last_repair.json"
        )
        self.assertEqual(len(cohort.source_ids), 15)
        self.assertEqual(cohort.maximum_attempts, 1)
        self.assertEqual(
            {name: len(values) for name, values in cohort.groups.items()},
            {
                "false_success_recheck": 3,
                "partial_extraction_recheck": 5,
                "url_backed_gpu_rescue": 4,
                "workday_api_hydration": 3,
            },
        )

    def test_audit_never_counts_false_success_as_production_ready(self) -> None:
        cohort = load_repair_cohort(
            ROOT / "configs/portal_cohorts/phase7c3d_last_repair.json"
        )
        source_id = cohort.source_ids[0]
        audit = audit_repair_records(
            [
                {
                    "source_id": source_id,
                    "status": "success",
                    "extracted_jobs": 1,
                    "effective_listing_url": "https://optechus.com/jobs",
                    "sample_jobs": [
                        {
                            "title": "OpTech",
                            "job_url": "https://optechus.com/jobs/developer-121240",
                            "summary": "Recruiting, Staffing and Solutions",
                            "job_reference": "https://optechus.com/#website",
                        }
                    ],
                }
            ],
            cohort,
        )
        self.assertEqual(audit["clean_success_source_ids"], [])
        self.assertEqual(audit["production_ready_after"], 24)
        self.assertEqual(audit["false_successes"][0]["source_id"], source_id)
        self.assertFalse(audit["campaign_complete"])


if __name__ == "__main__":
    unittest.main()

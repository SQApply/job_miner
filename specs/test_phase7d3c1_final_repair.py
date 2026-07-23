from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.blueprint_hub import BlueprintHub
from src.portals.acquisition import (
    AcquisitionOutcome,
    _icims_match,
)
from src.portals.certification import (
    CertificationOptions,
    _acquisition_evidence_hosts,
)
from src.portals.orchestrator import (
    ScrapeExecutionOptions,
    ScrapeOrchestrator,
)
from src.portals.production_target25 import (
    build_target25_plan,
    evaluate_target25_promotion,
)
from src.portals.production_target25_repair import (
    FINAL_REPAIR_DETAIL_BUDGET,
    build_final_repair_report,
    final_repair_source_ids,
)
from src.portals.route_resolver import resolve_listing_route
from src.portals.url_intelligence import assess_job_candidate_url
from src.schemas import JobPosting
from specs.test_phase7d3c_target25 import NOW, _fixtures


ROOT = Path(__file__).resolve().parents[1]


class _Adapter:
    def __init__(self, urls: list[str]) -> None:
        self.urls = urls

    async def discover_job_urls(self, *_args, **_kwargs) -> list[str]:
        return list(self.urls)


class _Crawler:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def arun(self, *, url: str, config):
        self.calls.append(url)
        return SimpleNamespace(
            success=True,
            url=url,
            error_message=None,
            job=JobPosting(title=f"Role {len(self.calls)}", job_url=url),
        )


class Phase7D3C1GenericRepairTests(unittest.IsolatedAsyncioTestCase):
    def test_complete_catalog_detail_budget_is_truthful_and_bounded(self) -> None:
        options = CertificationOptions(
            catalog_mode="complete_catalog",
            max_jobs=None,
            detail_budget=FINAL_REPAIR_DETAIL_BUDGET,
        )
        self.assertEqual(options.detail_budget, 10)
        with self.assertRaisesRegex(ValueError, "cannot also set detail_budget"):
            CertificationOptions(detail_budget=10)

    async def test_detail_budget_preserves_discovery_and_defers_hydration(self) -> None:
        hub = BlueprintHub(ROOT)
        blueprint = hub.get_fleet_targets()[0]
        urls = [
            f"https://example.com/jobs/{index}"
            for index in range(1, 4)
        ]
        crawler = _Crawler()
        orchestrator = ScrapeOrchestrator(
            blueprint=blueprint,
            system_config=hub.system,
            run_session_id="phase7d3c1-budget",
            adapter=_Adapter(urls),
        )

        def detail_config(*_args, **_kwargs):
            return SimpleNamespace(kind="detail")

        with patch(
            "src.portals.orchestrator.detail_run_config",
            side_effect=detail_config,
        ), patch(
            "src.portals.orchestrator.extract_job_from_result",
            side_effect=lambda result, _url: result.job,
        ), patch(
            "src.portals.orchestrator.close_session",
            new_callable=AsyncMock,
        ):
            result = await orchestrator.run_with_crawler(
                crawler,
                options=ScrapeExecutionOptions(
                    prefer_platform_api=False,
                    max_detail_urls=2,
                ),
            )

        self.assertEqual(result.discovered_job_urls, urls)
        self.assertEqual(result.attempted_job_urls, [urls[0], urls[-1]])
        self.assertEqual(len(result.jobs), 2)
        self.assertTrue(result.rescrape_plan["detail_budget_applied"])
        self.assertEqual(result.rescrape_plan["detail_urls_deferred"], 1)
        self.assertEqual(
            result.rescrape_plan["detail_budget_selection"],
            "deterministic_stratified_catalog_sample",
        )

    def test_empty_acquisition_retains_only_explicit_evidence_hosts(self) -> None:
        outcome = AcquisitionOutcome(
            selected=None,
            attempts=[
                {
                    "status": "empty",
                    "trusted_hosts": [
                        "www.computerfutures.com",
                        "www.huxley.com",
                    ],
                    "metadata": {
                        "redirect_evidence": [
                            {"trusted_hosts": ["jobs.huxley.com"]}
                        ]
                    },
                },
                {
                    "status": "failed",
                    "trusted_hosts": ["attacker.example"],
                },
            ],
        )
        self.assertEqual(
            _acquisition_evidence_hosts(outcome),
            (
                "www.computerfutures.com",
                "www.huxley.com",
                "jobs.huxley.com",
            ),
        )

    def test_authentication_routes_do_not_replace_public_job_routes(self) -> None:
        resolution = resolve_listing_route(
            source_url="https://careers.example.com/NA",
            html=(
                '<a href="/na/jobs">Search jobs</a>'
                '<iframe src="https://tenant.icims.com/jobs/login?loginOnly=1&in_iframe=1"></iframe>'
            ),
        )
        self.assertIsNotNone(resolution.selected)
        assert resolution.selected is not None
        self.assertEqual(
            resolution.selected.url,
            "https://careers.example.com/na/jobs",
        )
        self.assertNotIn("login", resolution.selected.url)

    def test_icims_login_hint_is_sanitized_to_public_listing(self) -> None:
        matched = _icims_match(
            "https://tenant.icims.com/jobs/login?loginOnly=1&in_iframe=1"
        )
        self.assertIsNotNone(matched)
        assert matched is not None
        self.assertEqual(matched.listing_url, "https://tenant.icims.com/jobs")

    def test_embedded_board_requisition_slug_is_a_strong_detail_route(self) -> None:
        assessment = assess_job_candidate_url(
            "https://www.abrjobs.com/echojobs/"
            "assembler-stevens-point-wi-93651#!/job-details",
            listing_url="https://www.abrjobs.com/jobs/",
        )
        self.assertFalse(assessment.hard_reject)
        self.assertGreaterEqual(assessment.score, 8)
        self.assertIn(
            "embedded_board_requisition_detail_path",
            assessment.reasons,
        )

    def test_four_repair_results_close_the_verified_target_25_shortfall(self) -> None:
        tiers, policy, inventory, production_plan = _fixtures()
        plan = build_target25_plan(
            inventory=inventory,
            production_plan=production_plan,
            tiers=tiers,
            checkpoint_evidence={"checkpoint_count": 7, "rollout_id": "rollout"},
            policy=policy,
            generated_at=NOW,
        )
        candidate_ids = list(plan["promotion_candidate_source_ids"])
        baseline_records = []
        for index, source_id in enumerate(candidate_ids):
            productive = index < 5
            baseline_records.append(
                {
                    "source_id": source_id,
                    "status": "partial" if productive else "failed",
                    "certification_status": (
                        "catalog_incomplete" if productive else "needs_repair"
                    ),
                    "discovered_urls": 10 if productive else 0,
                    "attempted_urls": 10 if productive else 0,
                    "extracted_jobs": 8 if productive else 0,
                    "catalog_complete": False,
                    "error_type": (
                        "catalog_incomplete" if productive else "zero_discovery"
                    ),
                    "error_message": None,
                }
            )
        prior = evaluate_target25_promotion(
            plan=plan,
            certification_summary={"records": baseline_records},
            generated_at=NOW,
        )
        repair_ids = final_repair_source_ids(
            plan=plan,
            prior_report=prior,
        )
        repair_records = [
            {
                "source_id": source_id,
                "status": "partial",
                "certification_status": "catalog_incomplete",
                "discovered_urls": 100,
                "attempted_urls": 10,
                "extracted_jobs": 10,
                "catalog_complete": False,
                "error_type": "catalog_incomplete",
                "error_message": None,
            }
            for source_id in repair_ids
        ]
        report = build_final_repair_report(
            plan=plan,
            prior_report=prior,
            baseline_summary={"records": baseline_records},
            repair_summary={"records": repair_records},
            generated_at=NOW,
        )

        self.assertTrue(report["target_achieved"])
        self.assertEqual(report["counts"]["final_recurring_usable"], 25)
        self.assertEqual(len(report["repair"]["productive_source_ids"]), 4)
        self.assertFalse(report["controls"]["deactivation_enabled"])
        self.assertFalse(report["controls"]["anti_bot_bypass_enabled"])


if __name__ == "__main__":
    unittest.main()

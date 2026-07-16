from datetime import datetime, timezone
from pathlib import Path
import unittest

from pydantic import ValidationError

from src.blueprint_hub import BlueprintHub
from src.portals.contracts import (
    AcquisitionResult,
    CertificationResult,
    CertificationSideEffects,
    CertificationStatus,
    CompletenessState,
    DiscoveryManifest,
    ExtractionResult,
    GpuExtractionRequest,
    RunMode,
    RunStatus,
    ScrapeStrategy,
    SourceContract,
    SourceRunResult,
    source_contract_from_blueprint,
)
from src.schemas import JobPosting


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)


def sample_job(index: int = 1) -> JobPosting:
    return JobPosting(
        title=f"Data Engineer {index}",
        job_url=f"https://example.com/jobs/{index}",
        company="Example",
        location_text="Remote",
        summary="Build reliable data pipelines.",
        job_reference=f"REQ-{index}",
    )


class ScrapeContractTests(unittest.TestCase):
    def test_all_existing_blueprints_convert_to_versioned_source_contracts(self) -> None:
        hub = BlueprintHub(ROOT)
        contracts = [source_contract_from_blueprint(hub.get_target(target_id)) for target_id in hub.list_target_ids()]

        self.assertEqual(len(contracts), len(hub.list_target_ids()))
        self.assertGreaterEqual(len(contracts), 19)
        self.assertEqual(len({contract.source_id for contract in contracts}), len(contracts))
        self.assertTrue(all(contract.contract_version == 1 for contract in contracts))
        self.assertTrue(all(contract.fingerprint() for contract in contracts))
        self.assertTrue(all(contract.strategy_order[-1] == ScrapeStrategy.LLM_FALLBACK for contract in contracts))

    def test_source_contract_preserves_hash_route_identity(self) -> None:
        hub = BlueprintHub(ROOT)
        contract = source_contract_from_blueprint(hub.get_target("jobdiva_jobs"))

        self.assertTrue(contract.identity.preserve_url_fragment)
        self.assertTrue(contract.listing_url.endswith("#/"))

    def test_source_contract_rejects_duplicate_strategies(self) -> None:
        with self.assertRaises(ValidationError):
            SourceContract(
                source_id="example_jobs",
                provided_url="https://example.com/jobs",
                listing_url="https://example.com/jobs",
                allowed_hosts=["example.com"],
                strategy_order=[ScrapeStrategy.JSON_LD, ScrapeStrategy.JSON_LD],
            )

    def test_complete_manifest_is_reconciliation_safe(self) -> None:
        manifest = DiscoveryManifest(
            run_id="run-1",
            source_id="example_jobs",
            contract_version=1,
            strategy=ScrapeStrategy.PLATFORM_API,
            completeness=CompletenessState.COMPLETE,
            discovered_count=2,
            discovered_source_job_ids=["1", "2"],
            discovered_urls=["https://example.com/jobs/1", "https://example.com/jobs/2"],
            pages_visited=1,
            pagination_complete=True,
            started_at=NOW,
            completed_at=NOW,
        )

        self.assertTrue(manifest.reconciliation_allowed)

    def test_partial_and_incomplete_manifests_cannot_reconcile(self) -> None:
        partial = DiscoveryManifest(
            run_id="run-1",
            source_id="example_jobs",
            contract_version=1,
            strategy=ScrapeStrategy.CRAWL4AI,
            completeness=CompletenessState.PARTIAL,
            discovered_count=1,
            discovered_urls=["https://example.com/jobs/1"],
            pages_visited=1,
            pagination_complete=False,
            reasons=["Load-more stopped before a stable terminal state."],
        )
        self.assertFalse(partial.reconciliation_allowed)

        with self.assertRaises(ValidationError):
            DiscoveryManifest(
                run_id="run-2",
                source_id="example_jobs",
                contract_version=1,
                strategy=ScrapeStrategy.CRAWL4AI,
                completeness=CompletenessState.COMPLETE,
                discovered_count=1,
                discovered_urls=["https://example.com/jobs/1"],
                pagination_complete=False,
                completed_at=NOW,
            )

    def test_zero_discovery_requires_explicit_empty_confirmation(self) -> None:
        with self.assertRaises(ValidationError):
            DiscoveryManifest(
                run_id="run-1",
                source_id="example_jobs",
                contract_version=1,
                strategy=ScrapeStrategy.PLATFORM_API,
                completeness=CompletenessState.COMPLETE,
                discovered_count=0,
                pagination_complete=True,
                completed_at=NOW,
            )

        with self.assertRaises(ValidationError):
            DiscoveryManifest(
                run_id="run-2",
                source_id="example_jobs",
                contract_version=1,
                strategy=ScrapeStrategy.PLATFORM_API,
                completeness=CompletenessState.EMPTY_CONFIRMED,
                discovered_count=0,
                pagination_complete=True,
                started_at=NOW,
                completed_at=NOW,
            )

        empty = DiscoveryManifest(
            run_id="run-3",
            source_id="example_jobs",
            contract_version=1,
            strategy=ScrapeStrategy.PLATFORM_API,
            completeness=CompletenessState.EMPTY_CONFIRMED,
            discovered_count=0,
            pagination_complete=True,
            started_at=NOW,
            completed_at=NOW,
            reasons=["The source API returned an authenticated, complete empty result."],
        )
        self.assertTrue(empty.reconciliation_allowed)

    def test_crawl4ai_acquisition_records_browser_usage(self) -> None:
        with self.assertRaises(ValidationError):
            AcquisitionResult(
                strategy=ScrapeStrategy.CRAWL4AI,
                success=True,
                final_url="https://example.com/jobs",
            )

        result = AcquisitionResult(
            strategy=ScrapeStrategy.CRAWL4AI,
            success=True,
            final_url="https://example.com/jobs",
            browser_used=True,
        )
        self.assertTrue(result.browser_used)

    def test_gpu_cache_key_is_stable_and_versioned(self) -> None:
        request = GpuExtractionRequest(
            request_id="gpu-1",
            run_id="run-1",
            source_id="example_jobs",
            source_job_id="REQ-1",
            content_ref="artifacts/run-1/example_jobs/REQ-1.md",
            content_hash="a" * 64,
            known_fields={"title": "Data Engineer"},
        )
        duplicate = request.model_copy(update={"request_id": "gpu-2"})
        changed_prompt = request.model_copy(update={"prompt_version": "2"})
        changed_known_fields = request.model_copy(update={"known_fields": {"title": "Senior Data Engineer"}})

        self.assertEqual(request.cache_key(), duplicate.cache_key())
        self.assertNotEqual(request.cache_key(), changed_prompt.cache_key())
        self.assertNotEqual(request.cache_key(), changed_known_fields.cache_key())

    def test_llm_extraction_must_record_gpu_or_cache_usage(self) -> None:
        with self.assertRaises(ValidationError):
            ExtractionResult(
                strategy=ScrapeStrategy.LLM_FALLBACK,
                valid=True,
                job=sample_job(),
                model_name="qwen2.5:3b",
            )

        cached = ExtractionResult(
            strategy=ScrapeStrategy.LLM_FALLBACK,
            valid=True,
            job=sample_job(),
            cache_hit=True,
            model_name="qwen2.5:3b",
        )
        self.assertTrue(cached.cache_hit)
        self.assertFalse(cached.gpu_used)

    def test_certification_is_limited_to_ten_and_has_no_side_effects(self) -> None:
        jobs = [sample_job(index) for index in range(1, 11)]
        result = CertificationResult(
            source_id="example_jobs",
            status=CertificationStatus.PASSED,
            discovered_count=25,
            attempted_count=10,
            jobs=jobs,
        )

        self.assertEqual(result.requested_limit, 10)
        self.assertEqual(result.valid_job_count, 10)
        self.assertEqual(result.side_effects, CertificationSideEffects())

        with self.assertRaises(ValidationError):
            CertificationSideEffects(write_catalog=True)

    def test_source_run_enforces_nested_identity_and_certification_mode(self) -> None:
        jobs = [sample_job(index) for index in range(1, 11)]
        manifest = DiscoveryManifest(
            run_id="run-1",
            source_id="example_jobs",
            contract_version=1,
            strategy=ScrapeStrategy.JSON_LD,
            completeness=CompletenessState.COMPLETE,
            discovered_count=10,
            discovered_urls=[job.job_url for job in jobs if job.job_url],
            pagination_complete=True,
            started_at=NOW,
            completed_at=NOW,
        )
        certification = CertificationResult(
            source_id="example_jobs",
            status=CertificationStatus.PASSED,
            discovered_count=10,
            attempted_count=10,
            jobs=jobs,
        )
        result = SourceRunResult(
            run_id="run-1",
            source_id="example_jobs",
            contract_version=1,
            mode=RunMode.CERTIFICATION,
            status=RunStatus.SUCCEEDED,
            started_at=NOW,
            completed_at=NOW,
            manifest=manifest,
            certification=certification,
        )

        self.assertIsNotNone(result.manifest)
        self.assertTrue(result.manifest and result.manifest.reconciliation_allowed)
        self.assertIsNotNone(result.certification)
        self.assertEqual(result.certification.valid_job_count if result.certification else 0, 10)


if __name__ == "__main__":
    unittest.main()

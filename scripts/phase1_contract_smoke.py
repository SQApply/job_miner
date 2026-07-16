from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.blueprint_hub import BlueprintHub
from src.portals.contracts import (
    CertificationResult,
    CertificationStatus,
    CompletenessState,
    DiscoveryManifest,
    GpuExtractionRequest,
    ScrapeStrategy,
    source_contract_from_blueprint,
)
from src.schemas import JobPosting


def main() -> int:
    hub = BlueprintHub(ROOT)
    target_ids = hub.list_target_ids()
    contracts = [source_contract_from_blueprint(hub.get_target(target_id)) for target_id in target_ids]

    if len(contracts) != len(target_ids):
        raise RuntimeError("Not every existing blueprint produced a source contract")
    if len({contract.source_id for contract in contracts}) != len(contracts):
        raise RuntimeError("Duplicate source IDs were found")

    now = datetime.now(timezone.utc)
    job = JobPosting(
        title="Phase 1 Contract Test",
        job_url="https://example.com/jobs/phase-1",
        company="Example",
        location_text="Remote",
        summary="Validate the shared scraper contracts.",
        job_reference="PHASE-1",
    )
    manifest = DiscoveryManifest(
        run_id="phase-1-smoke",
        source_id="phase_1_smoke",
        contract_version=1,
        strategy=ScrapeStrategy.JSON_LD,
        completeness=CompletenessState.COMPLETE,
        discovered_count=1,
        discovered_source_job_ids=["PHASE-1"],
        discovered_urls=[job.job_url],
        pages_visited=1,
        pagination_complete=True,
        started_at=now,
        completed_at=now,
    )
    if not manifest.reconciliation_allowed:
        raise RuntimeError("A complete manifest was not recognized as reconciliation-safe")

    certification = CertificationResult(
        source_id="phase_1_smoke",
        status=CertificationStatus.SOURCE_EXHAUSTED,
        discovered_count=1,
        attempted_count=1,
        jobs=[job],
    )
    if certification.valid_job_count != 1:
        raise RuntimeError("Certification contract did not preserve the validated job")

    gpu_request = GpuExtractionRequest(
        request_id="phase-1-gpu",
        run_id="phase-1-smoke",
        source_id="phase_1_smoke",
        source_job_id="PHASE-1",
        content_ref="artifacts/phase-1-smoke/PHASE-1.md",
        content_hash="a" * 64,
    )
    if len(gpu_request.cache_key()) != 64:
        raise RuntimeError("GPU extraction cache key is not a SHA-256 digest")

    print(
        "PHASE_1_CONTRACT_SMOKE_OK",
        f"blueprints={len(contracts)}",
        f"unique_sources={len({contract.source_id for contract in contracts})}",
        f"certified_jobs={certification.valid_job_count}",
        f"gpu_cache_key={gpu_request.cache_key()[:12]}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

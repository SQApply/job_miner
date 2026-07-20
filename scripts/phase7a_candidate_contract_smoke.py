from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.blueprint_hub import BlueprintHub
from src.portals.contracts import (
    DiscoveryBatch,
    DiscoveryCandidate,
    DiscoveryCandidateKind,
    ScrapeStrategy,
)
from src.portals.orchestrator import ScrapeExecutionOptions, ScrapeOrchestrator
from src.schemas import JobPosting


class CandidateAdapter:
    async def discover_candidates(
        self,
        crawler,
        blueprint,
        system_config,
        session_logger=None,
    ) -> DiscoveryBatch:
        detail_url = "https://example.com/jobs/phase7a-1"
        return DiscoveryBatch(
            strategy=ScrapeStrategy.BLUEPRINT_DOM,
            candidates=[
                DiscoveryCandidate.from_url(
                    detail_url,
                    preextracted_job=JobPosting(
                        title="Phase 7A Contract Engineer",
                        job_url=detail_url,
                    ),
                ),
                DiscoveryCandidate(
                    candidate_id="phase7a_linkless_card_1",
                    kind=DiscoveryCandidateKind.DOM_CLICK,
                    node_token="session-node-1",
                    title_hint="Linkless Data Engineer",
                    confidence=0.9,
                    evidence={"structural_signature": "article>h2+button"},
                ),
            ],
        )


class NoBrowserCrawler:
    async def arun(self, **kwargs):
        raise AssertionError("preextracted URL candidate must not launch the detail browser")


async def smoke() -> None:
    hub = BlueprintHub(ROOT)
    orchestrator = ScrapeOrchestrator(
        blueprint=hub.get_fleet_targets()[0],
        system_config=hub.system,
        run_session_id="phase7a-smoke",
        adapter=CandidateAdapter(),
    )
    result = await orchestrator.run_with_crawler(
        NoBrowserCrawler(),
        options=ScrapeExecutionOptions(
            prefer_platform_api=False,
            max_jobs=1,
        ),
    )

    assert len(result.discovered_candidates) == 2
    assert len(result.discovered_job_urls) == 1
    assert len(result.jobs) == 1
    assert result.linkless_candidate_count == 1
    assert len(result.attempted_candidate_ids) == 1
    print(
        "PHASE_7A_CANDIDATE_CONTRACT_SMOKE_OK",
        json.dumps(
            {
                "candidates": len(result.discovered_candidates),
                "url_candidates": len(result.discovered_job_urls),
                "linkless_candidates": result.linkless_candidate_count,
                "jobs": len(result.jobs),
                "browser_calls": 0,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    asyncio.run(smoke())

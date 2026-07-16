from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.acquisition import AcquisitionContext, AcquisitionRegistry
from src.portals.detector import detect_portal


class SmokeJsonClient:
    def __init__(self) -> None:
        self.calls = 0

    async def request_json(self, url, *, method="GET", payload=None, timeout_seconds=20.0):
        self.calls += 1
        return {
            "jobs": [
                {
                    "id": 1,
                    "title": "Phase 3 Test Engineer",
                    "absolute_url": "https://job-boards.greenhouse.io/example/jobs/1",
                    "location": {"name": "Remote"},
                    "content": "Validates API-first acquisition.",
                }
            ]
        }


async def main() -> None:
    detection = detect_portal(
        listing_url="https://example.com/careers",
        html='<iframe src="https://boards.greenhouse.io/embed/job_board?for=example"></iframe>',
    )
    client = SmokeJsonClient()
    outcome = await AcquisitionRegistry(client=client).acquire(
        AcquisitionContext(
            listing_url="https://example.com/careers",
            source_platform_hint=detection.source_platform,
            acquisition_hints=detection.acquisition_hints,
        )
    )
    assert outcome.selected is not None
    assert outcome.selected.complete
    assert len(outcome.selected.preextracted_jobs) == 1
    print(
        "PHASE_3_ACQUISITION_SMOKE_OK",
        f"platform={outcome.selected.platform}",
        f"jobs={len(outcome.selected.discovered_urls)}",
        f"api_calls={client.calls}",
        "browser_calls=0",
        "llm_calls=0",
    )


if __name__ == "__main__":
    asyncio.run(main())

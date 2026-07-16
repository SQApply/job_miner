from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.blueprint_hub import BlueprintHub
from src.portals.orchestrator import ScrapeExecutionOptions, ScrapeOrchestrator


def main() -> int:
    hub = BlueprintHub(ROOT)
    blueprints = hub.get_fleet_targets()
    if not blueprints:
        raise RuntimeError("No fleet blueprints were loaded")
    mission_control = (ROOT / "src" / "mission_control.py").read_text(encoding="utf-8")
    portal_runner = (ROOT / "src" / "portals" / "runner.py").read_text(encoding="utf-8")
    if "orchestrator = ScrapeOrchestrator(" not in mission_control:
        raise RuntimeError("Static fleet is not wired to ScrapeOrchestrator")
    if "orchestrator = ScrapeOrchestrator(" not in portal_runner:
        raise RuntimeError("Portal runner is not wired to ScrapeOrchestrator")

    for blueprint in blueprints:
        ScrapeOrchestrator(
            blueprint=blueprint,
            system_config=hub.system,
            run_session_id=f"phase2-smoke-{blueprint.id}",
        )

    options = ScrapeExecutionOptions(
        detail_concurrency=hub.system.browser.detail_extraction_concurrency,
        detail_retry_attempts=2,
        requests_per_minute=30,
        max_jobs=10,
    )
    print(
        "PHASE_2_ORCHESTRATOR_SMOKE_OK "
        f"blueprints={len(blueprints)} "
        f"entry_points=2 max_jobs={options.max_jobs} "
        f"concurrency={options.detail_concurrency} retries={options.detail_retry_attempts}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

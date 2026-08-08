from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from specs.test_phase8_2_queue_isolation import Phase82QueueIsolationTests
from src.infrastructure.scrape_queue_topology import ScrapeQueueTopology


def main() -> None:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        Phase82QueueIsolationTests
    )
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)

    topology = ScrapeQueueTopology.from_environment({})
    payload = {
        "queues": len(topology.lanes),
        "worker_groups": len(topology.worker_groups()),
        "browser_queues": [
            lane.logical_name for lane in topology.lanes if lane.browser_bound
        ],
        "protected_queues": [
            "fleet_control",
            "job_persistence",
            "reconciliation",
        ],
        "scheduler": False,
        "network": False,
        "mongodb_reads": False,
        "mongodb_writes": False,
    }
    print(
        "PHASE_8_2_QUEUE_ISOLATION_SMOKE_OK "
        + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )


if __name__ == "__main__":
    main()

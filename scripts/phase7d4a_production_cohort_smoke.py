from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from specs.test_phase7d4a_production_cohort import (
    Phase7D4AProductionCohortTests,
)


def main() -> None:
    suite = __import__("unittest").TestLoader().loadTestsFromTestCase(
        Phase7D4AProductionCohortTests
    )
    result = __import__("unittest").TextTestRunner(verbosity=0).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(2)
    print(
        "PHASE_7D4A_23_SOURCE_COHORT_SMOKE_OK",
        json.dumps(
            {
                "recurring_sources": 23,
                "complete_catalog_sources": 7,
                "partial_safe_sources": 16,
                "promoted_backfill_sources": 7,
                "initial_backfill_batch_sizes": [4, 3],
                "steady_state_batch_sizes": [4, 4, 4, 4, 4, 3],
                "cadence_hours": 72,
                "network": False,
                "mongodb_reads": False,
                "mongodb_writes": False,
                "reconciliation": False,
                "deactivation": False,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from specs.test_phase7d3c1_final_repair import (
    Phase7D3C1GenericRepairTests,
)


def main() -> None:
    suite = unittest.TestLoader().loadTestsFromTestCase(
        Phase7D3C1GenericRepairTests
    )
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(2)
    print(
        "PHASE_7D3C1_FINAL_REPAIR_SMOKE_OK",
        json.dumps(
            {
                "repair_sources": 4,
                "detail_budget_per_source": 10,
                "target_recurring_sources": 25,
                "mongodb_writes": False,
                "reconciliation": False,
                "deactivation": False,
                "anti_bot_bypass": False,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

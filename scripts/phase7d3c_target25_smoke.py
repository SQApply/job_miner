from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from specs.test_phase7d3c_target25 import Phase7D3CTarget25Tests


def main() -> None:
    suite = __import__("unittest").TestLoader().loadTestsFromTestCase(
        Phase7D3CTarget25Tests
    )
    result = __import__("unittest").TextTestRunner(verbosity=0).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(2)
    print(
        "PHASE_7D3C_TARGET25_SMOKE_OK",
        json.dumps(
            {
                "current_complete": 5,
                "current_partial_safe": 11,
                "current_recurring_usable": 16,
                "promotion_candidates": 9,
                "target": 25,
                "reconciliation": False,
                "deactivation": False,
                "gpu_llm_concurrency": 1,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

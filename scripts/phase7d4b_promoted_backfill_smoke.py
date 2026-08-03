from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from specs.test_phase7d4b_promoted_backfill import (
    test_authorization_is_exactly_the_signed_seven_promoted_sources,
    test_complete_batch_passes_and_round_trips,
    test_nonproductive_source_is_deferred_without_blocking_other_sources,
    test_runner_config_is_unbounded_single_gpu_safe_write_mode,
    test_write_confirmation_is_exact,
)


class Phase7D4BPromotedBackfillSmoke(unittest.TestCase):
    def test_authorized_scope(self) -> None:
        test_authorization_is_exactly_the_signed_seven_promoted_sources()

    def test_runner_controls(self) -> None:
        test_runner_config_is_unbounded_single_gpu_safe_write_mode()

    def test_confirmation_guard(self) -> None:
        test_write_confirmation_is_exact()

    def test_complete_report(self) -> None:
        test_complete_batch_passes_and_round_trips()

    def test_safe_deferral(self) -> None:
        test_nonproductive_source_is_deferred_without_blocking_other_sources()


def main() -> None:
    result = unittest.TextTestRunner(verbosity=0).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(
            Phase7D4BPromotedBackfillSmoke
        )
    )
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(
        "PHASE_7D4B_PROMOTED_BACKFILL_SMOKE_OK",
        json.dumps(
            {
                "promoted_sources": 7,
                "batch_sizes": [4, 3],
                "catalog_mode": "complete_catalog",
                "max_jobs_per_source": None,
                "source_concurrency": 1,
                "gpu_llm_concurrency": 1,
                "confirmation_required": True,
                "mongodb_writes_in_smoke": False,
                "reconciliation": False,
                "deactivation": False,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

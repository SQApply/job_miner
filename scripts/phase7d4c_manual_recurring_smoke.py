from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from src.portals.production_manual_recurring import (
        PHASE_7D4C_MANUAL_WRITE_CONFIRMATION,
        build_phase7d4c_runner_config,
        load_phase7d4c_manual_policy,
        require_phase7d4c_manual_confirmation,
    )

    policy = load_phase7d4c_manual_policy(
        root=root,
        policy_path=(
            root
            / "configs"
            / "portal_cohorts"
            / "phase7d4c_manual_recurring_policy.json"
        ),
    )
    require_phase7d4c_manual_confirmation(
        PHASE_7D4C_MANUAL_WRITE_CONFIRMATION
    )
    runner = build_phase7d4c_runner_config(policy)
    assert len(policy["source_ids"]) == 22
    assert runner.catalog_mode == "complete_catalog"
    assert runner.max_jobs is None
    assert runner.max_source_concurrency == 1
    assert runner.detail_concurrency == 1
    assert runner.incremental_rescrape is True
    assert policy["execution"]["automatic_scheduler_enabled"] is False
    assert policy["lifecycle"]["deactivate_after_complete_misses"] == 2
    assert policy["lifecycle"]["failed_or_partial_runs_increment_missing_count"] is False
    assert policy["downstream"]["changed_only"] is True
    print(
        "PHASE_7D4C_MANUAL_RECURRING_SMOKE_OK",
        json.dumps(
            {
                "sources": 22,
                "cadence_hours": 72,
                "scheduler": False,
                "source_concurrency": 1,
                "gpu_llm_concurrency": 1,
                "incremental_detail_rescrape": True,
                "deep_refresh_days": 14,
                "complete_snapshot_gate": True,
                "deactivate_after_complete_misses": 2,
                "changed_only_downstream": True,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()

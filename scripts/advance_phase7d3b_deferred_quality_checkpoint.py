from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_guarded_execution import (
    PHASE_7D3B_QUALITY_DEFERRAL_CONFIRMATION,
    ProductionGuardedExecutionError,
    read_phase7d3b_report,
    reclassify_phase7d3b_deferred_quality_checkpoint,
    write_phase7d3b_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reclassify an existing Phase 7D3B checkpoint when isolated quality "
            "quarantine is its only blocker. No scraping, network, MongoDB read, "
            "MongoDB write, reconciliation, or deactivation is performed."
        )
    )
    parser.add_argument(
        "--checkpoint",
        default="data/phase7d3b/phase7d3b_batch_03_checkpoint.json",
    )
    parser.add_argument("--confirm-quality-deferral", default="")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    checkpoint = Path(args.checkpoint).resolve()
    try:
        if args.confirm_quality_deferral != PHASE_7D3B_QUALITY_DEFERRAL_CONFIRMATION:
            raise ProductionGuardedExecutionError(
                "Checkpoint reclassification requires --confirm-quality-deferral "
                + PHASE_7D3B_QUALITY_DEFERRAL_CONFIRMATION
            )
        original = read_phase7d3b_report(checkpoint)
        revised = reclassify_phase7d3b_deferred_quality_checkpoint(original)

        backup = checkpoint.with_name(
            checkpoint.stem + ".pre_quality_deferral" + checkpoint.suffix
        )
        if backup.exists():
            existing_backup = read_phase7d3b_report(backup)
            if existing_backup.get("report_sha256") != original.get("report_sha256"):
                raise ProductionGuardedExecutionError(
                    f"Existing checkpoint backup differs from the current input: {backup}"
                )
        else:
            write_phase7d3b_report(backup, original)
        write_phase7d3b_report(checkpoint, revised)

        reclassification = revised["checkpoint_reclassification"]
        print(
            "PHASE_7D3B2_CHECKPOINT_RECLASSIFIED",
            f"batch={revised['batch_ordinal']}/{revised['batch_count']}",
            f"status={revised['status']}",
            f"complete={revised['complete_catalog_source_count']}",
            f"deferred={revised['deferred_source_count']}",
            f"quarantined={revised['counters']['quarantined']}",
            "removed_blockers="
            + json.dumps(reclassification["removed_blockers"], separators=(",", ":")),
            "next_batch_allowed=true",
            "network=false",
            "mongodb_reads=false",
            "mongodb_writes=false",
            "reconciliation=false",
            "deactivation=false",
            f"backup={backup}",
            f"checkpoint={checkpoint}",
            flush=True,
        )
    except Exception as exc:
        print(
            "PHASE_7D3B2_CHECKPOINT_RECLASSIFICATION_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={str(exc)}",
            "network=false",
            "mongodb_reads=false",
            "mongodb_writes=false",
            "reconciliation=false",
            "deactivation=false",
            f"checkpoint={checkpoint}",
            flush=True,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

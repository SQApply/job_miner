from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.mongo import get_mongo_database
from src.portals.production_closeout import (
    load_and_build_phase6f_report,
    write_phase6f_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate two Phase 6E write manifests and MongoDB invariants, then "
            "generate the formal Phase 6 closeout report."
        )
    )
    parser.add_argument("--plan", default="data/phase6a/phase6a_ingestion_plan.json")
    parser.add_argument("--first-write-manifest", required=True)
    parser.add_argument("--rerun-manifest", required=True)
    parser.add_argument("--expected-source-count", type=int, default=19)
    parser.add_argument("--output", default="data/phase6f/phase6_closeout_report.json")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        report = load_and_build_phase6f_report(
            plan_path=Path(args.plan),
            first_write_manifest_path=Path(args.first_write_manifest),
            rerun_manifest_path=Path(args.rerun_manifest),
            db=get_mongo_database(),
            expected_source_count=args.expected_source_count,
        )
        output = write_phase6f_report(Path(args.output), report)
        checks = report.database_checks
        if report.status != "passed":
            print(
                "PHASE_6F_CLOSEOUT_FAILED",
                f"sources={len(report.selected_source_ids)}",
                f"issues={len(report.issues)}",
                f"rerun_inserted={report.rerun_summary['inserted_jobs']}",
                f"duplicates={checks.duplicate_identity_hashes + checks.duplicate_external_job_keys}",
                f"history_mismatches={checks.history_mismatches}",
                "reconciliation=false",
                "deactivation=false",
                f"output={output}",
            )
            for issue in report.issues:
                print(" -", issue)
            raise SystemExit(2)
        print(
            "PHASE_6F_CLOSEOUT_OK",
            f"sources={len(report.selected_source_ids)}",
            f"first_inserted={report.first_write_summary['inserted_jobs']}",
            f"rerun_inserted={report.rerun_summary['inserted_jobs']}",
            f"current_jobs={checks.current_jobs_observed_on_rerun}",
            "duplicates=0",
            "history_mismatches=0",
            "reconciliation=false",
            "deactivation=false",
            "ready_for_phase7=true",
            f"output={output}",
        )
    except SystemExit:
        raise
    except Exception as exc:
        print(
            "PHASE_6F_CLOSEOUT_ERROR",
            f"error_type={type(exc).__name__}",
            f"error={str(exc)}",
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

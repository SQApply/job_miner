from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_ingestion import (
    Phase6AIngestionConfig,
    ProductionIngestionError,
    build_phase6a_ingestion_plan,
    requested_source_ids_from_inputs,
    write_phase6a_ingestion_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the frozen Phase 5.5C cohort and prepare a safe, plan-only "
            "Phase 6A ingestion execution manifest. No scraping or database writes occur."
        )
    )
    parser.add_argument(
        "--cohort-file",
        default="configs/portal_cohorts/phase5_5c_certified_cohort.json",
        help="Frozen Phase 5.5C cohort manifest",
    )
    parser.add_argument(
        "--output",
        default="data/phase6a/phase6a_ingestion_plan.json",
        help="Output JSON plan",
    )
    parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="Optional cohort source to select; repeat for multiple sources",
    )
    parser.add_argument(
        "--source-id-file",
        default=None,
        help="Optional newline-delimited source-id file",
    )
    parser.add_argument("--expected-cohort-size", type=int, default=19)
    parser.add_argument("--max-source-concurrency", type=int, default=2)
    parser.add_argument("--source-timeout-seconds", type=int, default=600)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        requested = requested_source_ids_from_inputs(
            source_ids=args.source_id,
            source_id_file=Path(args.source_id_file) if args.source_id_file else None,
        )
        config = Phase6AIngestionConfig(
            expected_cohort_size=args.expected_cohort_size,
            max_source_concurrency=args.max_source_concurrency,
            source_timeout_seconds=args.source_timeout_seconds,
            requested_source_ids=requested,
        )
        plan = build_phase6a_ingestion_plan(
            cohort_path=Path(args.cohort_file),
            config=config,
        )
        output = write_phase6a_ingestion_plan(Path(args.output), plan)
    except (FileNotFoundError, ProductionIngestionError, ValueError) as exc:
        print(f"PHASE_6A_PLAN_FAILED error={exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from exc

    print(
        "PHASE_6A_PLAN_READY",
        f"selected={plan.selected_source_count}",
        f"cohort={plan.cohort_source_count}",
        f"deferred={plan.deferred_source_count}",
        "writes=false",
        "reconciliation=false",
        "deactivation=false",
        f"output={output}",
        flush=True,
    )


if __name__ == "__main__":
    main()

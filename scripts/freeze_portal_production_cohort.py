from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_cohort import (
    build_frozen_production_cohort,
    validate_frozen_production_cohort,
    write_frozen_production_cohort,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze the Phase 5.5C certified portal cohort from complete, "
            "cross-checked fleet evidence."
        )
    )
    parser.add_argument("--input", required=True, help="Portal XLSX/CSV/TXT inventory")
    parser.add_argument("--fleet-report", required=True, help="fleet_campaign_report.json")
    parser.add_argument(
        "--certification-summary",
        required=True,
        help="portal_certification_summary.json",
    )
    parser.add_argument(
        "--output-dir",
        default="configs/portal_cohorts",
        help="Directory for the frozen cohort and source-id files",
    )
    parser.add_argument("--expected-inventory", type=int, default=102)
    parser.add_argument("--expected-certified", type=int, default=19)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate an existing frozen cohort without rewriting it",
    )
    parser.add_argument(
        "--cohort-file",
        default=None,
        help="Existing cohort JSON; required with --validate-only",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    input_path = Path(args.input).resolve()
    fleet_report_path = Path(args.fleet_report).resolve()
    summary_path = Path(args.certification_summary).resolve()

    if args.expected_inventory < 1:
        parser.error("--expected-inventory must be at least 1")
    if args.expected_certified < 1:
        parser.error("--expected-certified must be at least 1")

    if args.validate_only:
        if not args.cohort_file:
            parser.error("--validate-only requires --cohort-file")
        cohort = validate_frozen_production_cohort(
            cohort_path=Path(args.cohort_file),
            input_path=input_path,
            fleet_report_path=fleet_report_path,
            certification_summary_path=summary_path,
            expected_inventory=args.expected_inventory,
            expected_certified=args.expected_certified,
        )
        print(
            "PHASE_5_5C_COHORT_VALID",
            f"sources={cohort['cohort_source_count']}",
            f"deferred={cohort['deferred_source_count']}",
            f"sha256={cohort['cohort_sha256']}",
            flush=True,
        )
        return

    cohort = build_frozen_production_cohort(
        input_path=input_path,
        fleet_report_path=fleet_report_path,
        certification_summary_path=summary_path,
        expected_inventory=args.expected_inventory,
        expected_certified=args.expected_certified,
    )
    artifacts = write_frozen_production_cohort(
        output_dir=Path(args.output_dir),
        cohort=cohort,
    )
    validate_frozen_production_cohort(
        cohort_path=Path(artifacts["cohort"]),
        input_path=input_path,
        fleet_report_path=fleet_report_path,
        certification_summary_path=summary_path,
        expected_inventory=args.expected_inventory,
        expected_certified=args.expected_certified,
    )
    print(
        "PHASE_5_5C_COHORT_FROZEN",
        f"sources={cohort['cohort_source_count']}",
        f"deferred={cohort['deferred_source_count']}",
        f"cohort={artifacts['cohort']}",
        f"source_ids={artifacts['cohort_source_ids']}",
        f"sha256={cohort['cohort_sha256']}",
        flush=True,
    )


if __name__ == "__main__":
    main()

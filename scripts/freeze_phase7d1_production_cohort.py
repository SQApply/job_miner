from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_rollout import (
    ProductionRolloutError,
    build_phase7d1_production_rollout,
    validate_phase7d1_production_rollout,
    write_phase7d1_production_rollout,
)


DEFAULT_REPAIR_MANIFEST = Path("configs/portal_cohorts/phase7c3d_last_repair.json")
DEFAULT_OUTPUT_DIR = Path("configs/portal_cohorts")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline Phase 7D1 closeout: revalidate saved bounded job samples, "
            "overlay the final 15-source repair evidence, and freeze only truthful "
            "successes. This command performs no scraping, GPU work, or database writes."
        )
    )
    parser.add_argument("--input", required=True, help="The original 102-source inventory")
    parser.add_argument(
        "--baseline-summary",
        required=True,
        help="Completed 102-source Phase 7C3C portal_certification_summary.json",
    )
    parser.add_argument(
        "--repair-summary",
        required=True,
        help="Completed 15-source Phase 7C3D portal_certification_summary.json",
    )
    parser.add_argument("--repair-manifest", default=str(DEFAULT_REPAIR_MANIFEST))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--expected-inventory", type=int, default=102)
    parser.add_argument("--expected-baseline-ready", type=int, default=24)
    parser.add_argument("--expected-repair-records", type=int, default=15)
    parser.add_argument("--expected-final-ready", type=int, default=26)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate existing Phase 7D1 artifacts without rewriting them",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    root = Path.cwd().resolve()
    input_path = Path(args.input).resolve()
    baseline_path = Path(args.baseline_summary).resolve()
    repair_path = Path(args.repair_summary).resolve()
    manifest_path = Path(args.repair_manifest)
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    cohort_path = output_dir / "phase7d_truthful_production_cohort.json"
    report_path = output_dir / "phase7d_truthful_quality_report.json"

    common = {
        "input_path": input_path,
        "baseline_summary_path": baseline_path,
        "repair_manifest_path": manifest_path,
        "repair_summary_path": repair_path,
        "expected_inventory": args.expected_inventory,
        "expected_baseline_ready": args.expected_baseline_ready,
        "expected_repair_records": args.expected_repair_records,
        "expected_final_ready": args.expected_final_ready,
    }
    try:
        if args.validate_only:
            cohort, report = validate_phase7d1_production_rollout(
                cohort_path=cohort_path,
                quality_report_path=report_path,
                **common,
            )
            action = "VALID"
        else:
            cohort, report = build_phase7d1_production_rollout(**common)
            artifacts = write_phase7d1_production_rollout(
                output_dir=output_dir,
                cohort=cohort,
                quality_report=report,
            )
            validate_phase7d1_production_rollout(
                cohort_path=Path(artifacts["cohort"]),
                quality_report_path=Path(artifacts["quality_report"]),
                **common,
            )
            action = "FROZEN"
    except (FileNotFoundError, ProductionRolloutError, ValueError) as exc:
        print(
            "PHASE_7D1_COHORT_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={exc}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2) from exc

    counts = report["counts"]
    print(
        f"PHASE_7D1_COHORT_{action}",
        f"baseline={counts['baseline_production_ready']}",
        f"additions={counts['newly_production_ready']}",
        f"sources={cohort['cohort_source_count']}",
        f"deferred={cohort['deferred_source_count']}",
        "network=false",
        "writes=false",
        "reconciliation=false",
        "deactivation=false",
        f"cohort={cohort_path}",
        f"quality_report={report_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()

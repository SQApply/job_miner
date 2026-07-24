from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_cohort23 import (
    ProductionCohort23Error,
    build_phase7d4a_from_paths,
    validate_phase7d4a_from_paths,
    write_phase7d4a_artifacts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze the truthful 23-source production cohort and prepare its "
            "tier-aware 72-hour rollout plan. This command performs no network "
            "requests, GPU work, MongoDB access, reconciliation, or deactivation."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        help="The original 102-source XLSX/CSV/TXT portal inventory",
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--target25-plan",
        default="data/phase7d3c/phase7d3c_target25_plan.json",
    )
    parser.add_argument(
        "--final-report",
        default=(
            "data/phase7d3c/final_repair/"
            "phase7d3c1_target25_final_report.json"
        ),
    )
    parser.add_argument(
        "--policy",
        default="configs/portal_cohorts/phase7d4_23_source_policy.json",
    )
    parser.add_argument(
        "--cohort-output",
        default=(
            "configs/portal_cohorts/"
            "phase7d4_truthful_23_source_cohort.json"
        ),
    )
    parser.add_argument(
        "--rollout-output",
        default="data/phase7d4/phase7d4a_production_rollout_plan.json",
    )
    parser.add_argument("--validate-only", action="store_true")
    return parser


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    input_path = Path(args.input).resolve()
    plan_path = _resolve(root, args.target25_plan)
    report_path = _resolve(root, args.final_report)
    policy_path = _resolve(root, args.policy)
    cohort_path = _resolve(root, args.cohort_output)
    rollout_path = _resolve(root, args.rollout_output)
    try:
        if args.validate_only:
            cohort, rollout = validate_phase7d4a_from_paths(
                input_path=input_path,
                target25_plan_path=plan_path,
                final_report_path=report_path,
                policy_path=policy_path,
                cohort_path=cohort_path,
                rollout_path=rollout_path,
            )
            created = False
            action = "VALID"
        else:
            cohort, rollout = build_phase7d4a_from_paths(
                input_path=input_path,
                target25_plan_path=plan_path,
                final_report_path=report_path,
                policy_path=policy_path,
            )
            _, created = write_phase7d4a_artifacts(
                cohort_path=cohort_path,
                rollout_path=rollout_path,
                cohort=cohort,
                rollout=rollout,
            )
            validate_phase7d4a_from_paths(
                input_path=input_path,
                target25_plan_path=plan_path,
                final_report_path=report_path,
                policy_path=policy_path,
                cohort_path=cohort_path,
                rollout_path=rollout_path,
            )
            action = "FROZEN"
    except (
        FileNotFoundError,
        ProductionCohort23Error,
        ValueError,
    ) as exc:
        print(
            "PHASE_7D4A_23_SOURCE_COHORT_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={exc}",
            "network=false",
            "mongodb_reads=false",
            "mongodb_writes=false",
            "reconciliation=false",
            "deactivation=false",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2) from exc
    counts = cohort["counts"]
    print(
        f"PHASE_7D4A_23_SOURCE_COHORT_{action}",
        f"sources={counts['recurring_usable']}",
        f"complete={counts['complete_catalog']}",
        f"partial_safe={counts['partial_safe']}",
        f"promoted_backfill={counts['promoted_backfill']}",
        f"failed_candidates_excluded={counts['failed_candidates_excluded']}",
        f"deferred={counts['deferred']}",
        f"initial_backfill_batches={rollout['initial_backfill']['batch_count']}",
        f"initial_backfill_batch_sizes={json.dumps([batch['source_count'] for batch in rollout['initial_backfill']['batches']], separators=(',', ':'))}",
        f"recurring_batches={rollout['steady_state_rescrape']['batch_count']}",
        f"recurring_batch_sizes={json.dumps([batch['source_count'] for batch in rollout['steady_state_rescrape']['batches']], separators=(',', ':'))}",
        f"cadence_hours={rollout['steady_state_rescrape']['cadence_hours']}",
        f"created={str(created).lower()}",
        "catalog_mode=complete_catalog",
        "max_jobs_per_source=unlimited",
        "source_concurrency=1",
        "gpu_llm_concurrency=1",
        "network=false",
        "mongodb_reads=false",
        "mongodb_writes=false",
        "reconciliation=false",
        "deactivation=false",
        f"cohort={cohort_path}",
        f"rollout={rollout_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()

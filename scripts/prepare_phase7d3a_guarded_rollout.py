from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_guarded_rollout import (
    build_phase7d3a_rollout_plan,
    load_phase7d3a_evidence,
    read_phase7d3a_rollout_plan,
    write_phase7d3a_rollout_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare the offline Phase 7D3A guarded rollout plan. All 26 production "
            "sources, including the two pilot-seeded sources, are included in both "
            "complete-catalog backfill and the 72-hour recurring rescrape schedule."
        )
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--cohort",
        default="configs/portal_cohorts/phase7d_truthful_production_cohort.json",
    )
    parser.add_argument(
        "--quality-report",
        default="configs/portal_cohorts/phase7d_truthful_quality_report.json",
    )
    parser.add_argument(
        "--production-plan",
        default="data/phase7d1/phase7d1_production_plan.json",
    )
    parser.add_argument(
        "--semantic-closeout",
        default="data/phase7d2c/phase7d2c_semantic_closeout.json",
    )
    parser.add_argument(
        "--output",
        default="data/phase7d3/phase7d3a_guarded_rollout_plan.json",
    )
    parser.add_argument("--validate-only", action="store_true")
    return parser


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    output_path = _resolve(root, args.output)
    try:
        if args.validate_only:
            stored = read_phase7d3a_rollout_plan(output_path)
            print(
                "PHASE_7D3A_ROLLOUT_PLAN_VALID",
                f"rollout_id={stored['rollout_id']}",
                f"full_sources={stored['full_source_count']}",
                f"already_seeded={stored['seeded_source_count']}",
                f"initial_backfill_sources={stored['initial_backfill_source_count']}",
                f"initial_backfill_batches={stored['initial_backfill_batch_count']}",
                f"steady_state_rescrape_sources={stored['steady_state_rescrape_source_count']}",
                f"steady_state_rescrape_batches={stored['steady_state_rescrape_batch_count']}",
                f"cadence_hours={stored['rescrape_policy']['cadence_hours']}",
                "catalog_mode=complete",
                "max_jobs_per_source=unlimited",
                "all_sources_rescraped=true",
                f"plan={output_path}",
                flush=True,
            )
            return

        production_plan, selection, semantic, semantic_sha = load_phase7d3a_evidence(
            cohort_path=_resolve(root, args.cohort),
            quality_report_path=_resolve(root, args.quality_report),
            production_plan_path=_resolve(root, args.production_plan),
            semantic_closeout_path=_resolve(root, args.semantic_closeout),
        )
        candidate = build_phase7d3a_rollout_plan(
            production_plan=production_plan,
            selection=selection,
            semantic_closeout=semantic,
            semantic_closeout_sha256=semantic_sha,
        )
        plan_path, stored, created = write_phase7d3a_rollout_plan(
            output_path,
            candidate,
        )
        print(
            "PHASE_7D3A_ROLLOUT_PLAN_READY",
            f"rollout_id={stored['rollout_id']}",
            f"full_sources={stored['full_source_count']}",
            f"already_seeded={stored['seeded_source_count']}",
            f"initial_backfill_sources={stored['initial_backfill_source_count']}",
            f"initial_backfill_batch_size={stored['initial_backfill_batch_size']}",
            f"initial_backfill_batches={stored['initial_backfill_batch_count']}",
            f"steady_state_rescrape_sources={stored['steady_state_rescrape_source_count']}",
            f"steady_state_rescrape_batch_size={stored['steady_state_rescrape_batch_size']}",
            f"steady_state_rescrape_batches={stored['steady_state_rescrape_batch_count']}",
            f"cadence_hours={stored['rescrape_policy']['cadence_hours']}",
            "all_sources_rescraped=true",
            f"created={str(created).lower()}",
            "source_concurrency=1",
            "gpu_llm_concurrency=1",
            "catalog_mode=complete",
            "max_jobs_per_source=unlimited",
            f"page_safety_cap={stored['catalog_completion_policy']['page_safety_cap']}",
            "network=false",
            "mongodb_reads=false",
            "mongodb_writes=false",
            "reconciliation=false",
            "deactivation=false",
            f"initial_backfill_batch_source_counts={json.dumps([batch['source_count'] for batch in stored['initial_backfill_batches']], separators=(',', ':'))}",
            f"steady_state_rescrape_batch_source_counts={json.dumps([batch['source_count'] for batch in stored['steady_state_rescrape_batches']], separators=(',', ':'))}",
            f"plan={plan_path}",
            flush=True,
        )
    except Exception as exc:
        print(
            "PHASE_7D3A_ROLLOUT_PLAN_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={str(exc)}",
            "network=false",
            "mongodb_writes=false",
            f"plan={output_path}",
            flush=True,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

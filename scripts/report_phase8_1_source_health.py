from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.mongo import get_mongo_database
from src.portals.production_health import (
    collect_phase8_1_source_health,
    write_phase8_1_source_health_reports,
)
from src.portals.production_manual_recurring import (
    load_phase7d4c_manual_policy,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate the read-only Phase 8.1 health report for the exact "
            "Phase 7D4C recurring cohort."
        )
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path(
            "configs/portal_cohorts/phase7d4c_manual_recurring_policy.json"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/phase8_1/source_health"),
    )
    parser.add_argument(
        "--fail-on-attention",
        action="store_true",
        help="Return exit code 2 when any source needs operator attention.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    root = args.root.resolve()
    policy_path = (
        args.policy.resolve()
        if args.policy.is_absolute()
        else (root / args.policy).resolve()
    )
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir.is_absolute()
        else (root / args.output_dir).resolve()
    )
    policy = load_phase7d4c_manual_policy(
        root=root,
        policy_path=policy_path,
    )
    database = get_mongo_database()
    report = collect_phase8_1_source_health(
        database,
        source_ids=policy["source_ids"],
        cadence_hours=int(policy["cadence_hours"]),
        failure_retry_hours=int(policy["failure_retry_hours"]),
    )
    json_path, csv_path = write_phase8_1_source_health_reports(
        report,
        output_dir=output_dir,
    )
    total = len(report["sources"])
    for position, source in enumerate(report["sources"], start=1):
        counts = source["latest_run_counts"]
        current = source["current_jobs"]
        print(
            f"[{position}/{total}]",
            str(source["health_status"]).upper(),
            source["source_id"],
            f"checkpoint={source['latest_checkpoint_status']}",
            f"discovered={counts['discovered']}",
            f"accepted={counts['accepted']}",
            f"active={current['active']}",
            f"missing={counts['missing']}",
            f"deactivated={counts['deactivated']}",
            f"failures={source['consecutive_failures']}",
            f"due={str(source['is_due']).lower()}",
            f"error={source['latest_error_type'] or '-'}",
            flush=True,
        )
    print(
        "PHASE_8_1_SOURCE_HEALTH_COMPLETE",
        f"sources={report['cohort_source_count']}",
        f"accounted={report['sources_accounted']}",
        "status_counts=" + json.dumps(report["status_counts"], separators=(",", ":")),
        f"healthy={report['healthy_source_count']}",
        f"attention={report['attention_source_count']}",
        f"complete_snapshots={report['complete_snapshot_source_count']}",
        f"due={report['due_source_count']}",
        "scheduler=false",
        "mongodb_reads=true",
        "mongodb_writes=false",
        "reconciliation=false",
        "deactivation=false",
        f"json={json_path}",
        f"csv={csv_path}",
        flush=True,
    )
    if args.fail_on_attention and report["attention_source_count"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

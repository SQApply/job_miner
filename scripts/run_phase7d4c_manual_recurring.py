from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Phase 7D4C 22-source recurring scrape manually. "
            "No Celery Beat task or automatic scheduler is created."
        )
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--input", required=True, help="Portal .xlsx, .csv, or .txt inventory")
    parser.add_argument(
        "--policy",
        default=(
            "configs/portal_cohorts/"
            "phase7d4c_manual_recurring_policy.json"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="data/phase7d4c/manual_recurring",
    )
    parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="Run an exact cohort source only; repeat for multiple sources.",
    )
    parser.add_argument("--resume-cycle-id")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow an intentional run before the stored 72-hour due time.",
    )
    parser.add_argument("--confirm-production-writes", default="")
    return parser


def _progress(checkpoint: dict[str, Any], position: int, total: int) -> None:
    result = checkpoint.get("result") or {}
    lifecycle = checkpoint.get("lifecycle") or {}
    downstream = checkpoint.get("downstream") or {}
    print(
        f"[{position}/{total}]",
        str(checkpoint.get("status") or "unknown").upper(),
        str(checkpoint.get("source_id") or "unknown"),
        f"discovered={int(result.get('discovered_count') or 0)}",
        f"accepted={int(result.get('accepted_count') or 0)}",
        f"inserted={int(result.get('inserted_count') or 0)}",
        f"updated={int(result.get('updated_count') or 0)}",
        f"unchanged={int(result.get('unchanged_count') or 0)}",
        f"missing={int(lifecycle.get('missing_marked') or 0)}",
        f"deactivated={int(lifecycle.get('deactivated') or 0)}",
        f"indexed={int(downstream.get('qdrant_indexed') or 0)}",
        f"error={checkpoint.get('error_type') or result.get('error_type') or '-'}",
        flush=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from src.portals.production_manual_recurring import (
        PHASE_7D4C_MANUAL_WRITE_CONFIRMATION,
        Phase7D4CManualRecurringRunner,
        ProductionManualRecurringError,
        build_phase7d4c_manual_plan,
        ensure_manual_cycle_is_due,
        load_phase7d4c_manual_policy,
        require_phase7d4c_manual_confirmation,
        write_phase7d4c_report,
    )

    policy_path = _resolve(root, args.policy)
    inventory_path = _resolve(root, args.input)
    output_dir = _resolve(root, args.output_dir)

    try:
        policy = load_phase7d4c_manual_policy(
            root=root,
            policy_path=policy_path,
        )
        requested_source_ids = list(args.source_id)
        database = None
        if args.resume_cycle_id:
            if args.validate_only:
                raise ProductionManualRecurringError(
                    "--resume-cycle-id cannot be combined with --validate-only"
                )
            from src.infrastructure.mongo import get_mongo_database

            database = get_mongo_database()
            database.client.admin.command("ping")
            stored = database["production_recurring_cycles"].find_one(
                {"cycle_id": args.resume_cycle_id}
            )
            if not stored:
                raise ProductionManualRecurringError(
                    f"Recurring cycle does not exist: {args.resume_cycle_id}"
                )
            stored_ids = [
                str(value) for value in stored.get("selected_source_ids") or []
            ]
            if requested_source_ids and requested_source_ids != stored_ids:
                raise ProductionManualRecurringError(
                    "--source-id values differ from the stored resume scope"
                )
            requested_source_ids = stored_ids

        plan = build_phase7d4c_manual_plan(
            root=root,
            policy=policy,
            inventory_path=inventory_path,
            requested_source_ids=requested_source_ids,
        )
        if args.validate_only:
            print(
                "PHASE_7D4C_MANUAL_RECURRING_VALID",
                f"inventory_sources={plan.cohort_source_count + plan.deferred_source_count}",
                f"cohort_sources={plan.cohort_source_count}",
                f"selected_sources={plan.selected_source_count}",
                "cadence_hours=72",
                "scheduler=false",
                "network=false",
                "mongodb_reads=false",
                "mongodb_writes=false",
                "reconciliation=false",
                "deactivation=false",
                flush=True,
            )
            return

        require_phase7d4c_manual_confirmation(
            args.confirm_production_writes
        )
        if database is None:
            from src.infrastructure.mongo import get_mongo_database

            database = get_mongo_database()
            database.client.admin.command("ping")
        if not args.resume_cycle_id:
            ensure_manual_cycle_is_due(
                database,
                expected_source_count=plan.selected_source_count,
                force=bool(args.force),
            )

        runner = Phase7D4CManualRecurringRunner(
            root=root,
            output_dir=output_dir,
            database=database,
            plan=plan,
            policy=policy,
        )
        report = asyncio.run(
            runner.run(
                resume_cycle_id=args.resume_cycle_id,
                on_progress=_progress,
            )
        )
        report_path = (
            output_dir
            / str(report["cycle_id"])
            / "phase7d4c_manual_recurring_report.json"
        )
        write_phase7d4c_report(report_path, report)
        counters = report["counters"]
        print(
            "PHASE_7D4C_MANUAL_RECURRING_"
            + (
                "PASSED"
                if report["status"] == "completed"
                else "COMPLETED_WITH_DEFERRED"
            ),
            f"cycle_id={report['cycle_id']}",
            f"status={report['status']}",
            f"sources={report['selected_source_count']}",
            f"status_counts={json.dumps(report['status_counts'], separators=(',', ':'))}",
            f"accepted={counters['accepted_count']}",
            f"inserted={counters['inserted_count']}",
            f"updated={counters['updated_count']}",
            f"unchanged={counters['unchanged_count']}",
            f"reactivated={counters['reactivated_count']}",
            f"missing={counters['missing_marked']}",
            f"deactivated={counters['deactivated']}",
            "scheduler=false",
            f"next_due_at={report['next_due_at']}",
            f"report={report_path}",
            flush=True,
        )
        if report["status"] == "completed_with_failures":
            print(
                "PHASE_7D4C_MANUAL_RECURRING_RESUME_REQUIRED",
                f"command=python scripts\\run_phase7d4c_manual_recurring.py --input {inventory_path} --resume-cycle-id {report['cycle_id']} --confirm-production-writes {PHASE_7D4C_MANUAL_WRITE_CONFIRMATION}",
                flush=True,
            )
            raise SystemExit(2)
    except (ProductionManualRecurringError, FileNotFoundError, ValueError) as exc:
        print(
            "PHASE_7D4C_MANUAL_RECURRING_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={exc}",
            "scheduler=false",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

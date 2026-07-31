from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan, apply, or verify the reversible Phase 7D4C soft cutover "
            "to the exact 22-source production cohort."
        )
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--mode",
        choices=("plan", "apply", "verify"),
        default="plan",
    )
    parser.add_argument(
        "--policy",
        default=(
            "configs/portal_cohorts/"
            "phase7d4c_active22_cutover_policy.json"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="data/phase7d4c/cohort_cutover",
    )
    parser.add_argument(
        "--plan",
        default=(
            "data/phase7d4c/cohort_cutover/"
            "phase7d4c_cohort_cutover_plan.json"
        ),
    )
    parser.add_argument("--backup-archive")
    parser.add_argument("--confirm", default="")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from src.infrastructure.mongo import get_mongo_database
    from src.portals.production_cohort_cutover import (
        CohortCutoverError,
        execute_cutover,
        load_cutover_policy,
        read_plan,
        verify_cutover,
        write_json,
    )

    policy_path = _resolve(root, args.policy)
    output_dir = _resolve(root, args.output_dir)
    plan_path = _resolve(root, args.plan)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        database = get_mongo_database()
        database.client.admin.command("ping")
        policy = load_cutover_policy(policy_path)

        if args.mode == "plan":
            from src.portals.production_cohort_cutover import (
                build_cutover_plan,
            )

            report = build_cutover_plan(database, policy=policy)
            write_json(plan_path, report)
            print(
                "PHASE_7D4C_COHORT_CUTOVER_PLAN_READY",
                f"database={database.name}",
                f"cohort_sources={len(report['cohort_source_ids'])}",
                f"active_cohort_jobs={report['counts']['cohort_active']}",
                f"active_non_cohort_jobs={report['counts']['outside_active']}",
                "mongo_writes=false",
                "deactivation=false",
                f"ready_to_apply={str(report['ready_to_apply']).lower()}",
                f"plan={plan_path}",
                flush=True,
            )
            if not report["ready_to_apply"]:
                print(
                    "PHASE_7D4C_COHORT_CUTOVER_BLOCKERS",
                    json.dumps(report["blockers"], separators=(",", ":")),
                    flush=True,
                )
                raise SystemExit(2)
            return

        if args.mode == "apply":
            if not args.backup_archive:
                raise CohortCutoverError(
                    "--backup-archive is required in apply mode"
                )
            plan = read_plan(plan_path)
            backup_path = _resolve(root, args.backup_archive)
            snapshot_path = (
                output_dir
                / "phase7d4c_pre_cutover_lifecycle_snapshot.json.gz"
            )
            report = execute_cutover(
                database,
                policy=policy,
                plan=plan,
                backup_archive=backup_path,
                snapshot_path=snapshot_path,
                confirmation=args.confirm,
            )
            report_path = (
                output_dir / "phase7d4c_cohort_cutover_apply_report.json"
            )
            write_json(report_path, report)
            print(
                "PHASE_7D4C_COHORT_CUTOVER_APPLIED",
                f"cutover_id={report['cutover_id']}",
                f"deactivated={report['deactivated_jobs']}",
                f"active_after={report['after']['jobs_current_active']}",
                f"inactive_after={report['after']['jobs_current_inactive']}",
                "normalized_jobs_deleted=false",
                "history_deleted=false",
                "raw_evidence_deleted=false",
                "scheduler=false",
                "qdrant_rebuild_required=true",
                f"report={report_path}",
                flush=True,
            )
            return

        report = verify_cutover(database, policy=policy)
        report_path = (
            output_dir / "phase7d4c_cohort_cutover_verify_report.json"
        )
        write_json(report_path, report)
        print(
            "PHASE_7D4C_COHORT_CUTOVER_"
            + ("VERIFIED" if report["status"] == "passed" else "FAILED"),
            f"active_jobs={report['active_jobs']}",
            f"inactive_jobs={report['inactive_jobs']}",
            f"active_sources={report['active_cohort_source_count']}",
            f"active_non_cohort={report['active_non_cohort_jobs']}",
            f"non_cohort_towers={report['non_cohort_job_towers']}",
            f"ready_for_candidate_pipeline={str(report['ready_for_candidate_pipeline']).lower()}",
            "mongo_writes=false",
            f"report={report_path}",
            flush=True,
        )
        if report["status"] != "passed":
            print(
                "PHASE_7D4C_COHORT_CUTOVER_BLOCKERS",
                json.dumps(report["blockers"], separators=(",", ":")),
                flush=True,
            )
            raise SystemExit(2)
    except CohortCutoverError as exc:
        print(
            "PHASE_7D4C_COHORT_CUTOVER_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={exc}",
            "physical_job_deletion=false",
            "scheduler=false",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

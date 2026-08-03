from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.mongo import get_mongo_database
from src.portals.production_cohort23_backfill import (
    PHASE_7D4B_WRITE_CONFIRMATION,
    batch_checkpoint_path,
    build_phase7d4b_batch_report,
    build_phase7d4b_runner_config,
    capture_phase7d4b_source_snapshot,
    load_phase7d4b_authorization,
    require_phase7d4b_checkpoint_state,
    require_phase7d4b_write_confirmation,
    write_phase7d4b_report,
)
from src.portals.production_jobs import init_phase6c_indexes
from src.portals.production_persistence import init_phase6b_indexes
from src.portals.production_quality import init_phase6d_indexes
from src.portals.production_runner import (
    CertificationPhase6ESourceExecutor,
    Phase6EProductionRunner,
    Phase6ESourceResult,
    write_phase6e_manifest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill one signed Phase 7D4B promoted-source batch. Only new and "
            "changed jobs are upserted; reconciliation and deactivation stay off."
        )
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--cohort",
        default=(
            "configs/portal_cohorts/"
            "phase7d4_truthful_23_source_cohort.json"
        ),
    )
    parser.add_argument(
        "--rollout-plan",
        default="data/phase7d4/phase7d4a_production_rollout_plan.json",
    )
    parser.add_argument("--output-dir", default="data/phase7d4b")
    parser.add_argument("--batch-ordinal", type=int, default=1)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--resume-incomplete", action="store_true")
    parser.add_argument("--confirm-production-writes", default="")
    return parser


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _progress(result: Phase6ESourceResult, completed: int, total: int) -> None:
    print(
        f"[{completed}/{total}]",
        result.status.upper(),
        result.source_id,
        f"attempts={len(result.attempts)}",
        f"discovered={result.discovered_count}",
        f"attempted={result.attempted_count}",
        f"extracted={result.extracted_count}",
        f"accepted={result.accepted_count}",
        f"inserted={result.inserted_count}",
        f"updated={result.updated_count}",
        f"unchanged={result.unchanged_count}",
        f"reactivated={result.reactivated_count}",
        f"quarantined={result.quarantined_count}",
        f"rejected={result.rejected_count}",
        f"catalog_complete={str(result.catalog_complete).lower()}",
        f"error={result.error_type or '-'}",
        flush=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    output_dir = _resolve(root, args.output_dir)
    writes_started = False
    run_dir: Path | None = None
    try:
        plan, authorization = load_phase7d4b_authorization(
            cohort_path=_resolve(root, args.cohort),
            rollout_plan_path=_resolve(root, args.rollout_plan),
            batch_ordinal=args.batch_ordinal,
        )
        print(
            "PHASE_7D4B_AUTHORIZATION_OK",
            f"rollout_id={authorization.rollout_id}",
            f"batch={authorization.batch_ordinal}/{authorization.batch_count}",
            f"batch_id={authorization.batch_id}",
            f"sources={authorization.source_count}",
            f"source_ids={json.dumps(authorization.source_ids, separators=(',', ':'))}",
            f"source_tiers={json.dumps(authorization.source_tiers, separators=(',', ':'))}",
            "scope=promoted_sources_only",
            "catalog_mode=complete_catalog",
            "max_jobs_per_source=unlimited",
            "source_concurrency=1",
            "detail_concurrency=1",
            "gpu_llm_concurrency=1",
            "reconciliation=false",
            "deactivation=false",
            flush=True,
        )
        if args.validate_only:
            print(
                "PHASE_7D4B_VALIDATE_OK",
                f"batch={authorization.batch_ordinal}/{authorization.batch_count}",
                f"sources={authorization.source_count}",
                "network=false",
                "mongodb_reads=false",
                "mongodb_writes=false",
                "reconciliation=false",
                "deactivation=false",
                flush=True,
            )
            return

        require_phase7d4b_write_confirmation(args.confirm_production_writes)
        require_phase7d4b_checkpoint_state(
            output_dir=output_dir,
            authorization=authorization,
            resume_incomplete=args.resume_incomplete,
        )
        database = get_mongo_database()
        before = capture_phase7d4b_source_snapshot(
            database,
            source_ids=authorization.source_ids,
        )
        init_phase6b_indexes(database)
        init_phase6c_indexes(database)
        init_phase6d_indexes(database)

        config = build_phase7d4b_runner_config(authorization)
        run_id = (
            f"phase7d4b_b{authorization.batch_ordinal:02d}_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "_"
            + uuid.uuid4().hex[:10]
        )
        run_dir = (
            output_dir
            / f"batch_{authorization.batch_ordinal:02d}"
            / run_id
        )
        runner = Phase6EProductionRunner(
            plan=plan,
            db=database,
            executor=CertificationPhase6ESourceExecutor(
                root=root,
                output_dir=run_dir / "scrape_evidence",
                options=config.certification_options(),
            ),
            config=config,
        )
        writes_started = True
        manifest = asyncio.run(
            runner.run(
                requested_source_ids=authorization.source_ids,
                run_id=run_id,
                on_progress=_progress,
            )
        )
        manifest_path = write_phase6e_manifest(run_dir / "manifest.json", manifest)
        after = capture_phase7d4b_source_snapshot(
            database,
            source_ids=authorization.source_ids,
        )
        report = build_phase7d4b_batch_report(
            authorization=authorization,
            manifest=manifest,
            before=before,
            after=after,
        )
        report_path = write_phase7d4b_report(run_dir / "report.json", report)
        checkpoint_path = write_phase7d4b_report(
            batch_checkpoint_path(output_dir, authorization.batch_ordinal),
            report,
        )
        print(
            "PHASE_7D4B_PROMOTED_BACKFILL_" + str(report["status"]).upper(),
            f"batch={authorization.batch_ordinal}/{authorization.batch_count}",
            f"complete={report['complete_catalog_source_count']}/{authorization.source_count}",
            f"productive_partial={report['productive_partial_source_count']}",
            f"deferred={report['deferred_source_count']}",
            f"accepted={report['counters']['accepted']}",
            f"inserted={report['counters']['inserted']}",
            f"updated={report['counters']['updated']}",
            f"unchanged={report['counters']['unchanged']}",
            f"reactivated={report['counters']['reactivated']}",
            f"quarantined={report['counters']['quarantined']}",
            f"rejected={report['counters']['rejected']}",
            f"next_batch_or_closeout={str(report['ready_for_next_batch_or_closeout']).lower()}",
            "reconciliation=false",
            "deactivation=false",
            f"manifest={manifest_path}",
            f"report={report_path}",
            f"checkpoint={checkpoint_path}",
            flush=True,
        )
        if report["status"] == "passed_with_partial":
            print(
                "PHASE_7D4B_PARTIAL_OR_DEFERRED_SOURCES",
                json.dumps(report["source_dispositions"], separators=(",", ":")),
                "recurring_retry_enabled=true",
                "missing_count_unchanged=true",
                flush=True,
            )
        elif report["status"] == "failed":
            print(
                "PHASE_7D4B_SAFETY_BLOCKERS",
                json.dumps(report["blockers"], separators=(",", ":")),
                "resume_incomplete=true",
                "reconciliation=false",
                "deactivation=false",
                flush=True,
            )
            raise SystemExit(2)
    except SystemExit:
        raise
    except Exception as exc:
        print(
            "PHASE_7D4B_PROMOTED_BACKFILL_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={exc}",
            f"writes_may_have_occurred={str(writes_started).lower()}",
            "reconciliation=false",
            "deactivation=false",
            f"run_dir={run_dir or '-'}",
            flush=True,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

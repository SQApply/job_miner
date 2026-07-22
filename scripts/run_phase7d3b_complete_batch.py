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
from src.portals.production_guarded_execution import (
    PHASE_7D3B_WRITE_CONFIRMATION,
    batch_checkpoint_path,
    build_phase7d3b_batch_report,
    capture_phase7d3b_source_snapshot,
    load_phase7d3b_authorization,
    require_phase7d3b_checkpoint_state,
    require_phase7d3b_write_confirmation,
    write_phase7d3b_report,
)
from src.portals.production_jobs import init_phase6c_indexes
from src.portals.production_persistence import init_phase6b_indexes
from src.portals.production_quality import init_phase6d_indexes
from src.portals.production_runner import (
    CertificationPhase6ESourceExecutor,
    Phase6EProductionRunner,
    Phase6ERunnerConfig,
    Phase6ESourceResult,
    write_phase6e_manifest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Execute one ordered Phase 7D3B complete-catalog backfill batch. "
            "Partial writes remain idempotently resumable; reconciliation and "
            "deactivation stay disabled."
        )
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--production-plan",
        default="data/phase7d1/phase7d1_production_plan.json",
    )
    parser.add_argument(
        "--rollout-plan",
        default="data/phase7d3/phase7d3a_guarded_rollout_plan.json",
    )
    parser.add_argument("--output-dir", default="data/phase7d3b")
    parser.add_argument("--batch-ordinal", type=int, default=1)
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
        f"catalog_complete={str(result.catalog_complete).lower()}",
        f"error={result.error_type or '-'}",
        flush=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    output_dir = _resolve(root, args.output_dir)
    write_started = False
    run_dir: Path | None = None
    try:
        production_plan, authorization = load_phase7d3b_authorization(
            rollout_plan_path=_resolve(root, args.rollout_plan),
            production_plan_path=_resolve(root, args.production_plan),
            batch_ordinal=args.batch_ordinal,
        )
        require_phase7d3b_write_confirmation(args.confirm_production_writes)
        require_phase7d3b_checkpoint_state(
            output_dir=output_dir,
            authorization=authorization,
            resume_incomplete=args.resume_incomplete,
        )
        print(
            "PHASE_7D3B_AUTHORIZATION_OK",
            f"rollout_id={authorization.rollout_id}",
            f"batch={authorization.batch_ordinal}/{authorization.batch_count}",
            f"batch_id={authorization.batch_id}",
            f"sources={authorization.source_count}",
            f"source_ids={json.dumps(authorization.source_ids, separators=(',', ':'))}",
            "catalog_mode=complete",
            "max_jobs_per_source=unlimited",
            f"page_safety_cap={authorization.page_safety_cap}",
            "source_concurrency=1",
            "gpu_llm_concurrency=1",
            "reconciliation=false",
            "deactivation=false",
            flush=True,
        )

        database = get_mongo_database()
        before = capture_phase7d3b_source_snapshot(
            database,
            source_ids=authorization.source_ids,
        )
        init_phase6b_indexes(database)
        init_phase6c_indexes(database)
        init_phase6d_indexes(database)

        config = Phase6ERunnerConfig(
            execution_mode="write",
            catalog_mode="complete_catalog",
            max_source_concurrency=1,
            max_attempts=authorization.max_attempts,
            retry_backoff_seconds=1,
            max_jobs=None,
            max_pages=authorization.page_safety_cap,
            detail_concurrency=1,
            detail_retry_attempts=authorization.detail_retry_attempts,
            requests_per_minute=authorization.requests_per_minute,
            source_timeout_seconds=authorization.source_timeout_seconds,
            acquisition_timeout_seconds=authorization.acquisition_timeout_seconds,
            allow_llm_fallback=authorization.allow_llm_fallback,
            normalized_job_writes_enabled=True,
            lifecycle_reconciliation_enabled=False,
            deactivation_enabled=False,
        )
        run_id = (
            f"phase7d3b_b{authorization.batch_ordinal:02d}_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "_"
            + uuid.uuid4().hex[:10]
        )
        run_dir = output_dir / f"batch_{authorization.batch_ordinal:02d}" / run_id
        runner = Phase6EProductionRunner(
            plan=production_plan,
            db=database,
            executor=CertificationPhase6ESourceExecutor(
                root=root,
                output_dir=run_dir / "scrape_evidence",
                options=config.certification_options(),
            ),
            config=config,
        )
        write_started = True
        manifest = asyncio.run(
            runner.run(
                requested_source_ids=authorization.source_ids,
                run_id=run_id,
                on_progress=_progress,
            )
        )
        manifest_path = write_phase6e_manifest(run_dir / "manifest.json", manifest)
        after = capture_phase7d3b_source_snapshot(
            database,
            source_ids=authorization.source_ids,
        )
        report = build_phase7d3b_batch_report(
            authorization=authorization,
            manifest=manifest,
            before=before,
            after=after,
        )
        report_path = write_phase7d3b_report(run_dir / "report.json", report)
        checkpoint = write_phase7d3b_report(
            batch_checkpoint_path(output_dir, authorization.batch_ordinal),
            report,
        )
        print(
            "PHASE_7D3B_COMPLETE_BATCH_" + str(report["status"]).upper(),
            f"batch={authorization.batch_ordinal}/{authorization.batch_count}",
            f"sources={manifest.successful_source_count}/{manifest.requested_source_count}",
            f"catalog_complete={report['complete_catalog_source_count']}/{authorization.source_count}",
            f"discovered={report['counters']['discovered']}",
            f"accepted={manifest.accepted_job_count}",
            f"inserted={manifest.inserted_job_count}",
            f"updated={manifest.updated_job_count}",
            f"unchanged={manifest.unchanged_job_count}",
            f"reactivated={manifest.reactivated_job_count}",
            f"quarantined={manifest.quarantined_job_count}",
            "reconciliation=false",
            "deactivation=false",
            f"manifest={manifest_path}",
            f"report={report_path}",
            f"checkpoint={checkpoint}",
            flush=True,
        )
        if report["status"] != "passed":
            print(
                "PHASE_7D3B_BLOCKERS",
                json.dumps(report["blockers"], separators=(",", ":")),
                "writes_occurred=true",
                "next_batch_blocked=true",
                "resume_incomplete=true",
                flush=True,
            )
            raise SystemExit(2)
    except SystemExit:
        raise
    except Exception as exc:
        print(
            "PHASE_7D3B_COMPLETE_BATCH_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={str(exc)}",
            f"writes_may_have_occurred={str(write_started).lower()}",
            "reconciliation=false",
            "deactivation=false",
            f"run_dir={run_dir or '-'}",
            flush=True,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

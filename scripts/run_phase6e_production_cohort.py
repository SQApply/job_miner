from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.mongo import get_mongo_database
from src.portals.production_jobs import init_phase6c_indexes
from src.portals.production_persistence import init_phase6b_indexes, read_phase6a_ingestion_plan
from src.portals.production_quality import init_phase6d_indexes
from src.portals.production_runner import (
    PRODUCTION_WRITE_CONFIRMATION,
    CertificationPhase6ESourceExecutor,
    Phase6EProductionRunner,
    Phase6ERunnerConfig,
    Phase6ESourceResult,
    ProductionRunnerError,
    write_phase6e_manifest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen Phase 5.5C cohort through Phase 6B-6D persistence. "
            "Dry-run is the default; --write requires an explicit confirmation token."
        )
    )
    parser.add_argument("--plan", default="data/phase6a/phase6a_ingestion_plan.json")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="data/phase6e/phase6e_run_manifest.json")
    parser.add_argument("--evidence-dir", default="data/phase6e/certification_evidence")
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm-production-writes", default="")
    parser.add_argument("--max-source-concurrency", type=int, default=2, choices=(1, 2, 3, 4))
    parser.add_argument("--max-attempts", type=int, default=2, choices=(1, 2, 3))
    parser.add_argument("--retry-backoff-seconds", type=float, default=1.0)
    parser.add_argument("--max-jobs", type=int, default=10, choices=range(1, 11))
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--detail-concurrency", type=int, default=1, choices=(1, 2))
    parser.add_argument("--detail-retries", type=int, default=1, choices=(0, 1, 2))
    parser.add_argument("--requests-per-minute", type=int, default=30)
    parser.add_argument("--source-timeout-seconds", type=int, default=600)
    parser.add_argument("--acquisition-timeout-seconds", type=float, default=20.0)
    parser.add_argument("--allow-llm-fallback", action="store_true")
    return parser


def _progress(result: Phase6ESourceResult, completed: int, total: int) -> None:
    print(
        f"[{completed}/{total}]",
        result.status.upper(),
        result.source_id,
        f"attempts={len(result.attempts)}",
        f"extracted={result.extracted_count}",
        f"accepted={result.accepted_count}",
        f"quarantined={result.quarantined_count}",
        f"inserted={result.inserted_count}",
        f"updated={result.updated_count}",
        f"unchanged={result.unchanged_count}",
        f"error={result.error_type or '-'}",
        flush=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.write and args.confirm_production_writes != PRODUCTION_WRITE_CONFIRMATION:
            raise ProductionRunnerError(
                "Write mode requires --confirm-production-writes " + PRODUCTION_WRITE_CONFIRMATION
            )
        plan = read_phase6a_ingestion_plan(Path(args.plan))
        root = Path(args.root).resolve()
        evidence_dir = Path(args.evidence_dir)
        if not evidence_dir.is_absolute():
            evidence_dir = root / evidence_dir
        config = Phase6ERunnerConfig(
            execution_mode="write" if args.write else "dry_run",
            max_source_concurrency=args.max_source_concurrency,
            max_attempts=args.max_attempts,
            retry_backoff_seconds=args.retry_backoff_seconds,
            max_jobs=args.max_jobs,
            max_pages=args.max_pages,
            detail_concurrency=args.detail_concurrency,
            detail_retry_attempts=args.detail_retries,
            requests_per_minute=args.requests_per_minute,
            source_timeout_seconds=args.source_timeout_seconds,
            acquisition_timeout_seconds=args.acquisition_timeout_seconds,
            allow_llm_fallback=bool(args.allow_llm_fallback),
            normalized_job_writes_enabled=bool(args.write),
        )
        db = get_mongo_database()
        created_indexes = {
            **init_phase6b_indexes(db),
            **init_phase6c_indexes(db),
            **init_phase6d_indexes(db),
        }
        runner = Phase6EProductionRunner(
            plan=plan,
            db=db,
            executor=CertificationPhase6ESourceExecutor(
                root=root,
                output_dir=evidence_dir,
                options=config.certification_options(),
            ),
            config=config,
        )
        manifest = asyncio.run(
            runner.run(
                requested_source_ids=args.source_id or None,
                limit=args.limit,
                on_progress=_progress,
            )
        )
        output = write_phase6e_manifest(Path(args.output), manifest)
        print(
            "PHASE_6E_RUN_COMPLETE",
            f"mode={manifest.execution_mode}",
            f"sources={manifest.completed_source_count}/{manifest.requested_source_count}",
            f"success={manifest.successful_source_count}",
            f"failed={manifest.failed_source_count}",
            f"blocked={manifest.blocked_source_count}",
            f"accepted={manifest.accepted_job_count}",
            f"quarantined={manifest.quarantined_job_count}",
            f"inserted={manifest.inserted_job_count}",
            f"updated={manifest.updated_job_count}",
            f"unchanged={manifest.unchanged_job_count}",
            "reconciliation=false",
            "deactivation=false",
            f"indexes={sum(created_indexes.values())}",
            f"output={output}",
        )
    except Exception as exc:
        print(
            "PHASE_6E_RUN_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={str(exc)}",
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

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
from src.portals.production_closeout import (
    build_phase6f_closeout_report,
    read_phase6e_manifest,
    write_phase6f_report,
)
from src.portals.production_idempotency import (
    PHASE_7D2C_WRITE_CONFIRMATION,
    build_phase7d2c_report,
    load_phase7d2c_authorization,
    require_phase7d2c_database_preflight,
    require_phase7d2c_write_confirmation,
    write_phase7d2c_report,
)
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
            "Run the one-time Phase 7D2C idempotency rerun for the two Phase 7D2B "
            "sources. The rerun must produce only unchanged jobs."
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
    parser.add_argument("--plan", default="data/phase7d1/phase7d1_production_plan.json")
    parser.add_argument(
        "--pilot-report",
        default="data/phase7d2a/phase7d2a_pilot_report.json",
    )
    parser.add_argument(
        "--pilot-manifest",
        default="data/phase7d2a/phase7d2a_run_manifest.json",
    )
    parser.add_argument(
        "--first-write-report",
        default="data/phase7d2b/phase7d2b_write_report.json",
    )
    parser.add_argument(
        "--first-write-manifest",
        default="data/phase7d2b/phase7d2b_first_write_manifest.json",
    )
    parser.add_argument(
        "--output",
        default="data/phase7d2c/phase7d2c_rerun_manifest.json",
    )
    parser.add_argument(
        "--closeout-report",
        default="data/phase7d2c/phase7d2c_phase6f_closeout.json",
    )
    parser.add_argument(
        "--report",
        default="data/phase7d2c/phase7d2c_idempotency_report.json",
    )
    parser.add_argument(
        "--evidence-dir",
        default="data/phase7d2c/certification_evidence",
    )
    parser.add_argument("--confirm-idempotency-rerun", default="")
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
        f"accepted={result.accepted_count}",
        f"inserted={result.inserted_count}",
        f"updated={result.updated_count}",
        f"unchanged={result.unchanged_count}",
        f"reactivated={result.reactivated_count}",
        f"quarantined={result.quarantined_count}",
        f"error={result.error_type or '-'}",
        flush=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    output_path = _resolve(root, args.output)
    closeout_path = _resolve(root, args.closeout_report)
    report_path = _resolve(root, args.report)
    first_manifest_path = _resolve(root, args.first_write_manifest)
    write_started = False
    try:
        plan, authorization = load_phase7d2c_authorization(
            cohort_path=_resolve(root, args.cohort),
            quality_report_path=_resolve(root, args.quality_report),
            plan_path=_resolve(root, args.plan),
            pilot_report_path=_resolve(root, args.pilot_report),
            pilot_manifest_path=_resolve(root, args.pilot_manifest),
            first_write_report_path=_resolve(root, args.first_write_report),
            first_write_manifest_path=first_manifest_path,
        )
        require_phase7d2c_write_confirmation(args.confirm_idempotency_rerun)
        database = get_mongo_database()
        before = require_phase7d2c_database_preflight(
            database,
            authorization=authorization,
        )
        print(
            "PHASE_7D2C_AUTHORIZATION_OK",
            f"sources={authorization.expected_source_count}",
            f"jobs={authorization.expected_job_count}",
            f"source_ids={json.dumps(authorization.source_ids, separators=(',', ':'))}",
            f"first_write_run_id={authorization.first_write_run_id}",
            "expected_inserted=0",
            "expected_updated=0",
            f"expected_unchanged={authorization.expected_job_count}",
            "reconciliation=false",
            "deactivation=false",
            flush=True,
        )

        config = Phase6ERunnerConfig(
            execution_mode="write",
            max_source_concurrency=1,
            max_attempts=1,
            retry_backoff_seconds=0,
            max_jobs=10,
            max_pages=3,
            detail_concurrency=1,
            detail_retry_attempts=0,
            requests_per_minute=30,
            source_timeout_seconds=600,
            acquisition_timeout_seconds=25,
            allow_llm_fallback=True,
            normalized_job_writes_enabled=True,
            lifecycle_reconciliation_enabled=False,
            deactivation_enabled=False,
        )
        runner = Phase6EProductionRunner(
            plan=plan,
            db=database,
            executor=CertificationPhase6ESourceExecutor(
                root=root,
                output_dir=_resolve(root, args.evidence_dir),
                options=config.certification_options(),
            ),
            config=config,
        )
        run_id = (
            "phase7d2c_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "_"
            + uuid.uuid4().hex[:10]
        )
        write_started = True
        manifest = asyncio.run(
            runner.run(
                requested_source_ids=authorization.source_ids,
                run_id=run_id,
                on_progress=_progress,
            )
        )
        rerun_manifest_path = write_phase6e_manifest(output_path, manifest)
        first_manifest = read_phase6e_manifest(first_manifest_path)
        closeout = build_phase6f_closeout_report(
            plan=plan,
            first_write_manifest=first_manifest,
            rerun_manifest=manifest,
            first_write_manifest_path=first_manifest_path,
            rerun_manifest_path=rerun_manifest_path,
            db=database,
            expected_source_count=authorization.expected_source_count,
        )
        closeout_output = write_phase6f_report(closeout_path, closeout)
        report = build_phase7d2c_report(
            authorization=authorization,
            rerun_manifest=manifest.model_dump(mode="json"),
            rerun_manifest_path=rerun_manifest_path,
            closeout_report=closeout.model_dump(mode="json"),
            closeout_report_path=closeout_output,
            database=database,
            before=before,
        )
        report_output = write_phase7d2c_report(report_path, report)
        print(
            "PHASE_7D2C_IDEMPOTENCY_" + str(report["status"]).upper(),
            f"sources={manifest.successful_source_count}/{manifest.requested_source_count}",
            f"accepted={manifest.accepted_job_count}",
            f"inserted={manifest.inserted_job_count}",
            f"updated={manifest.updated_job_count}",
            f"unchanged={manifest.unchanged_job_count}",
            f"phase6f={closeout.status}",
            "reconciliation=false",
            "deactivation=false",
            f"manifest={rerun_manifest_path}",
            f"closeout={closeout_output}",
            f"report={report_output}",
            flush=True,
        )
        if report["status"] != "passed":
            print(
                "PHASE_7D2C_BLOCKERS",
                json.dumps(report["blockers"], separators=(",", ":")),
                "writes_occurred=true",
                flush=True,
            )
            raise SystemExit(2)
    except SystemExit:
        raise
    except Exception as exc:
        print(
            "PHASE_7D2C_IDEMPOTENCY_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={str(exc)}",
            f"writes_may_have_occurred={str(write_started).lower()}",
            "automatic_rollback=false",
            f"manifest={output_path}",
            f"closeout={closeout_path}",
            f"report={report_path}",
            flush=True,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

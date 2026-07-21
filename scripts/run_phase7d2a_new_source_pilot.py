from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.mongo import get_mongo_database
from src.portals.production_pilot import (
    ProductionPilotError,
    ReadOnlyDatabaseProxy,
    build_phase7d2a_pilot_report,
    load_phase7d2a_pilot_inputs,
    write_phase7d2a_pilot_report,
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
            "Run the two Phase 7D1 repair additions as a read-only Phase 7D2A "
            "production-path pilot. Source ids and safety limits are evidence-derived "
            "and cannot be overridden from the command line."
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
        "--plan",
        default="data/phase7d1/phase7d1_production_plan.json",
    )
    parser.add_argument(
        "--output",
        default="data/phase7d2a/phase7d2a_run_manifest.json",
    )
    parser.add_argument(
        "--report",
        default="data/phase7d2a/phase7d2a_pilot_report.json",
    )
    parser.add_argument(
        "--evidence-dir",
        default="data/phase7d2a/certification_evidence",
    )
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
        f"extracted={result.extracted_count}",
        f"accepted={result.accepted_count}",
        f"quarantined={result.quarantined_count}",
        f"would_insert={result.inserted_count}",
        f"would_update={result.updated_count}",
        f"unchanged={result.unchanged_count}",
        f"error={result.error_type or '-'}",
        flush=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    output_path = _resolve(root, args.output)
    report_path = _resolve(root, args.report)
    try:
        plan, selection = load_phase7d2a_pilot_inputs(
            cohort_path=_resolve(root, args.cohort),
            quality_report_path=_resolve(root, args.quality_report),
            plan_path=_resolve(root, args.plan),
        )
        print(
            "PHASE_7D2A_PLAN_OK",
            f"sources={selection.expected_source_count}",
            f"source_ids={json.dumps(selection.source_ids, separators=(',', ':'))}",
            "gpu_detail_concurrency=1",
            "source_concurrency=1",
            "writes=false",
            "reconciliation=false",
            "deactivation=false",
            flush=True,
        )

        config = Phase6ERunnerConfig(
            execution_mode="dry_run",
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
            normalized_job_writes_enabled=False,
            lifecycle_reconciliation_enabled=False,
            deactivation_enabled=False,
        )
        database = ReadOnlyDatabaseProxy(get_mongo_database())
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
        manifest = asyncio.run(
            runner.run(
                requested_source_ids=selection.source_ids,
                on_progress=_progress,
            )
        )
        manifest_output = write_phase6e_manifest(output_path, manifest)
        report = build_phase7d2a_pilot_report(
            selection=selection,
            manifest=manifest.model_dump(mode="json"),
            database_write_guard_enabled=True,
        )
        report_output = write_phase7d2a_pilot_report(report_path, report)
        print(
            "PHASE_7D2A_PILOT_" + str(report["status"]).upper(),
            f"sources={manifest.successful_source_count}/{manifest.requested_source_count}",
            f"accepted={manifest.accepted_job_count}",
            f"quarantined={manifest.quarantined_job_count}",
            f"would_insert={manifest.inserted_job_count}",
            f"would_update={manifest.updated_job_count}",
            "writes=false",
            "indexes=0",
            "reconciliation=false",
            "deactivation=false",
            f"manifest={manifest_output}",
            f"report={report_output}",
            flush=True,
        )
        if report["status"] != "passed":
            print(
                "PHASE_7D2A_BLOCKERS",
                json.dumps(report["blockers"], separators=(",", ":")),
                flush=True,
            )
            raise SystemExit(2)
    except (FileNotFoundError, ProductionPilotError, ValueError) as exc:
        print(
            "PHASE_7D2A_PILOT_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={str(exc)}",
            f"manifest={output_path}",
            f"report={report_path}",
            flush=True,
        )
        raise SystemExit(2) from exc
    except Exception as exc:
        print(
            "PHASE_7D2A_PILOT_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={str(exc)}",
            f"manifest={output_path}",
            f"report={report_path}",
            flush=True,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

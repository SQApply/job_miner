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
from src.portals.certification import PortalCertificationRecord
from src.portals.production_jobs import init_phase6c_indexes
from src.portals.production_persistence import init_phase6b_indexes, read_phase6a_ingestion_plan
from src.portals.production_quality import init_phase6d_indexes
from src.portals.production_runner import (
    Phase6EProductionRunner,
    Phase6ERunnerConfig,
    write_phase6e_manifest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Phase 6E retry, source isolation, persistence, and manifest generation "
            "with synthetic certified-source responses. No websites are contacted."
        )
    )
    parser.add_argument("--plan", default="data/phase6a/phase6a_ingestion_plan.json")
    parser.add_argument("--output", default="data/phase6e/phase6e_runner_validation.json")
    parser.add_argument("--keep-records", action="store_true")
    return parser


def _record(source, *, run_id: str, attempt: int, status: str, error_type=None, error_message=None):
    token = uuid.uuid4().hex
    jobs = []
    extracted = 0
    if status == "success":
        route = (source.resolved_route_url or source.listing_url).rstrip("/")
        jobs = [
            {
                "title": "Phase 6E Production Data Engineer",
                "job_url": f"{route}/phase6e-validation/{token}",
                "company": source.display_name,
                "location_text": "Remote",
                "employment_type": "Full-time",
                "posted_date": "2026-07-17",
                "summary": (
                    "Design, build, test, operate, and monitor reliable production data "
                    "pipelines with Python and MongoDB for critical customer workflows."
                ),
                "required_skills": ["Python", "MongoDB"],
                "job_reference": f"phase6e-{token}",
            }
        ]
        extracted = 1
    now = datetime.now(timezone.utc).isoformat()
    return PortalCertificationRecord(
        contract_version="1.3",
        run_id=run_id,
        attempt_number=attempt,
        source_id=source.source_id,
        display_name=source.display_name,
        provided_url=source.listing_url,
        effective_listing_url=source.resolved_route_url or source.listing_url,
        status=status,
        certification_status="passed" if status == "success" else "needs_repair",
        stage="complete",
        detected_platform=source.detected_platform,
        detected_profile=None,
        discovered_urls=extracted,
        attempted_urls=extracted,
        extracted_jobs=extracted,
        sample_jobs=jobs,
        acquisition={"strategy": "synthetic_validation", "trusted_hosts": []},
        detail_failures=[],
        rejected_urls=0,
        event_counts={},
        gpu_before={},
        gpu_after={},
        started_at=now,
        completed_at=now,
        elapsed_seconds=0.01,
        error_type=error_type,
        error_message=error_message,
    )


class _SyntheticExecutor:
    def __init__(self, retry_source_id: str, failed_source_id: str):
        self.retry_source_id = retry_source_id
        self.failed_source_id = failed_source_id
        self.calls: dict[str, int] = {}

    async def execute(self, source, *, attempt_number: int, run_id: str):
        self.calls[source.source_id] = self.calls.get(source.source_id, 0) + 1
        if source.source_id == self.retry_source_id and attempt_number == 1:
            return _record(
                source,
                run_id=run_id,
                attempt=attempt_number,
                status="failed",
                error_type="network_error",
                error_message="temporary connection reset",
            )
        if source.source_id == self.failed_source_id:
            return _record(
                source,
                run_id=run_id,
                attempt=attempt_number,
                status="failed",
                error_type="zero_discovery",
                error_message="no valid job urls",
            )
        return _record(source, run_id=run_id, attempt=attempt_number, status="success")


def main() -> None:
    args = build_parser().parse_args()
    fleet_run_id = None
    job_ids: list[str] = []
    cleanup = None
    try:
        plan = read_phase6a_ingestion_plan(Path(args.plan))
        selected = plan.sources[:2]
        if len(selected) < 2:
            raise RuntimeError("Phase 6E validation requires at least two cohort sources")
        db = get_mongo_database()
        created_indexes = {
            **init_phase6b_indexes(db),
            **init_phase6c_indexes(db),
            **init_phase6d_indexes(db),
        }
        executor = _SyntheticExecutor(selected[0].source_id, selected[1].source_id)
        runner = Phase6EProductionRunner(
            plan=plan,
            db=db,
            executor=executor,
            config=Phase6ERunnerConfig(
                execution_mode="write",
                normalized_job_writes_enabled=True,
                max_source_concurrency=2,
                max_attempts=2,
                retry_backoff_seconds=0,
                max_jobs=1,
            ),
        )
        manifest = asyncio.run(
            runner.run(requested_source_ids=[source.source_id for source in selected])
        )
        fleet_run_id = manifest.run_id
        current_jobs = list(db["jobs_current"].find({"last_fleet_run_id": fleet_run_id}))
        job_ids = [str(row.get("job_id")) for row in current_jobs if row.get("job_id")]
        if manifest.successful_source_count != 1 or manifest.failed_source_count != 1:
            raise RuntimeError("Phase 6E source isolation invariant failed")
        if executor.calls.get(selected[0].source_id) != 2:
            raise RuntimeError("Phase 6E transient retry invariant failed")
        if executor.calls.get(selected[1].source_id) != 1:
            raise RuntimeError("Phase 6E permanent failure was retried")
        if manifest.inserted_job_count != 1 or len(current_jobs) != 1:
            raise RuntimeError("Phase 6E normalized persistence invariant failed")
        output_manifest = write_phase6e_manifest(Path(args.output), manifest)
        payload = manifest.model_dump(mode="json")
        payload.update(
            {
                "created_indexes": created_indexes,
                "retry_source_calls": executor.calls.get(selected[0].source_id),
                "permanent_failure_calls": executor.calls.get(selected[1].source_id),
                "current_job_count": len(current_jobs),
                "records_kept": bool(args.keep_records),
            }
        )
        if not args.keep_records:
            jobs_current = db["jobs_current"].delete_many({"last_fleet_run_id": fleet_run_id})
            jobs_history = db["jobs_history"].delete_many({"run_session_id": {"$in": [row.source_run_id for row in manifest.source_results if row.source_run_id]}})
            quarantine = db["production_job_quarantine"].delete_many({"fleet_run_id": fleet_run_id})
            raw = db["production_raw_job_evidence"].delete_many({"fleet_run_id": fleet_run_id})
            sources = db["production_ingestion_source_runs"].delete_many({"fleet_run_id": fleet_run_id})
            fleet = db["production_ingestion_fleet_runs"].delete_many({"fleet_run_id": fleet_run_id})
            cleanup = {
                "jobs_current": jobs_current.deleted_count,
                "jobs_history": jobs_history.deleted_count,
                "job_quarantine": quarantine.deleted_count,
                "raw_evidence": raw.deleted_count,
                "source_runs": sources.deleted_count,
                "fleet_runs": fleet.deleted_count,
            }
            payload["cleanup"] = cleanup
        target = Path(args.output).resolve()
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(target)
        print(
            "PHASE_6E_RUNNER_OK",
            f"sources={manifest.completed_source_count}",
            f"success={manifest.successful_source_count}",
            f"failed={manifest.failed_source_count}",
            f"retry_calls={executor.calls.get(selected[0].source_id)}",
            f"permanent_failure_calls={executor.calls.get(selected[1].source_id)}",
            f"inserted={manifest.inserted_job_count}",
            "reconciliation=false",
            "deactivation=false",
            f"records_kept={str(args.keep_records).lower()}",
            f"output={output_manifest}",
        )
    except Exception as exc:
        if fleet_run_id and not args.keep_records:
            try:
                db = get_mongo_database()
                db["jobs_current"].delete_many({"last_fleet_run_id": fleet_run_id})
                db["jobs_history"].delete_many({"payload.fleet_run_id": fleet_run_id})
                db["production_job_quarantine"].delete_many({"fleet_run_id": fleet_run_id})
                db["production_raw_job_evidence"].delete_many({"fleet_run_id": fleet_run_id})
                db["production_ingestion_source_runs"].delete_many({"fleet_run_id": fleet_run_id})
                db["production_ingestion_fleet_runs"].delete_many({"fleet_run_id": fleet_run_id})
            except Exception:
                pass
        print(
            "PHASE_6E_RUNNER_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={str(exc)}",
            f"cleanup={cleanup}",
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.mongo import get_mongo_database
from src.portals.production_jobs import (
    Phase6CNormalizedJobInput,
    ProductionJobUpsertRepository,
    init_phase6c_indexes,
)
from src.portals.production_persistence import (
    Phase6BRawEvidenceInput,
    Phase6BSourceCounters,
    ProductionIngestionPersistenceRepository,
    ProductionPersistenceError,
    init_phase6b_indexes,
    read_phase6a_ingestion_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Phase 6C stable identity and idempotent normalized-job upsert. "
            "The command writes one synthetic job, verifies inserted/unchanged/updated "
            "outcomes, and removes all synthetic records unless --keep-records is used."
        )
    )
    parser.add_argument("--plan", default="data/phase6a/phase6a_ingestion_plan.json")
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--output", default="data/phase6c/phase6c_job_upsert_validation.json")
    parser.add_argument("--keep-records", action="store_true")
    return parser


def _write_result(path: Path, payload: dict) -> Path:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(target)
    return target


def main() -> None:
    args = build_parser().parse_args()
    audit: ProductionIngestionPersistenceRepository | None = None
    jobs: ProductionJobUpsertRepository | None = None
    fleet_run_id: str | None = None
    job_id: str | None = None
    cleanup: dict | None = None
    try:
        plan = read_phase6a_ingestion_plan(Path(args.plan))
        selected = args.source_id or plan.selected_source_ids[0]
        source = next((item for item in plan.sources if item.source_id == selected), None)
        if source is None:
            raise ProductionPersistenceError(f"SOURCE_NOT_IN_PHASE_6A_PLAN: {selected}")

        db = get_mongo_database()
        created_indexes = {
            **init_phase6b_indexes(db),
            **init_phase6c_indexes(db),
        }
        audit = ProductionIngestionPersistenceRepository(db)
        jobs = ProductionJobUpsertRepository(db)
        fleet = audit.create_fleet_run(
            plan=plan,
            selected_source_ids=[selected],
            normalized_job_writes_enabled=True,
        )
        fleet_run_id = fleet.fleet_run_id
        source_run = audit.start_source_run(
            fleet_run_id=fleet_run_id,
            source=source,
            extractor_version="phase6c-validation-1.0",
            metadata={"synthetic_validation": True},
        )

        token = uuid.uuid4().hex
        job_url = (source.resolved_route_url or source.listing_url).rstrip("/") + f"/phase6c-validation/{token}"
        external_id = f"phase6c-{token}"
        initial_payload = {
            "external_job_id": external_id,
            "canonical_url": job_url + "?utm_source=phase6c",
            "job_url": job_url + "?utm_campaign=validation",
            "title": "Phase 6C Data Engineer",
            "company": source.display_name,
            "location_text": "Remote",
            "description": "Build reliable production data pipelines.",
            "employment_type": "Full Time",
            "posted_date": "3 days ago",
            "required_skills": ["Python", "MongoDB"],
        }
        evidence_one, _ = audit.store_raw_evidence(
            fleet_run_id=fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source.source_id,
            evidence=Phase6BRawEvidenceInput(
                source_url=job_url,
                canonical_url=job_url,
                external_job_id=external_id,
                payload=initial_payload,
                extractor_name="phase6c_validation",
                extractor_version="1.0",
                metadata={"synthetic_validation": True},
            ),
        )

        inserted = jobs.upsert_job(
            fleet_run_id=fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source.source_id,
            raw_evidence_id=evidence_one.evidence_id,
            job=Phase6CNormalizedJobInput.model_validate(initial_payload),
        )
        job_id = inserted.job_id

        unchanged_payload = dict(initial_payload)
        unchanged_payload["canonical_url"] = job_url + "?utm_source=different"
        unchanged_payload["job_url"] = job_url
        unchanged_payload["title"] = "  Phase 6C   Data Engineer  "
        unchanged_payload["posted_date"] = "4 days ago"
        unchanged = jobs.upsert_job(
            fleet_run_id=fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source.source_id,
            raw_evidence_id=evidence_one.evidence_id,
            job=Phase6CNormalizedJobInput.model_validate(unchanged_payload),
        )

        updated_payload = dict(initial_payload)
        updated_payload["description"] = "Build, operate, and monitor reliable production data pipelines."
        evidence_two, _ = audit.store_raw_evidence(
            fleet_run_id=fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source.source_id,
            evidence=Phase6BRawEvidenceInput(
                source_url=job_url,
                canonical_url=job_url,
                external_job_id=external_id,
                payload=updated_payload,
                extractor_name="phase6c_validation",
                extractor_version="1.0",
                metadata={"synthetic_validation": True, "content_changed": True},
            ),
        )
        updated = jobs.upsert_job(
            fleet_run_id=fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source.source_id,
            raw_evidence_id=evidence_two.evidence_id,
            job=Phase6CNormalizedJobInput.model_validate(updated_payload),
        )

        current_count = db["jobs_current"].count_documents({"job_id": job_id})
        history_count = db["jobs_history"].count_documents({"job_id": job_id})
        current = jobs.get_job(job_id)
        if [inserted.outcome, unchanged.outcome, updated.outcome] != ["inserted", "unchanged", "updated"]:
            raise RuntimeError("Unexpected Phase 6C outcome sequence")
        if current_count != 1 or history_count != 2:
            raise RuntimeError("Phase 6C duplicate/history invariant failed")
        if not current or current.get("is_active") is not True or current.get("deactivated_at") is not None:
            raise RuntimeError("Phase 6C active/deactivation invariant failed")

        finalized_source = audit.finalize_source_run(
            source_run_id=source_run.source_run_id,
            status="success",
            counters=Phase6BSourceCounters(
                discovered_count=1,
                attempted_count=3,
                extracted_count=3,
                valid_count=3,
            ),
            acquisition_strategy="synthetic_validation",
            metadata={"phase6c_job_id": job_id},
        )
        finalized_fleet = audit.finalize_fleet_run(fleet_run_id=fleet_run_id)
        payload = {
            "contract_version": "1.0",
            "phase": "6C",
            "fleet_run_id": fleet_run_id,
            "source_run_id": source_run.source_run_id,
            "source_id": source.source_id,
            "job_id": job_id,
            "identity_strategy": inserted.identity_strategy,
            "identity_hash": inserted.identity_hash,
            "outcomes": [inserted.outcome, unchanged.outcome, updated.outcome],
            "current_job_count": current_count,
            "history_version_count": history_count,
            "final_version": updated.version,
            "source_counts": {
                "inserted": finalized_source.inserted_job_count,
                "updated": finalized_source.updated_job_count,
                "unchanged": finalized_source.unchanged_job_count,
                "reactivated": finalized_source.reactivated_job_count,
            },
            "fleet_counts": {
                "inserted": finalized_fleet.inserted_job_count,
                "updated": finalized_fleet.updated_job_count,
                "unchanged": finalized_fleet.unchanged_job_count,
                "reactivated": finalized_fleet.reactivated_job_count,
            },
            "created_indexes": created_indexes,
            "normalized_job_writes_enabled": True,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "records_kept": bool(args.keep_records),
        }
        if not args.keep_records:
            cleanup = {
                **jobs.delete_validation_job(job_id),
                **audit.delete_validation_run(fleet_run_id),
            }
            payload["cleanup"] = cleanup
        output = _write_result(Path(args.output), payload)
    except Exception as exc:
        if not args.keep_records:
            try:
                cleanup = {}
                if jobs is not None and job_id:
                    cleanup.update(jobs.delete_validation_job(job_id))
                if audit is not None and fleet_run_id:
                    cleanup.update(audit.delete_validation_run(fleet_run_id))
            except Exception:
                cleanup = None
        print(
            "PHASE_6C_JOB_UPSERT_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={exc}",
            f"cleanup={cleanup}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2) from exc

    print(
        "PHASE_6C_JOB_UPSERT_OK",
        "outcomes=inserted,unchanged,updated",
        "current_jobs=1",
        "history_versions=2",
        "duplicates=0",
        "normalized_job_writes=true",
        "reconciliation=false",
        "deactivation=false",
        f"records_kept={str(bool(args.keep_records)).lower()}",
        f"output={output}",
        flush=True,
    )


if __name__ == "__main__":
    main()

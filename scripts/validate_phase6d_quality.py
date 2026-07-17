from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.mongo import get_mongo_database
from src.portals.production_jobs import init_phase6c_indexes
from src.portals.production_persistence import (
    Phase6BRawEvidenceInput,
    Phase6BSourceCounters,
    ProductionIngestionPersistenceRepository,
    ProductionPersistenceError,
    init_phase6b_indexes,
    read_phase6a_ingestion_plan,
)
from src.portals.production_quality import (
    ProductionJobQualityRepository,
    init_phase6d_indexes,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Phase 6D normalization, quality controls, and quarantine. "
            "The command writes one valid synthetic job and one invalid record, "
            "proves the invalid record cannot overwrite the valid job, then cleans up."
        )
    )
    parser.add_argument("--plan", default="data/phase6a/phase6a_ingestion_plan.json")
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--output", default="data/phase6d/phase6d_quality_validation.json")
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
    quality: ProductionJobQualityRepository | None = None
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
            **init_phase6d_indexes(db),
        }
        audit = ProductionIngestionPersistenceRepository(db)
        quality = ProductionJobQualityRepository(db)
        fleet = audit.create_fleet_run(
            plan=plan,
            selected_source_ids=[selected],
            normalized_job_writes_enabled=True,
        )
        fleet_run_id = fleet.fleet_run_id
        source_run = audit.start_source_run(
            fleet_run_id=fleet_run_id,
            source=source,
            extractor_version="phase6d-validation-1.0",
            metadata={"synthetic_validation": True},
        )

        token = uuid.uuid4().hex
        route = (source.resolved_route_url or source.listing_url).rstrip("/")
        job_url = route + f"/phase6d-validation/{token}"
        external_id = f"phase6d-{token}"
        host = urlsplit(job_url).hostname or urlsplit(source.listing_url).hostname
        valid_payload = {
            "jobId": external_id,
            "jobUrl": job_url + "?utm_source=phase6d",
            "jobTitle": "Phase 6D Production Data Engineer",
            "companyName": source.display_name,
            "jobLocation": "Remote",
            "jobDescription": (
                "Design, build, test, operate, and monitor reliable production data "
                "pipelines with Python and MongoDB while maintaining auditability."
            ),
            "jobType": "full time",
            "datePosted": "2026-07-17",
            "skills": ["Python", "MongoDB"],
        }
        valid_evidence, _ = audit.store_raw_evidence(
            fleet_run_id=fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source.source_id,
            evidence=Phase6BRawEvidenceInput(
                source_url=job_url,
                canonical_url=job_url,
                external_job_id=external_id,
                payload=valid_payload,
                extractor_name="phase6d_validation",
                extractor_version="1.0",
                metadata={"synthetic_validation": True, "quality": "valid"},
            ),
        )
        accepted = quality.process_raw_evidence(
            fleet_run_id=fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source.source_id,
            raw_evidence_id=valid_evidence.evidence_id,
            trusted_hosts=[host] if host else None,
        )
        if accepted.status != "accepted" or accepted.upsert is None:
            raise RuntimeError("Phase 6D valid candidate was not accepted")
        job_id = accepted.upsert.job_id
        current_before = db["jobs_current"].find_one({"job_id": job_id})
        if not current_before:
            raise RuntimeError("Phase 6D accepted job was not persisted")

        invalid_payload = dict(valid_payload)
        invalid_payload["jobDescription"] = "Access denied. Verify you are human before continuing."
        invalid_evidence, _ = audit.store_raw_evidence(
            fleet_run_id=fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source.source_id,
            evidence=Phase6BRawEvidenceInput(
                source_url=job_url,
                canonical_url=job_url,
                external_job_id=external_id,
                payload=invalid_payload,
                extractor_name="phase6d_validation",
                extractor_version="1.0",
                metadata={"synthetic_validation": True, "quality": "invalid"},
            ),
        )
        quarantined = quality.process_raw_evidence(
            fleet_run_id=fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source.source_id,
            raw_evidence_id=invalid_evidence.evidence_id,
            trusted_hosts=[host] if host else None,
        )
        duplicate_quarantine = quality.process_raw_evidence(
            fleet_run_id=fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source.source_id,
            raw_evidence_id=invalid_evidence.evidence_id,
            trusted_hosts=[host] if host else None,
        )
        current_after = db["jobs_current"].find_one({"job_id": job_id})
        quarantine_count = db["production_job_quarantine"].count_documents(
            {"fleet_run_id": fleet_run_id}
        )
        if quarantined.status != "quarantined" or quarantine_count != 1:
            raise RuntimeError("Phase 6D quarantine invariant failed")
        if duplicate_quarantine.quarantine_inserted is not False:
            raise RuntimeError("Phase 6D duplicate quarantine was inserted")
        if current_after.get("content_hash") != current_before.get("content_hash") or current_after.get("version") != 1:
            raise RuntimeError("Invalid Phase 6D candidate overwrote valid production data")

        finalized_source = audit.finalize_source_run(
            source_run_id=source_run.source_run_id,
            status="success",
            counters=Phase6BSourceCounters(
                discovered_count=2,
                attempted_count=2,
                extracted_count=2,
                valid_count=1,
                quarantined_count=1,
            ),
            acquisition_strategy="synthetic_validation",
            metadata={"phase6d_job_id": job_id, "phase6d_quarantine_id": quarantined.quarantine_id},
        )
        finalized_fleet = audit.finalize_fleet_run(fleet_run_id=fleet_run_id)
        payload = {
            "contract_version": "1.0",
            "phase": "6D",
            "fleet_run_id": fleet_run_id,
            "source_run_id": source_run.source_run_id,
            "source_id": source.source_id,
            "job_id": job_id,
            "accepted_status": accepted.status,
            "accepted_outcome": accepted.upsert.outcome,
            "accepted_quality_score": accepted.quality_scores.overall,
            "quarantine_status": quarantined.status,
            "quarantine_id": quarantined.quarantine_id,
            "quarantine_reasons": quarantined.reason_codes,
            "quarantine_count": quarantine_count,
            "duplicate_quarantine_inserted": duplicate_quarantine.quarantine_inserted,
            "valid_job_preserved": current_after.get("content_hash") == current_before.get("content_hash"),
            "current_job_count": db["jobs_current"].count_documents({"job_id": job_id}),
            "history_version_count": db["jobs_history"].count_documents({"job_id": job_id}),
            "source_counts": {
                "valid": finalized_source.valid_count,
                "quarantined": finalized_source.quarantined_count,
                "inserted": finalized_source.inserted_job_count,
            },
            "fleet_counts": {
                "quarantined": finalized_fleet.quarantined_job_count,
                "inserted": finalized_fleet.inserted_job_count,
            },
            "created_indexes": created_indexes,
            "normalized_job_writes_enabled": True,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "records_kept": bool(args.keep_records),
        }
        if not args.keep_records:
            cleanup = quality.delete_validation_records(fleet_run_id=fleet_run_id, job_id=job_id)
            payload["cleanup"] = cleanup
        output = _write_result(Path(args.output), payload)
    except Exception as exc:
        if not args.keep_records:
            try:
                cleanup = quality.delete_validation_records(fleet_run_id=fleet_run_id, job_id=job_id) if quality and fleet_run_id else None
            except Exception:
                cleanup = None
        print(
            "PHASE_6D_QUALITY_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={exc}",
            f"cleanup={cleanup}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2) from exc

    print(
        "PHASE_6D_QUALITY_OK",
        "accepted=1",
        "quarantined=1",
        "duplicate_quarantine=0",
        "valid_job_preserved=true",
        "current_jobs=1",
        "history_versions=1",
        "normalized_job_writes=true",
        "reconciliation=false",
        "deactivation=false",
        f"records_kept={str(bool(args.keep_records)).lower()}",
        f"output={output}",
        flush=True,
    )


if __name__ == "__main__":
    main()

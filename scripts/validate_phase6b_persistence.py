from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.mongo import get_mongo_database
from src.portals.production_persistence import (
    Phase6BLiveValidationResult,
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
            "Validate Phase 6B MongoDB persistence with one synthetic fleet run, "
            "one source run, and one immutable raw-evidence record. No website is scraped "
            "and no normalized job is written."
        )
    )
    parser.add_argument(
        "--plan",
        default="data/phase6a/phase6a_ingestion_plan.json",
        help="Validated Phase 6A ingestion plan",
    )
    parser.add_argument(
        "--source-id",
        default=None,
        help="Optional source from the Phase 6A plan; defaults to its first source",
    )
    parser.add_argument(
        "--output",
        default="data/phase6b/phase6b_persistence_validation.json",
        help="Local JSON validation result",
    )
    parser.add_argument(
        "--keep-records",
        action="store_true",
        help="Keep synthetic validation records in MongoDB instead of cleaning them up",
    )
    return parser


def _write_result(path: Path, payload: dict) -> Path:
    target = Path(path).resolve()
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
    fleet_run_id: str | None = None
    repository: ProductionIngestionPersistenceRepository | None = None
    cleanup: dict[str, int] | None = None
    try:
        plan = read_phase6a_ingestion_plan(Path(args.plan))
        selected = args.source_id or plan.selected_source_ids[0]
        source_map = {source.source_id: source for source in plan.sources}
        source = source_map.get(selected)
        if source is None:
            raise ProductionPersistenceError(
                f"SOURCE_NOT_IN_PHASE_6A_PLAN: {selected}"
            )

        db = get_mongo_database()
        created_indexes = init_phase6b_indexes(db)
        repository = ProductionIngestionPersistenceRepository(db)
        fleet = repository.create_fleet_run(
            plan=plan,
            selected_source_ids=[selected],
        )
        fleet_run_id = fleet.fleet_run_id
        source_run = repository.start_source_run(
            fleet_run_id=fleet.fleet_run_id,
            source=source,
            extractor_version="phase6b-validation-1.0",
            metadata={"synthetic_validation": True},
        )
        evidence_input = Phase6BRawEvidenceInput(
            source_url=source.resolved_route_url or source.listing_url,
            canonical_url=(source.resolved_route_url or source.listing_url).rstrip("/")
            + "/phase6b-validation-job",
            external_job_id="phase6b-validation-job",
            payload={
                "title": "Phase 6B Persistence Validation",
                "company": source.display_name,
                "source_id": source.source_id,
                "synthetic_validation": True,
            },
            payload_format="normalized_scraper_output",
            extractor_name="phase6b_validation",
            extractor_version="1.0",
            metadata={"must_not_enter_jobs_current": True},
        )
        evidence, inserted = repository.store_raw_evidence(
            fleet_run_id=fleet.fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source.source_id,
            evidence=evidence_input,
        )
        duplicate, duplicate_inserted = repository.store_raw_evidence(
            fleet_run_id=fleet.fleet_run_id,
            source_run_id=source_run.source_run_id,
            source_id=source.source_id,
            evidence=evidence_input,
        )
        if duplicate.evidence_id != evidence.evidence_id:
            raise ProductionPersistenceError("Raw evidence idempotency check failed")
        finalized_source = repository.finalize_source_run(
            source_run_id=source_run.source_run_id,
            status="success",
            counters=Phase6BSourceCounters(
                discovered_count=1,
                attempted_count=1,
                extracted_count=1,
            ),
            acquisition_strategy="synthetic_validation",
            metadata={"normalized_job_writes": 0},
        )
        finalized_fleet = repository.finalize_fleet_run(
            fleet_run_id=fleet.fleet_run_id,
        )
        result = Phase6BLiveValidationResult(
            fleet_run_id=finalized_fleet.fleet_run_id,
            source_run_id=finalized_source.source_run_id,
            evidence_id=evidence.evidence_id,
            source_id=source.source_id,
            evidence_inserted=inserted,
            duplicate_evidence_inserted=duplicate_inserted,
            fleet_status=finalized_fleet.status,
            source_status=finalized_source.status,
            raw_evidence_count=finalized_fleet.raw_evidence_count,
        )
        payload = result.model_dump(mode="json")
        payload["created_indexes"] = created_indexes
        payload["records_kept"] = bool(args.keep_records)
        if not args.keep_records:
            cleanup = repository.delete_validation_run(fleet.fleet_run_id)
            payload["cleanup"] = cleanup
        output = _write_result(Path(args.output), payload)
    except Exception as exc:
        if repository is not None and fleet_run_id and not args.keep_records:
            try:
                cleanup = repository.delete_validation_run(fleet_run_id)
            except Exception:
                cleanup = None
        print(
            "PHASE_6B_PERSISTENCE_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={exc}",
            f"cleanup={cleanup}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2) from exc

    print(
        "PHASE_6B_PERSISTENCE_OK",
        f"fleet_status={result.fleet_status}",
        f"source_status={result.source_status}",
        f"raw_evidence={result.raw_evidence_count}",
        f"first_inserted={str(result.evidence_inserted).lower()}",
        f"duplicate_inserted={str(result.duplicate_evidence_inserted).lower()}",
        "normalized_job_writes=false",
        "reconciliation=false",
        "deactivation=false",
        f"records_kept={str(bool(args.keep_records)).lower()}",
        f"output={output}",
        flush=True,
    )


if __name__ == "__main__":
    main()

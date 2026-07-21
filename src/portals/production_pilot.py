from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import Field, model_validator

from .contracts import ContractModel
from .production_ingestion import Phase6AIngestionPlan, Phase6ASource


PHASE_7D2A_CONTRACT_VERSION = "1.0"
PHASE_7D2A = "7D2A"


class ProductionPilotError(ValueError):
    """Raised when a pilot input or output violates rollout safety controls."""


class Phase7D2PilotSelection(ContractModel):
    contract_version: Literal["1.0"] = PHASE_7D2A_CONTRACT_VERSION
    phase: Literal["7D2A"] = PHASE_7D2A
    plan_id: str = Field(min_length=1)
    cohort_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    quality_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    full_cohort_source_count: int = Field(ge=1)
    deferred_source_count: int = Field(ge=0)
    source_ids: list[str] = Field(min_length=1)
    expected_source_count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_selection(self) -> "Phase7D2PilotSelection":
        if len(self.source_ids) != self.expected_source_count:
            raise ValueError("Pilot source count does not match expected_source_count")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("Pilot source ids must be unique")
        return self


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Required {label} does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ProductionPilotError(f"{label} contains invalid JSON: {source}") from exc
    if not isinstance(payload, dict):
        raise ProductionPilotError(f"{label} must contain a JSON object")
    return payload


def _verify_checksum(payload: Mapping[str, Any], *, field: str, label: str) -> str:
    stored = str(payload.get(field) or "").strip().lower()
    unsigned = dict(payload)
    unsigned.pop(field, None)
    if not stored or stored != _canonical_sha256(unsigned):
        raise ProductionPilotError(f"{label} checksum is missing or invalid")
    return stored


def _unique_ids(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ProductionPilotError(f"{field} must be a JSON array")
    ids = [str(item or "").strip() for item in value]
    if any(not source_id for source_id in ids):
        raise ProductionPilotError(f"{field} contains an empty source id")
    if len(ids) != len(set(ids)):
        raise ProductionPilotError(f"{field} contains duplicate source ids")
    return ids


def load_phase7d2a_pilot_inputs(
    *,
    cohort_path: Path,
    quality_report_path: Path,
    plan_path: Path,
    expected_inventory: int = 102,
    expected_cohort_size: int = 26,
    expected_new_sources: int = 2,
) -> tuple[Phase6AIngestionPlan, Phase7D2PilotSelection]:
    """Derive the pilot only from linked, immutable Phase 7D1 evidence."""

    cohort = _load_json(cohort_path, label="Phase 7D1 cohort")
    quality = _load_json(quality_report_path, label="Phase 7D1 quality report")
    plan_payload = _load_json(plan_path, label="Phase 7D1 production plan")
    cohort_sha256 = _verify_checksum(
        cohort,
        field="cohort_sha256",
        label="Phase 7D1 cohort",
    )
    report_sha256 = _verify_checksum(
        quality,
        field="report_sha256",
        label="Phase 7D1 quality report",
    )

    if (
        str(cohort.get("contract_version") or ""),
        str(cohort.get("phase") or ""),
        str(cohort.get("cohort_status") or ""),
    ) != ("1.1", "7D1", "frozen_truthful_production_cohort"):
        raise ProductionPilotError("Pilot requires the Phase 7D1 truthful cohort")
    if (
        str(quality.get("contract_version") or ""),
        str(quality.get("phase") or ""),
        str(quality.get("report_status") or ""),
    ) != ("1.0", "7D1", "truthful_evidence_revalidation_complete"):
        raise ProductionPilotError("Pilot requires the Phase 7D1 quality report")

    evidence = cohort.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("quality_report_sha256") != report_sha256:
        raise ProductionPilotError("Cohort and quality report are not cryptographically linked")
    inventory = cohort.get("inventory")
    if not isinstance(inventory, dict) or int(inventory.get("source_count") or 0) != expected_inventory:
        raise ProductionPilotError("Cohort inventory count is inconsistent")

    cohort_ids = _unique_ids(cohort.get("cohort_source_ids"), field="cohort_source_ids")
    deferred_ids = _unique_ids(cohort.get("deferred_source_ids"), field="deferred_source_ids")
    if len(cohort_ids) != expected_cohort_size:
        raise ProductionPilotError(
            f"Expected {expected_cohort_size} production sources but cohort has {len(cohort_ids)}"
        )
    if len(cohort_ids) + len(deferred_ids) != expected_inventory:
        raise ProductionPilotError("Cohort does not account for the full inventory")
    if set(cohort_ids) & set(deferred_ids):
        raise ProductionPilotError("Cohort and deferred source ids overlap")

    final_ids = _unique_ids(
        quality.get("final_production_ready_source_ids"),
        field="final_production_ready_source_ids",
    )
    report_deferred_ids = _unique_ids(
        quality.get("deferred_source_ids"),
        field="quality_report.deferred_source_ids",
    )
    new_ids = _unique_ids(
        quality.get("newly_production_ready_source_ids"),
        field="newly_production_ready_source_ids",
    )
    if final_ids != cohort_ids or report_deferred_ids != deferred_ids:
        raise ProductionPilotError("Quality report source partitions do not match the cohort")
    if len(new_ids) != expected_new_sources:
        raise ProductionPilotError(
            f"Expected {expected_new_sources} new production sources but report has {len(new_ids)}"
        )
    if not set(new_ids).issubset(set(cohort_ids)):
        raise ProductionPilotError("New production sources are outside the frozen cohort")

    counts = quality.get("counts")
    if not isinstance(counts, dict):
        raise ProductionPilotError("Quality report counts are missing")
    required_counts = {
        "inventory": expected_inventory,
        "newly_production_ready": expected_new_sources,
        "final_production_ready": expected_cohort_size,
        "deferred": expected_inventory - expected_cohort_size,
    }
    for key, expected in required_counts.items():
        if int(counts.get(key) or 0) != expected:
            raise ProductionPilotError(f"Quality report count {key} is inconsistent")

    raw_records = quality.get("records")
    if not isinstance(raw_records, list):
        raise ProductionPilotError("Quality report records are missing")
    records: dict[str, dict[str, Any]] = {}
    for value in raw_records:
        if not isinstance(value, dict):
            raise ProductionPilotError("Quality report contains a non-object record")
        source_id = str(value.get("source_id") or "").strip()
        if not source_id or source_id in records:
            raise ProductionPilotError("Quality report contains an invalid source identity")
        records[source_id] = value
    if set(records) != set(cohort_ids) | set(deferred_ids):
        raise ProductionPilotError("Quality report records do not cover the full inventory")
    for source_id in new_ids:
        record = records[source_id]
        if record.get("production_ready") is not True:
            raise ProductionPilotError(f"Pilot source {source_id} is not production_ready")
        if str(record.get("classification") or "") != "production_ready":
            raise ProductionPilotError(f"Pilot source {source_id} has an unsafe classification")
        if str(record.get("evidence_origin") or "") != "repair":
            raise ProductionPilotError(f"Pilot source {source_id} is not a repair addition")

    try:
        plan = Phase6AIngestionPlan.model_validate(plan_payload)
    except (TypeError, ValueError) as exc:
        raise ProductionPilotError(f"Invalid Phase 7D1 production plan: {exc}") from exc
    if plan.cohort_sha256 != cohort_sha256:
        raise ProductionPilotError("Production plan references a different frozen cohort")
    if plan.cohort_source_count != expected_cohort_size:
        raise ProductionPilotError("Production plan cohort_source_count is inconsistent")
    if plan.selected_source_ids != cohort_ids or plan.selected_source_count != len(cohort_ids):
        raise ProductionPilotError("Production plan must contain the complete frozen cohort")
    if plan.deferred_source_count != len(deferred_ids):
        raise ProductionPilotError("Production plan deferred_source_count is inconsistent")
    required_plan_controls = {
        "execution_mode": "plan_only",
        "max_source_concurrency": 1,
        "production_writes_enabled": False,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
    }
    for key, expected in required_plan_controls.items():
        if plan.controls.get(key) != expected:
            raise ProductionPilotError(f"Unsafe or inconsistent plan control: {key}")

    raw_sources = cohort.get("sources")
    if not isinstance(raw_sources, list):
        raise ProductionPilotError("Cohort sources are missing")
    try:
        cohort_sources = [Phase6ASource.model_validate(value) for value in raw_sources]
    except (TypeError, ValueError) as exc:
        raise ProductionPilotError(f"Cohort contains an invalid source: {exc}") from exc
    if [source.source_id for source in cohort_sources] != cohort_ids:
        raise ProductionPilotError("Cohort sources are not aligned with source ids")
    if [source.model_dump(mode="json") for source in plan.sources] != [
        source.model_dump(mode="json") for source in cohort_sources
    ]:
        raise ProductionPilotError("Production plan source definitions differ from the signed cohort")

    return plan, Phase7D2PilotSelection(
        plan_id=plan.plan_id,
        cohort_sha256=cohort_sha256,
        quality_report_sha256=report_sha256,
        full_cohort_source_count=len(cohort_ids),
        deferred_source_count=len(deferred_ids),
        source_ids=new_ids,
        expected_source_count=expected_new_sources,
    )


_READ_ONLY_COLLECTION_METHODS = {
    "count_documents",
    "distinct",
    "estimated_document_count",
    "find",
    "find_one",
    "index_information",
    "list_indexes",
}


class _ReadOnlyCollectionProxy:
    def __init__(self, collection: Any, name: str) -> None:
        self.__collection = collection
        self.__name = name

    def __getattr__(self, name: str) -> Any:
        if name in {"database", "client"}:
            raise ProductionPilotError("Direct database access is blocked during Phase 7D2A")
        attribute = getattr(self.__collection, name)
        if callable(attribute) and name not in _READ_ONLY_COLLECTION_METHODS:
            def blocked(*args: Any, **kwargs: Any) -> Any:
                del args, kwargs
                raise ProductionPilotError(
                    f"DATABASE_WRITE_BLOCKED_DURING_PHASE_7D2A: {self.__name}.{name}"
                )

            return blocked
        return attribute


class ReadOnlyDatabaseProxy:
    """Permit production-path reads while failing closed on all collection writes."""

    def __init__(self, database: Any) -> None:
        self.__database = database

    def __getitem__(self, name: str) -> _ReadOnlyCollectionProxy:
        return _ReadOnlyCollectionProxy(self.__database[name], str(name))

    def get_collection(self, name: str, *args: Any, **kwargs: Any) -> _ReadOnlyCollectionProxy:
        collection = self.__database.get_collection(name, *args, **kwargs)
        return _ReadOnlyCollectionProxy(collection, str(name))

    def __getattr__(self, name: str) -> Any:
        if name == "client":
            raise ProductionPilotError(
                f"DATABASE_OPERATION_BLOCKED_DURING_PHASE_7D2A: {name}"
            )
        attribute = getattr(self.__database, name)
        if callable(attribute):
            raise ProductionPilotError(
                f"DATABASE_OPERATION_BLOCKED_DURING_PHASE_7D2A: {name}"
            )
        return attribute


def build_phase7d2a_pilot_report(
    *,
    selection: Phase7D2PilotSelection,
    manifest: Mapping[str, Any],
    database_write_guard_enabled: bool,
) -> dict[str, Any]:
    blockers: list[str] = []

    def block(reason: str) -> None:
        if reason not in blockers:
            blockers.append(reason)

    selected_ids = [str(value or "") for value in manifest.get("selected_source_ids") or []]
    source_results = manifest.get("source_results")
    if not isinstance(source_results, list):
        source_results = []
        block("source_results_missing")
    if str(manifest.get("execution_mode") or "") != "dry_run":
        block("execution_mode_not_dry_run")
    if str(manifest.get("plan_id") or "") != selection.plan_id:
        block("plan_id_mismatch")
    if str(manifest.get("cohort_sha256") or "") != selection.cohort_sha256:
        block("cohort_sha256_mismatch")
    if selected_ids != selection.source_ids:
        block("pilot_source_selection_mismatch")
    if int(manifest.get("requested_source_count") or 0) != selection.expected_source_count:
        block("requested_source_count_mismatch")
    if int(manifest.get("completed_source_count") or 0) != selection.expected_source_count:
        block("pilot_did_not_complete_all_sources")
    if int(manifest.get("successful_source_count") or 0) != selection.expected_source_count:
        block("pilot_did_not_succeed_for_all_sources")
    for field in ("failed_source_count", "blocked_source_count", "cancelled_source_count"):
        if int(manifest.get(field) or 0) != 0:
            block(f"nonzero_{field}")
    if int(manifest.get("quarantined_job_count") or 0) != 0:
        block("pilot_quarantined_jobs")
    if int(manifest.get("accepted_job_count") or 0) < selection.expected_source_count:
        block("pilot_accepted_too_few_jobs")

    controls = manifest.get("controls")
    if not isinstance(controls, dict):
        controls = {}
        block("manifest_controls_missing")
    required_controls = {
        "normalized_job_writes_enabled": False,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
        "max_source_concurrency": 1,
        "max_attempts": 1,
        "max_jobs_per_source": 10,
        "bounded_pilot_execution": True,
    }
    for key, expected in required_controls.items():
        if controls.get(key) != expected:
            block(f"unsafe_or_inconsistent_manifest_control:{key}")
    if not database_write_guard_enabled:
        block("database_write_guard_not_enabled")

    by_id: dict[str, Mapping[str, Any]] = {}
    for value in source_results:
        if not isinstance(value, Mapping):
            block("invalid_source_result")
            continue
        source_id = str(value.get("source_id") or "")
        if not source_id or source_id in by_id:
            block("duplicate_or_missing_source_result_id")
            continue
        by_id[source_id] = value
    if set(by_id) != set(selection.source_ids):
        block("source_result_partition_mismatch")

    summarized_results: list[dict[str, Any]] = []
    for source_id in selection.source_ids:
        result = by_id.get(source_id, {})
        status = str(result.get("status") or "missing")
        extracted = int(result.get("extracted_count") or 0)
        accepted = int(result.get("accepted_count") or 0)
        quarantined = int(result.get("quarantined_count") or 0)
        rejected = int(result.get("rejected_count") or 0)
        attempts = result.get("attempts") if isinstance(result.get("attempts"), list) else []
        if status != "success":
            block(f"source_not_successful:{source_id}")
        if extracted < 1 or accepted < 1:
            block(f"source_has_no_accepted_jobs:{source_id}")
        if quarantined or rejected:
            block(f"source_has_quality_rejections:{source_id}")
        if len(attempts) != 1:
            block(f"source_attempt_count_not_one:{source_id}")
        summarized_results.append(
            {
                "source_id": source_id,
                "status": status,
                "attempts": len(attempts),
                "discovered_count": int(result.get("discovered_count") or 0),
                "extracted_count": extracted,
                "accepted_count": accepted,
                "quarantined_count": quarantined,
                "rejected_count": rejected,
                "error_type": result.get("error_type"),
            }
        )

    report: dict[str, Any] = {
        "contract_version": PHASE_7D2A_CONTRACT_VERSION,
        "phase": PHASE_7D2A,
        "status": "passed" if not blockers else "failed",
        "ready_for_phase7d2b": not blockers,
        "run_id": manifest.get("run_id"),
        "generated_at": manifest.get("generated_at"),
        "plan_id": selection.plan_id,
        "cohort_sha256": selection.cohort_sha256,
        "quality_report_sha256": selection.quality_report_sha256,
        "pilot_source_ids": selection.source_ids,
        "source_results": summarized_results,
        "blockers": blockers,
        "preview_counts": {
            "extracted": int(manifest.get("extracted_job_count") or 0),
            "accepted": int(manifest.get("accepted_job_count") or 0),
            "quarantined": int(manifest.get("quarantined_job_count") or 0),
            "would_insert": int(manifest.get("inserted_job_count") or 0),
            "would_update": int(manifest.get("updated_job_count") or 0),
            "would_reactivate": int(manifest.get("reactivated_job_count") or 0),
            "unchanged": int(manifest.get("unchanged_job_count") or 0),
        },
        "safety": {
            "database_write_guard_enabled": database_write_guard_enabled,
            "index_creation_performed": False,
            "normalized_job_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "full_cohort_execution_performed": False,
            "anti_bot_bypass_enabled": False,
        },
    }
    report["report_sha256"] = _canonical_sha256(report)
    return report


def write_phase7d2a_pilot_report(path: Path, report: Mapping[str, Any]) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(report), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, target)
    return target

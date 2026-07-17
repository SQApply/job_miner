from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel
from .production_cohort import PRODUCTION_COHORT_CONTRACT_VERSION, read_source_id_file


PHASE_6A_CONTRACT_VERSION = "1.0"
PHASE_6A = "6A"
EXPECTED_COHORT_PHASE = "5.5C"
EXPECTED_COHORT_STATUS = "frozen_certified_cohort"


class ProductionIngestionError(ValueError):
    """Raised when a Phase 6 ingestion request violates cohort safety controls."""


class Phase6AIngestionConfig(ContractModel):
    """Safe controls for Phase 6A plan generation.

    Phase 6A performs cohort enforcement only. Network execution, database writes,
    reconciliation, and deactivation are deliberately forbidden until later steps.
    """

    contract_version: Literal["1.0"] = PHASE_6A_CONTRACT_VERSION
    phase: Literal["6A"] = PHASE_6A
    execution_mode: Literal["plan_only"] = "plan_only"
    expected_cohort_size: int = Field(default=19, ge=1, le=10000)
    max_source_concurrency: int = Field(default=2, ge=1, le=4)
    source_timeout_seconds: int = Field(default=600, ge=30, le=86400)
    requested_source_ids: list[str] = Field(default_factory=list)
    production_writes_enabled: bool = False
    lifecycle_reconciliation_enabled: bool = False
    deactivation_enabled: bool = False

    @field_validator("requested_source_ids")
    @classmethod
    def validate_requested_source_ids(cls, values: list[str]) -> list[str]:
        normalized = [str(value or "").strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("requested_source_ids cannot contain empty values")
        if len(normalized) != len(set(normalized)):
            raise ValueError("requested_source_ids cannot contain duplicates")
        return normalized

    @model_validator(mode="after")
    def forbid_dangerous_phase_6a_controls(self) -> "Phase6AIngestionConfig":
        if self.production_writes_enabled:
            raise ValueError("Phase 6A is plan-only; production writes are not permitted")
        if self.lifecycle_reconciliation_enabled:
            raise ValueError("Phase 6A cannot enable lifecycle reconciliation")
        if self.deactivation_enabled:
            raise ValueError("Phase 6A cannot enable job deactivation")
        return self


class Phase6ASource(ContractModel):
    source_id: str = Field(min_length=2, max_length=200)
    source_row: int = Field(ge=1)
    display_name: str = Field(min_length=1, max_length=300)
    listing_url: str
    detected_platform: str = Field(default="unknown", min_length=1, max_length=100)
    resolved_route_url: str | None = None
    bounded_extracted_jobs: int = Field(default=0, ge=0)
    evidence_run_id: str | None = None

    @field_validator("listing_url")
    @classmethod
    def validate_listing_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("listing_url must be an absolute HTTP(S) URL")
        return value

    @field_validator("resolved_route_url")
    @classmethod
    def validate_resolved_route_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("resolved_route_url must be an absolute HTTP(S) URL")
        return value


class LoadedProductionCohort(ContractModel):
    cohort_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    cohort_source_count: int = Field(ge=1)
    deferred_source_count: int = Field(ge=0)
    inventory_source_count: int = Field(ge=1)
    source_ids: list[str] = Field(min_length=1)
    deferred_source_ids: list[str] = Field(default_factory=list)
    sources: list[Phase6ASource] = Field(min_length=1)


class Phase6AIngestionPlan(ContractModel):
    contract_version: Literal["1.0"] = PHASE_6A_CONTRACT_VERSION
    phase: Literal["6A"] = PHASE_6A
    plan_id: str = Field(min_length=1, max_length=200)
    generated_at: datetime
    cohort_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    cohort_source_count: int = Field(ge=1)
    selected_source_count: int = Field(ge=1)
    deferred_source_count: int = Field(ge=0)
    selected_source_ids: list[str] = Field(min_length=1)
    sources: list[Phase6ASource] = Field(min_length=1)
    controls: dict[str, Any]

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_plan_counts_and_controls(self) -> "Phase6AIngestionPlan":
        if self.selected_source_count != len(self.selected_source_ids):
            raise ValueError("selected_source_count does not match selected_source_ids")
        if self.selected_source_count != len(self.sources):
            raise ValueError("selected_source_count does not match sources")
        if [source.source_id for source in self.sources] != self.selected_source_ids:
            raise ValueError("sources are not aligned with selected_source_ids")
        if self.controls.get("production_writes_enabled") is not False:
            raise ValueError("Phase 6A plans must keep production writes disabled")
        if self.controls.get("lifecycle_reconciliation_enabled") is not False:
            raise ValueError("Phase 6A plans must keep reconciliation disabled")
        if self.controls.get("deactivation_enabled") is not False:
            raise ValueError("Phase 6A plans must keep deactivation disabled")
        return self


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json_object(path: Path) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Frozen production cohort does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ProductionIngestionError(f"Frozen cohort contains invalid JSON: {source}") from exc
    if not isinstance(payload, dict):
        raise ProductionIngestionError("Frozen cohort must contain a JSON object")
    return payload


def _unique_ids(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ProductionIngestionError(f"{field} must be a JSON array")
    ids = [str(item or "").strip() for item in value]
    if any(not source_id for source_id in ids):
        raise ProductionIngestionError(f"{field} contains an empty source id")
    if len(ids) != len(set(ids)):
        raise ProductionIngestionError(f"{field} contains duplicate source ids")
    return ids


def load_phase6a_production_cohort(
    cohort_path: Path,
    *,
    expected_cohort_size: int = 19,
) -> LoadedProductionCohort:
    """Load and independently verify the immutable Phase 5.5C cohort manifest."""

    payload = _load_json_object(Path(cohort_path))
    stored_hash = str(payload.get("cohort_sha256") or "").strip().lower()
    unhashed = dict(payload)
    unhashed.pop("cohort_sha256", None)
    if not stored_hash or stored_hash != _canonical_sha256(unhashed):
        raise ProductionIngestionError("Frozen cohort checksum is missing or invalid")

    if str(payload.get("contract_version") or "") != PRODUCTION_COHORT_CONTRACT_VERSION:
        raise ProductionIngestionError("Unsupported frozen cohort contract version")
    if str(payload.get("phase") or "") != EXPECTED_COHORT_PHASE:
        raise ProductionIngestionError("Phase 6A requires a Phase 5.5C frozen cohort")
    if str(payload.get("cohort_status") or "") != EXPECTED_COHORT_STATUS:
        raise ProductionIngestionError("Cohort is not in frozen_certified_cohort status")

    cohort_ids = _unique_ids(payload.get("cohort_source_ids"), field="cohort_source_ids")
    deferred_ids = _unique_ids(payload.get("deferred_source_ids"), field="deferred_source_ids")
    if len(cohort_ids) != expected_cohort_size:
        raise ProductionIngestionError(
            f"Expected frozen cohort size {expected_cohort_size}, found {len(cohort_ids)}"
        )
    if set(cohort_ids) & set(deferred_ids):
        raise ProductionIngestionError("Cohort and deferred source ids overlap")
    if int(payload.get("cohort_source_count") or -1) != len(cohort_ids):
        raise ProductionIngestionError("cohort_source_count is inconsistent")
    if int(payload.get("deferred_source_count") or -1) != len(deferred_ids):
        raise ProductionIngestionError("deferred_source_count is inconsistent")

    inventory = payload.get("inventory")
    if not isinstance(inventory, dict):
        raise ProductionIngestionError("Frozen cohort inventory metadata is missing")
    inventory_count = int(inventory.get("source_count") or 0)
    if inventory_count != len(cohort_ids) + len(deferred_ids):
        raise ProductionIngestionError("Frozen cohort does not account for the full inventory")

    safety = payload.get("safety")
    if not isinstance(safety, dict):
        raise ProductionIngestionError("Frozen cohort safety metadata is missing")
    required_safety = {
        "bounded_certification_only": True,
        "full_inventory_extraction_validated": False,
        "production_ingestion_enabled": False,
        "lifecycle_reconciliation_enabled": False,
        "failed_or_partial_runs_may_deactivate_jobs": False,
        "phase_6_validation_required": True,
    }
    for key, expected in required_safety.items():
        if safety.get(key) is not expected:
            raise ProductionIngestionError(
                f"Frozen cohort safety control {key} must be {expected!r}"
            )

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise ProductionIngestionError("Frozen cohort sources must be a JSON array")
    try:
        sources = [Phase6ASource.model_validate(source) for source in raw_sources]
    except (TypeError, ValueError) as exc:
        raise ProductionIngestionError(f"Frozen cohort contains an invalid source: {exc}") from exc
    source_ids = [source.source_id for source in sources]
    if source_ids != cohort_ids:
        raise ProductionIngestionError(
            "Frozen cohort sources must exactly match cohort_source_ids in order"
        )

    return LoadedProductionCohort(
        cohort_sha256=stored_hash,
        cohort_source_count=len(cohort_ids),
        deferred_source_count=len(deferred_ids),
        inventory_source_count=inventory_count,
        source_ids=cohort_ids,
        deferred_source_ids=deferred_ids,
        sources=sources,
    )


def select_phase6a_sources(
    cohort: LoadedProductionCohort,
    requested_source_ids: Sequence[str] | None = None,
) -> list[Phase6ASource]:
    """Select only frozen-cohort sources and retain deterministic cohort ordering."""

    requested = [str(value or "").strip() for value in requested_source_ids or []]
    if any(not source_id for source_id in requested):
        raise ProductionIngestionError("Requested source ids cannot contain empty values")
    if len(requested) != len(set(requested)):
        raise ProductionIngestionError("Requested source ids cannot contain duplicates")
    if not requested:
        return list(cohort.sources)

    cohort_set = set(cohort.source_ids)
    rejected = [source_id for source_id in requested if source_id not in cohort_set]
    if rejected:
        raise ProductionIngestionError(
            "SOURCE_NOT_IN_PRODUCTION_COHORT: " + ", ".join(rejected)
        )
    requested_set = set(requested)
    return [source for source in cohort.sources if source.source_id in requested_set]


def build_phase6a_ingestion_plan(
    *,
    cohort_path: Path,
    config: Phase6AIngestionConfig,
    generated_at: datetime | None = None,
) -> Phase6AIngestionPlan:
    cohort = load_phase6a_production_cohort(
        cohort_path,
        expected_cohort_size=config.expected_cohort_size,
    )
    selected = select_phase6a_sources(cohort, config.requested_source_ids)
    generated = generated_at or datetime.now(timezone.utc)
    seed = {
        "cohort_sha256": cohort.cohort_sha256,
        "selected_source_ids": [source.source_id for source in selected],
        "generated_at": generated.isoformat(),
    }
    plan_id = f"phase6a_{generated.strftime('%Y%m%dT%H%M%SZ')}_{_canonical_sha256(seed)[:12]}"
    return Phase6AIngestionPlan(
        plan_id=plan_id,
        generated_at=generated,
        cohort_sha256=cohort.cohort_sha256,
        cohort_source_count=cohort.cohort_source_count,
        selected_source_count=len(selected),
        deferred_source_count=cohort.deferred_source_count,
        selected_source_ids=[source.source_id for source in selected],
        sources=selected,
        controls={
            "execution_mode": config.execution_mode,
            "max_source_concurrency": config.max_source_concurrency,
            "source_timeout_seconds": config.source_timeout_seconds,
            "production_writes_enabled": config.production_writes_enabled,
            "lifecycle_reconciliation_enabled": config.lifecycle_reconciliation_enabled,
            "deactivation_enabled": config.deactivation_enabled,
        },
    )


def write_phase6a_ingestion_plan(path: Path, plan: Phase6AIngestionPlan) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(plan.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, target)
    return target


def requested_source_ids_from_inputs(
    *,
    source_ids: Sequence[str] | None = None,
    source_id_file: Path | None = None,
) -> list[str]:
    combined = [str(value or "").strip() for value in source_ids or []]
    if source_id_file is not None:
        combined.extend(read_source_id_file(Path(source_id_file)))
    if any(not value for value in combined):
        raise ProductionIngestionError("Requested source ids cannot contain empty values")
    if len(combined) != len(set(combined)):
        raise ProductionIngestionError("Requested source ids cannot contain duplicates")
    return combined

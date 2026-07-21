from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..schemas import JobPosting
from .url_intelligence import assess_certification_job


REPAIR_CAMPAIGN_CONTRACT_VERSION = "1.0"


@dataclass(frozen=True)
class RepairCohort:
    cohort_id: str
    source_ids: tuple[str, ...]
    groups: dict[str, tuple[str, ...]]
    baseline_production_ready: int
    maximum_attempts: int

    def __post_init__(self) -> None:
        if not self.cohort_id.strip():
            raise ValueError("Repair cohort id is required")
        if not self.source_ids:
            raise ValueError("Repair cohort must contain at least one source")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Repair cohort source ids must be unique")
        grouped = [source_id for values in self.groups.values() for source_id in values]
        if set(grouped) != set(self.source_ids) or len(grouped) != len(self.source_ids):
            raise ValueError("Repair cohort groups must partition source_ids exactly")
        if self.baseline_production_ready < 0:
            raise ValueError("baseline_production_ready cannot be negative")
        if self.maximum_attempts != 1:
            raise ValueError("The final repair cohort must be a single bounded attempt")


def load_repair_cohort(path: Path) -> RepairCohort:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    groups = {
        str(name): tuple(str(source_id) for source_id in values)
        for name, values in dict(payload.get("groups") or {}).items()
    }
    source_ids = tuple(
        source_id
        for values in groups.values()
        for source_id in values
    )
    return RepairCohort(
        cohort_id=str(payload.get("cohort_id") or ""),
        source_ids=source_ids,
        groups=groups,
        baseline_production_ready=int(payload.get("baseline_production_ready") or 0),
        maximum_attempts=int(payload.get("maximum_attempts") or 0),
    )


def _sample_quality(record: dict[str, Any]) -> tuple[bool, list[str]]:
    samples = list(record.get("sample_jobs") or [])
    if not samples:
        return False, ["missing_sample_jobs"]
    reasons: list[str] = []
    urls: list[str] = []
    source_url = str(record.get("effective_listing_url") or record.get("provided_url") or "")
    for index, payload in enumerate(samples):
        try:
            job = JobPosting.model_validate(payload)
        except Exception as exc:
            reasons.append(f"sample_{index}:invalid_schema:{type(exc).__name__}")
            continue
        valid, reason = assess_certification_job(job, source_url)
        if not valid:
            reasons.append(f"sample_{index}:{reason}")
        if job.job_url:
            urls.append(str(job.job_url))
    if len(urls) != len(set(urls)):
        reasons.append("duplicate_sample_job_urls")
    return not reasons, reasons


def audit_repair_records(
    records: Iterable[dict[str, Any]],
    cohort: RepairCohort,
) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        source_id = str(record.get("source_id") or "")
        if source_id in cohort.source_ids:
            latest[source_id] = record

    clean_successes: list[str] = []
    usable_partials: list[str] = []
    false_successes: list[dict[str, Any]] = []
    deferred: list[str] = []
    for source_id in cohort.source_ids:
        record = latest.get(source_id)
        if record is None:
            continue
        quality_ok, quality_reasons = _sample_quality(record)
        status = str(record.get("status") or "failed")
        extracted = int(record.get("extracted_jobs") or 0)
        if status == "success" and extracted > 0 and quality_ok:
            clean_successes.append(source_id)
        elif status == "partial" and extracted > 0 and quality_ok:
            usable_partials.append(source_id)
        else:
            deferred.append(source_id)
            if status == "success":
                false_successes.append(
                    {"source_id": source_id, "reasons": quality_reasons or ["empty_success"]}
                )

    missing = [source_id for source_id in cohort.source_ids if source_id not in latest]
    return {
        "contract_version": REPAIR_CAMPAIGN_CONTRACT_VERSION,
        "cohort_id": cohort.cohort_id,
        "cohort_size": len(cohort.source_ids),
        "evaluated": len(latest),
        "missing_source_ids": missing,
        "clean_success_source_ids": clean_successes,
        "usable_partial_source_ids": usable_partials,
        "deferred_source_ids": deferred,
        "false_successes": false_successes,
        "production_ready_before": cohort.baseline_production_ready,
        "production_ready_after": cohort.baseline_production_ready + len(clean_successes),
        "campaign_complete": not missing,
        "stop_rule": (
            "complete_move_to_next_phase"
            if not missing
            else "incomplete_run_only_missing_sources"
        ),
    }

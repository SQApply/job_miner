from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PHASE_8_1_HEALTH_CONTRACT_VERSION = "1.0"

_SUCCESSFUL_CHECKPOINT_STATUSES = {"complete", "complete_guarded", "partial"}
_FAILURE_CHECKPOINT_STATUSES = {
    "failed",
    "failed_downstream",
    "blocked",
    "cancelled",
}
_RUNNING_CHECKPOINT_STATUSES = {"running", "downstream_pending"}


class ProductionHealthError(RuntimeError):
    """Raised when a Phase 8.1 health report cannot be built safely."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _event_time(document: Mapping[str, Any]) -> datetime | None:
    for key in ("completed_at", "updated_at", "started_at", "created_at"):
        parsed = _as_utc_datetime(document.get(key))
        if parsed is not None:
            return parsed
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_successful_checkpoint(document: Mapping[str, Any]) -> bool:
    status = str(document.get("status") or "")
    result = _as_dict(document.get("result"))
    return (
        status in _SUCCESSFUL_CHECKPOINT_STATUSES
        and str(result.get("status") or "") == "success"
    )


def _is_complete_checkpoint(document: Mapping[str, Any]) -> bool:
    if str(document.get("status") or "") != "complete":
        return False
    result = _as_dict(document.get("result"))
    snapshot = _as_dict(document.get("snapshot"))
    return (
        result.get("catalog_complete") is not False
        and result.get("discovery_complete") is not False
        and result.get("reconciliation_safe") is not False
        and snapshot.get("reconciliation_safe") is not False
    )


def _latest_document(
    documents: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    if not documents:
        return None
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return max(documents, key=lambda row: _event_time(row) or floor)


def _consecutive_failures(
    documents: Sequence[Mapping[str, Any]],
) -> int:
    floor = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(
        documents,
        key=lambda row: _event_time(row) or floor,
        reverse=True,
    )
    count = 0
    for row in ordered:
        if str(row.get("status") or "") in _FAILURE_CHECKPOINT_STATUSES:
            count += 1
            continue
        break
    return count


def _current_job_counts(
    source_id: str,
    job_documents: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    rows = [
        row
        for row in job_documents
        if str(row.get("target_id") or row.get("source_id") or "") == source_id
    ]
    active = sum(1 for row in rows if row.get("is_active") is not False)
    inactive = len(rows) - active
    missing_candidates = sum(
        1
        for row in rows
        if row.get("is_active") is not False
        and _as_int(
            row.get("missing_complete_run_count")
            or row.get("missing_count")
        )
        > 0
    )
    return {
        "total": len(rows),
        "active": active,
        "inactive": inactive,
        "missing_candidates": missing_candidates,
    }


def build_phase8_1_source_health_report(
    *,
    source_ids: Sequence[str],
    checkpoint_documents: Iterable[Mapping[str, Any]],
    cycle_documents: Iterable[Mapping[str, Any]],
    job_documents: Iterable[Mapping[str, Any]],
    cadence_hours: int = 72,
    failure_retry_hours: int = 6,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a truthful, read-only health view for the recurring cohort."""
    normalized_source_ids = [str(value or "").strip() for value in source_ids]
    if not normalized_source_ids or any(not value for value in normalized_source_ids):
        raise ProductionHealthError("source_ids must contain non-empty values")
    if len(normalized_source_ids) != len(set(normalized_source_ids)):
        raise ProductionHealthError("source_ids contains duplicates")
    if cadence_hours < 1 or failure_retry_hours < 1:
        raise ProductionHealthError("cadence and retry hours must be positive")

    report_now = _as_utc_datetime(now) or _utc_now()
    checkpoints_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in checkpoint_documents:
        source_id = str(row.get("source_id") or "").strip()
        if source_id in normalized_source_ids:
            checkpoints_by_source[source_id].append(row)

    cycles_by_id: dict[str, Mapping[str, Any]] = {}
    for row in cycle_documents:
        cycle_id = str(row.get("cycle_id") or "").strip()
        if not cycle_id:
            continue
        current = cycles_by_id.get(cycle_id)
        if current is None or (_event_time(row) or datetime.min.replace(tzinfo=timezone.utc)) > (
            _event_time(current) or datetime.min.replace(tzinfo=timezone.utc)
        ):
            cycles_by_id[cycle_id] = row

    jobs = list(job_documents)
    source_rows: list[dict[str, Any]] = []
    for source_id in normalized_source_ids:
        history = checkpoints_by_source.get(source_id, [])
        latest = _latest_document(history)
        successful = [row for row in history if _is_successful_checkpoint(row)]
        complete = [row for row in history if _is_complete_checkpoint(row)]
        last_success = _latest_document(successful)
        last_complete = _latest_document(complete)
        job_counts = _current_job_counts(source_id, jobs)

        latest_result = _as_dict((latest or {}).get("result"))
        latest_lifecycle = _as_dict((latest or {}).get("lifecycle"))
        latest_snapshot = _as_dict((latest or {}).get("snapshot"))
        latest_status = str((latest or {}).get("status") or "never_run")
        latest_cycle_id = str((latest or {}).get("cycle_id") or "")
        latest_cycle = cycles_by_id.get(latest_cycle_id, {})
        last_run_at = _event_time(latest or {})
        next_due_at = _as_utc_datetime(latest_cycle.get("next_due_at"))
        if next_due_at is None and last_run_at is not None:
            delay = (
                failure_retry_hours
                if latest_status in _FAILURE_CHECKPOINT_STATUSES
                else cadence_hours
            )
            next_due_at = last_run_at + timedelta(hours=delay)
        is_due = latest is None or (
            next_due_at is not None and next_due_at <= report_now
        )

        discovered = _as_int(latest_result.get("discovered_count"))
        catalog_complete = latest_result.get("catalog_complete") is not False
        reconciliation_safe = (
            latest_result.get("reconciliation_safe") is not False
            and latest_snapshot.get("reconciliation_safe") is not False
        )
        reasons: list[str] = []
        if latest is None:
            health_status = "never_run"
            reasons.append("no_recurring_checkpoint")
        elif latest_status in _RUNNING_CHECKPOINT_STATUSES:
            health_status = "running"
            reasons.append(f"checkpoint_status:{latest_status}")
        elif latest_status in _FAILURE_CHECKPOINT_STATUSES:
            health_status = "failed"
            reasons.append(f"checkpoint_status:{latest_status}")
        elif latest_status in {"partial", "complete_guarded"}:
            health_status = "degraded"
            reasons.append(f"checkpoint_status:{latest_status}")
        elif latest_status == "complete":
            if discovered <= 0:
                health_status = "degraded"
                reasons.append("complete_run_zero_discovery")
            elif not catalog_complete or not reconciliation_safe:
                health_status = "degraded"
                reasons.append("complete_snapshot_contract_not_satisfied")
            elif job_counts["active"] <= 0:
                health_status = "degraded"
                reasons.append("no_active_jobs")
            elif is_due:
                health_status = "overdue"
                reasons.append("next_due_at_elapsed")
            else:
                health_status = "healthy"
        else:
            health_status = "unknown"
            reasons.append(f"unrecognized_checkpoint_status:{latest_status}")

        failures = _consecutive_failures(history)
        if failures:
            reasons.append(f"consecutive_failures:{failures}")

        source_rows.append(
            {
                "source_id": source_id,
                "display_name": str(latest_result.get("display_name") or ""),
                "health_status": health_status,
                "attention_required": health_status != "healthy",
                "attention_reasons": reasons,
                "latest_checkpoint_status": latest_status,
                "latest_cycle_id": latest_cycle_id or None,
                "latest_source_run_id": str(
                    (latest or {}).get("source_run_id") or ""
                )
                or None,
                "last_run_at": _iso(last_run_at),
                "last_success_at": _iso(_event_time(last_success or {})),
                "last_complete_at": _iso(_event_time(last_complete or {})),
                "next_due_at": _iso(next_due_at),
                "is_due": is_due,
                "consecutive_failures": failures,
                "runtime_seconds": float(latest_result.get("elapsed_seconds") or 0.0),
                "latest_error_type": str(
                    (latest or {}).get("error_type")
                    or latest_result.get("error_type")
                    or ""
                )
                or None,
                "latest_error_message": str(
                    (latest or {}).get("error_message")
                    or latest_result.get("error_message")
                    or ""
                )[:1000]
                or None,
                "latest_run_counts": {
                    "discovered": discovered,
                    "accepted": _as_int(latest_result.get("accepted_count")),
                    "quarantined": _as_int(
                        latest_result.get("quarantined_count")
                    ),
                    "inserted": _as_int(latest_result.get("inserted_count")),
                    "updated": _as_int(latest_result.get("updated_count")),
                    "unchanged": _as_int(latest_result.get("unchanged_count")),
                    "missing": _as_int(latest_lifecycle.get("missing_marked")),
                    "deactivated": _as_int(latest_lifecycle.get("deactivated")),
                },
                "current_jobs": job_counts,
                "complete_snapshot": bool(
                    latest is not None and _is_complete_checkpoint(latest)
                ),
            }
        )

    status_counts = Counter(row["health_status"] for row in source_rows)
    attention_ids = [
        row["source_id"] for row in source_rows if row["attention_required"]
    ]
    due_ids = [row["source_id"] for row in source_rows if row["is_due"]]
    return {
        "contract_version": PHASE_8_1_HEALTH_CONTRACT_VERSION,
        "phase": "8.1",
        "mode": "source_health_monitoring",
        "generated_at": report_now.isoformat(),
        "cohort_source_count": len(normalized_source_ids),
        "sources_accounted": len(source_rows),
        "status_counts": dict(sorted(status_counts.items())),
        "healthy_source_count": status_counts.get("healthy", 0),
        "attention_source_count": len(attention_ids),
        "complete_snapshot_source_count": sum(
            1 for row in source_rows if row["last_complete_at"] is not None
        ),
        "due_source_count": len(due_ids),
        "attention_source_ids": attention_ids,
        "due_source_ids": due_ids,
        "sources": source_rows,
        "controls": {
            "read_only": True,
            "automatic_scheduler_enabled": False,
            "network_calls": False,
            "mongodb_reads": True,
            "mongodb_writes": False,
            "job_writes": False,
            "reconciliation": False,
            "deactivation": False,
            "qdrant_writes": False,
            "cadence_hours": int(cadence_hours),
            "failure_retry_hours": int(failure_retry_hours),
        },
    }


def collect_phase8_1_source_health(
    database: Any,
    *,
    source_ids: Sequence[str],
    cadence_hours: int = 72,
    failure_retry_hours: int = 6,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read only the collections required for the Phase 8.1 health report."""
    normalized = [str(value) for value in source_ids]
    checkpoints = list(
        database["production_recurring_source_checkpoints"].find(
            {"source_id": {"$in": normalized}},
            {"_id": 0},
        )
    )
    cycle_ids = sorted(
        {
            str(row.get("cycle_id") or "")
            for row in checkpoints
            if str(row.get("cycle_id") or "")
        }
    )
    cycles = (
        list(
            database["production_recurring_cycles"].find(
                {"cycle_id": {"$in": cycle_ids}},
                {"_id": 0},
            )
        )
        if cycle_ids
        else []
    )
    jobs = list(
        database["jobs_current"].find(
            {"target_id": {"$in": normalized}},
            {
                "_id": 0,
                "job_id": 1,
                "target_id": 1,
                "source_id": 1,
                "is_active": 1,
                "missing_count": 1,
                "missing_complete_run_count": 1,
            },
        )
    )
    return build_phase8_1_source_health_report(
        source_ids=normalized,
        checkpoint_documents=checkpoints,
        cycle_documents=cycles,
        job_documents=jobs,
        cadence_hours=cadence_hours,
        failure_retry_hours=failure_retry_hours,
        now=now,
    )


def write_phase8_1_source_health_reports(
    report: Mapping[str, Any],
    *,
    output_dir: Path,
) -> tuple[Path, Path]:
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "phase8_1_source_health_report.json"
    csv_path = destination / "phase8_1_source_health_report.csv"
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )

    columns = [
        "source_id",
        "display_name",
        "health_status",
        "attention_required",
        "attention_reasons",
        "latest_checkpoint_status",
        "last_run_at",
        "last_success_at",
        "last_complete_at",
        "next_due_at",
        "is_due",
        "consecutive_failures",
        "runtime_seconds",
        "discovered",
        "accepted",
        "quarantined",
        "inserted",
        "updated",
        "unchanged",
        "missing",
        "deactivated",
        "current_total_jobs",
        "current_active_jobs",
        "current_inactive_jobs",
        "current_missing_candidates",
        "latest_error_type",
        "latest_error_message",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for source in report.get("sources") or []:
            latest = _as_dict(source.get("latest_run_counts"))
            current = _as_dict(source.get("current_jobs"))
            writer.writerow(
                {
                    "source_id": source.get("source_id"),
                    "display_name": source.get("display_name"),
                    "health_status": source.get("health_status"),
                    "attention_required": source.get("attention_required"),
                    "attention_reasons": ";".join(
                        source.get("attention_reasons") or []
                    ),
                    "latest_checkpoint_status": source.get(
                        "latest_checkpoint_status"
                    ),
                    "last_run_at": source.get("last_run_at"),
                    "last_success_at": source.get("last_success_at"),
                    "last_complete_at": source.get("last_complete_at"),
                    "next_due_at": source.get("next_due_at"),
                    "is_due": source.get("is_due"),
                    "consecutive_failures": source.get("consecutive_failures"),
                    "runtime_seconds": source.get("runtime_seconds"),
                    "discovered": latest.get("discovered"),
                    "accepted": latest.get("accepted"),
                    "quarantined": latest.get("quarantined"),
                    "inserted": latest.get("inserted"),
                    "updated": latest.get("updated"),
                    "unchanged": latest.get("unchanged"),
                    "missing": latest.get("missing"),
                    "deactivated": latest.get("deactivated"),
                    "current_total_jobs": current.get("total"),
                    "current_active_jobs": current.get("active"),
                    "current_inactive_jobs": current.get("inactive"),
                    "current_missing_candidates": current.get(
                        "missing_candidates"
                    ),
                    "latest_error_type": source.get("latest_error_type"),
                    "latest_error_message": source.get("latest_error_message"),
                }
            )
    return json_path, csv_path

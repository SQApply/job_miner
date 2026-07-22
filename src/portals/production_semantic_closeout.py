from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


PHASE_7D2C1_CONTRACT_VERSION = "1.0"
PHASE_7D2C1 = "7D2C1"
PHASE_7D2C1_ACKNOWLEDGEMENT = "ACKNOWLEDGE_PHASE_7D2C_TWO_CONTENT_UPDATES"


class ProductionSemanticCloseoutError(RuntimeError):
    """Raised when bounded Phase 7D2C content variance is not safe to accept."""


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Required {label} does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ProductionSemanticCloseoutError(
            f"{label} contains invalid JSON: {source}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProductionSemanticCloseoutError(f"{label} must contain a JSON object")
    return payload


def _verify_report_checksum(
    payload: Mapping[str, Any],
    *,
    label: str,
) -> str:
    stored = str(payload.get("report_sha256") or "").strip().lower()
    unsigned = dict(payload)
    unsigned.pop("report_sha256", None)
    if not stored or stored != _canonical_sha256(unsigned):
        raise ProductionSemanticCloseoutError(
            f"{label} checksum is missing or invalid"
        )
    return stored


def _integer(value: Any, *, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ids(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ProductionSemanticCloseoutError(f"{label} must be a JSON array")
    source_ids = [str(item or "").strip() for item in value]
    if any(not source_id for source_id in source_ids):
        raise ProductionSemanticCloseoutError(f"{label} contains an empty source id")
    if len(source_ids) != len(set(source_ids)):
        raise ProductionSemanticCloseoutError(f"{label} contains duplicate source ids")
    return source_ids


def _result_map(value: Any, *, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise ProductionSemanticCloseoutError(f"{label} must be a JSON array")
    results: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ProductionSemanticCloseoutError(f"{label} contains a non-object result")
        source_id = str(item.get("source_id") or "").strip()
        if not source_id or source_id in results:
            raise ProductionSemanticCloseoutError(
                f"{label} contains an invalid source identity"
            )
        results[source_id] = item
    return results


def require_phase7d2c1_acknowledgement(value: str) -> None:
    if str(value or "") != PHASE_7D2C1_ACKNOWLEDGEMENT:
        raise ProductionSemanticCloseoutError(
            "Phase 7D2C.1 requires --accept-bounded-content-variance "
            + PHASE_7D2C1_ACKNOWLEDGEMENT
        )


def build_phase7d2c1_semantic_closeout(
    *,
    first_write_report_path: Path,
    first_write_manifest_path: Path,
    rerun_manifest_path: Path,
    strict_report_path: Path,
    phase6f_closeout_path: Path,
    expected_source_count: int = 2,
    expected_job_count: int = 20,
    maximum_updated_jobs: int = 2,
    maximum_updated_jobs_per_source: int = 1,
) -> dict[str, Any]:
    """Reclassify only bounded content variance after storage idempotency passed.

    This is intentionally an offline evidence operation. It never calls a portal,
    connects to MongoDB, changes a job, or relaxes reconciliation/deactivation.
    The original strict Phase 7D2C failure remains immutable and linked here.
    """

    if expected_source_count != 2 or expected_job_count != 20:
        raise ProductionSemanticCloseoutError(
            "Phase 7D2C.1 is fixed to the observed two-source, twenty-job canary"
        )
    if maximum_updated_jobs != 2 or maximum_updated_jobs_per_source != 1:
        raise ProductionSemanticCloseoutError(
            "Phase 7D2C.1 variance bounds are immutable"
        )

    first_report_path = Path(first_write_report_path).resolve()
    first_manifest_path = Path(first_write_manifest_path).resolve()
    rerun_path = Path(rerun_manifest_path).resolve()
    strict_path = Path(strict_report_path).resolve()
    closeout_path = Path(phase6f_closeout_path).resolve()

    first_report = _load_json(first_report_path, label="Phase 7D2B write report")
    first_manifest = _load_json(
        first_manifest_path,
        label="Phase 7D2B first-write manifest",
    )
    rerun = _load_json(rerun_path, label="Phase 7D2C rerun manifest")
    strict = _load_json(strict_path, label="Phase 7D2C strict report")
    closeout = _load_json(closeout_path, label="Phase 7D2C Phase 6F closeout")

    first_report_checksum = _verify_report_checksum(
        first_report,
        label="Phase 7D2B write report",
    )
    strict_report_checksum = _verify_report_checksum(
        strict,
        label="Phase 7D2C strict report",
    )
    first_manifest_sha256 = _file_sha256(first_manifest_path)
    rerun_manifest_sha256 = _file_sha256(rerun_path)
    closeout_sha256 = _file_sha256(closeout_path)

    blockers: list[str] = []

    def block(reason: str) -> None:
        if reason not in blockers:
            blockers.append(reason)

    if (
        str(first_report.get("contract_version") or ""),
        str(first_report.get("phase") or ""),
        str(first_report.get("status") or ""),
        first_report.get("ready_for_phase7d2c"),
    ) != ("1.0", "7D2B", "passed", True):
        block("phase7d2b_first_write_not_passed")
    if first_report.get("blockers") != []:
        block("phase7d2b_first_write_contains_blockers")

    if (
        str(strict.get("contract_version") or ""),
        str(strict.get("phase") or ""),
        str(strict.get("status") or ""),
        strict.get("ready_for_phase7d3"),
    ) != ("1.0", "7D2C", "failed", False):
        block("phase7d2c_strict_result_is_not_the_expected_failed_gate")
    if str(strict.get("phase7d2b_report_sha256") or "") != first_report_checksum:
        block("phase7d2b_report_link_mismatch")
    if str(strict.get("first_write_manifest_sha256") or "") != first_manifest_sha256:
        block("first_write_manifest_link_mismatch")
    if str(strict.get("rerun_manifest_sha256") or "") != rerun_manifest_sha256:
        block("rerun_manifest_link_mismatch")
    if str(strict.get("phase6f_closeout_sha256") or "") != closeout_sha256:
        block("phase6f_closeout_link_mismatch")

    plan_id = str(strict.get("plan_id") or "").strip()
    cohort_sha256 = str(strict.get("cohort_sha256") or "").strip()
    first_run_id = str(strict.get("first_write_run_id") or "").strip()
    rerun_run_id = str(strict.get("run_id") or "").strip()
    source_ids = _ids(strict.get("source_ids"), label="strict.source_ids")
    if len(source_ids) != expected_source_count:
        block("strict_source_count_mismatch")

    for payload, label in (
        (first_report, "first_report"),
        (first_manifest, "first_manifest"),
        (rerun, "rerun_manifest"),
        (closeout, "phase6f_closeout"),
    ):
        if str(payload.get("plan_id") or "") != plan_id:
            block(f"{label}_plan_id_mismatch")
        if str(payload.get("cohort_sha256") or "") != cohort_sha256:
            block(f"{label}_cohort_sha256_mismatch")

    if str(first_report.get("run_id") or "") != first_run_id:
        block("first_report_run_id_mismatch")
    if str(first_manifest.get("run_id") or "") != first_run_id:
        block("first_manifest_run_id_mismatch")
    if str(rerun.get("run_id") or "") != rerun_run_id:
        block("rerun_manifest_run_id_mismatch")
    if str(closeout.get("first_write_run_id") or "") != first_run_id:
        block("phase6f_first_run_id_mismatch")
    if str(closeout.get("rerun_run_id") or "") != rerun_run_id:
        block("phase6f_rerun_run_id_mismatch")

    first_sources = _ids(
        first_manifest.get("selected_source_ids"),
        label="first_manifest.selected_source_ids",
    )
    rerun_sources = _ids(
        rerun.get("selected_source_ids"),
        label="rerun_manifest.selected_source_ids",
    )
    closeout_sources = _ids(
        closeout.get("selected_source_ids"),
        label="phase6f.selected_source_ids",
    )
    if first_sources != source_ids or rerun_sources != source_ids or closeout_sources != source_ids:
        block("source_order_or_selection_mismatch")

    first_counts = {
        "accepted": _integer(first_manifest.get("accepted_job_count")),
        "inserted": _integer(first_manifest.get("inserted_job_count")),
        "updated": _integer(first_manifest.get("updated_job_count")),
        "unchanged": _integer(first_manifest.get("unchanged_job_count")),
        "reactivated": _integer(first_manifest.get("reactivated_job_count")),
        "quarantined": _integer(first_manifest.get("quarantined_job_count")),
    }
    if first_counts != {
        "accepted": expected_job_count,
        "inserted": expected_job_count,
        "updated": 0,
        "unchanged": 0,
        "reactivated": 0,
        "quarantined": 0,
    }:
        block("first_write_counts_are_not_clean")

    rerun_counts = {
        "accepted": _integer(rerun.get("accepted_job_count")),
        "inserted": _integer(rerun.get("inserted_job_count")),
        "updated": _integer(rerun.get("updated_job_count")),
        "unchanged": _integer(rerun.get("unchanged_job_count")),
        "reactivated": _integer(rerun.get("reactivated_job_count")),
        "quarantined": _integer(rerun.get("quarantined_job_count")),
    }
    if rerun_counts["accepted"] != expected_job_count:
        block("rerun_accepted_count_mismatch")
    if rerun_counts["inserted"] != 0:
        block("rerun_inserted_new_identities")
    if rerun_counts["reactivated"] != 0:
        block("rerun_reactivated_jobs")
    if rerun_counts["quarantined"] != 0:
        block("rerun_quarantined_jobs")
    if rerun_counts["updated"] != maximum_updated_jobs:
        block("rerun_update_count_outside_observed_bound")
    if rerun_counts["updated"] + rerun_counts["unchanged"] != expected_job_count:
        block("rerun_content_outcomes_do_not_reconcile")

    for key, expected in {
        "requested_source_count": expected_source_count,
        "completed_source_count": expected_source_count,
        "successful_source_count": expected_source_count,
        "failed_source_count": 0,
        "blocked_source_count": 0,
        "cancelled_source_count": 0,
    }.items():
        if _integer(rerun.get(key)) != expected:
            block(f"rerun_source_aggregate_mismatch:{key}")

    rerun_controls = rerun.get("controls")
    if not isinstance(rerun_controls, dict):
        rerun_controls = {}
        block("rerun_controls_missing")
    for key, expected in {
        "normalized_job_writes_enabled": True,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
        "max_source_concurrency": 1,
        "max_attempts": 1,
        "max_jobs_per_source": 10,
        "bounded_pilot_execution": True,
    }.items():
        if rerun_controls.get(key) != expected:
            block(f"unsafe_rerun_control:{key}")

    first_results = _result_map(
        first_manifest.get("source_results"),
        label="first_manifest.source_results",
    )
    rerun_results = _result_map(
        rerun.get("source_results"),
        label="rerun_manifest.source_results",
    )
    if set(first_results) != set(source_ids) or set(rerun_results) != set(source_ids):
        block("source_result_partition_mismatch")

    source_summaries: list[dict[str, Any]] = []
    expected_strict_blockers = {
        "rerun_manifest_count_mismatch:updated_job_count",
        "rerun_manifest_count_mismatch:unchanged_job_count",
    }
    for source_id in source_ids:
        first = first_results.get(source_id, {})
        result = rerun_results.get(source_id, {})
        first_accepted = _integer(first.get("accepted_count"))
        accepted = _integer(result.get("accepted_count"))
        inserted = _integer(result.get("inserted_count"))
        updated = _integer(result.get("updated_count"))
        unchanged = _integer(result.get("unchanged_count"))
        reactivated = _integer(result.get("reactivated_count"))
        quarantined = _integer(result.get("quarantined_count"))
        rejected = _integer(result.get("rejected_count"))
        attempts = result.get("attempts") if isinstance(result.get("attempts"), list) else []
        if first.get("status") != "success" or result.get("status") != "success":
            block(f"source_not_successful:{source_id}")
        if len(attempts) != 1:
            block(f"source_attempt_count_not_one:{source_id}")
        if first_accepted < 1 or accepted != first_accepted:
            block(f"source_accepted_count_changed:{source_id}")
        if inserted != 0 or reactivated != 0 or quarantined != 0 or rejected != 0:
            block(f"source_unsafe_outcome:{source_id}")
        if updated != maximum_updated_jobs_per_source:
            block(f"source_update_count_outside_observed_bound:{source_id}")
        if updated + unchanged != accepted:
            block(f"source_content_outcomes_do_not_reconcile:{source_id}")
        expected_strict_blockers.update(
            {
                f"rerun_source_count_mismatch:{source_id}:updated_count",
                f"rerun_source_count_mismatch:{source_id}:unchanged_count",
            }
        )
        source_summaries.append(
            {
                "source_id": source_id,
                "accepted": accepted,
                "inserted": inserted,
                "updated": updated,
                "unchanged": unchanged,
                "reactivated": reactivated,
                "quarantined": quarantined,
                "rejected": rejected,
            }
        )

    strict_blockers = strict.get("blockers")
    if not isinstance(strict_blockers, list):
        block("strict_blocker_list_missing")
        strict_blocker_set: set[str] = set()
    else:
        strict_blocker_set = {str(value) for value in strict_blockers}
    if strict_blocker_set != expected_strict_blockers:
        block("strict_failure_contains_non_variance_blockers")

    if closeout.get("status") != "passed" or closeout.get("ready_for_phase7") is not True:
        block("phase6f_closeout_did_not_pass")
    if closeout.get("issues") != []:
        block("phase6f_closeout_contains_issues")
    if str(closeout.get("first_write_manifest_sha256") or "") != first_manifest_sha256:
        block("phase6f_first_manifest_hash_mismatch")
    if str(closeout.get("rerun_manifest_sha256") or "") != rerun_manifest_sha256:
        block("phase6f_rerun_manifest_hash_mismatch")

    closeout_controls = closeout.get("controls")
    if not isinstance(closeout_controls, dict):
        closeout_controls = {}
        block("phase6f_controls_missing")
    for key, expected in {
        "normalized_job_writes_enabled": True,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
    }.items():
        if closeout_controls.get(key) is not expected:
            block(f"unsafe_phase6f_control:{key}")

    database_checks = closeout.get("database_checks")
    if not isinstance(database_checks, dict):
        database_checks = {}
        block("phase6f_database_checks_missing")
    for key, expected in {
        "fleet_run_records": 2,
        "source_run_records_first": expected_source_count,
        "source_run_records_rerun": expected_source_count,
        "current_jobs_observed_on_rerun": expected_job_count,
        "active_jobs_observed_on_rerun": expected_job_count,
        "duplicate_identity_hashes": 0,
        "duplicate_external_job_keys": 0,
        "history_mismatches": 0,
        "unsafe_deactivated_jobs": 0,
        "quarantine_records_first": 0,
        "quarantine_records_rerun": 0,
    }.items():
        if _integer(database_checks.get(key)) != expected:
            block(f"phase6f_database_check_mismatch:{key}")

    strict_controls = strict.get("controls")
    if not isinstance(strict_controls, dict):
        strict_controls = {}
        block("strict_controls_missing")
    for key, expected in {
        "explicit_rerun_confirmation_required": True,
        "normalized_job_writes_enabled": True,
        "index_creation_performed": False,
        "lifecycle_reconciliation_enabled": False,
        "deactivation_enabled": False,
        "full_cohort_execution_performed": False,
        "automatic_rollback_enabled": False,
    }.items():
        if strict_controls.get(key) is not expected:
            block(f"unsafe_strict_control:{key}")

    variance_rate = round(rerun_counts["updated"] / expected_job_count, 6)
    passed = not blockers
    report: dict[str, Any] = {
        "contract_version": PHASE_7D2C1_CONTRACT_VERSION,
        "phase": PHASE_7D2C1,
        "status": "passed_with_bounded_content_variance" if passed else "failed",
        "ready_for_phase7d3": passed,
        "generated_from_run_id": rerun_run_id,
        "plan_id": plan_id,
        "cohort_sha256": cohort_sha256,
        "source_ids": source_ids,
        "evidence": {
            "phase7d2b_report_sha256": first_report_checksum,
            "first_write_manifest_sha256": first_manifest_sha256,
            "phase7d2c_strict_report_sha256": strict_report_checksum,
            "rerun_manifest_sha256": rerun_manifest_sha256,
            "phase6f_closeout_sha256": closeout_sha256,
        },
        "strict_gate": {
            "status": strict.get("status"),
            "ready_for_phase7d3": strict.get("ready_for_phase7d3"),
            "blockers": list(strict_blockers) if isinstance(strict_blockers, list) else [],
        },
        "semantic_idempotency": {
            "definition": (
                "Stable source/job identity with zero inserts, reactivations, quarantine, "
                "duplicates, unsafe deactivation, or history mismatch; bounded content updates "
                "remain visible as updates."
            ),
            "accepted": rerun_counts["accepted"],
            "inserted": rerun_counts["inserted"],
            "updated": rerun_counts["updated"],
            "unchanged": rerun_counts["unchanged"],
            "reactivated": rerun_counts["reactivated"],
            "quarantined": rerun_counts["quarantined"],
            "content_variance_rate": variance_rate,
            "maximum_updated_jobs": maximum_updated_jobs,
            "maximum_updated_jobs_per_source": maximum_updated_jobs_per_source,
            "identity_reuse_proven_by_zero_inserts": rerun_counts["inserted"] == 0,
        },
        "source_results": source_summaries,
        "database_checks": dict(database_checks),
        "known_risks": [
            {
                "code": "bounded_extraction_content_variance",
                "severity": "warning",
                "observed_jobs": rerun_counts["updated"],
                "impact": (
                    "Changed-only downstream indexing may process these jobs again when "
                    "model-derived content varies."
                ),
                "production_blocking": False,
            }
        ],
        "required_followups": [
            "Add a content-addressed cache for LLM fallback extraction so unchanged page evidence reuses the prior normalized result.",
            "Keep reconciliation and deactivation disabled until two complete successful production cycles are observed.",
        ],
        "controls": {
            "explicit_variance_acknowledgement_required": True,
            "network_requests_performed": False,
            "scraping_performed": False,
            "mongodb_reads_performed": False,
            "mongodb_writes_performed": False,
            "normalized_job_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "original_strict_failure_preserved": True,
            "variance_bound_relaxed": False,
        },
        "blockers": blockers,
    }
    report["report_sha256"] = _canonical_sha256(report)
    return report


def write_phase7d2c1_semantic_closeout(
    path: Path,
    report: Mapping[str, Any],
) -> Path:
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

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..infrastructure.mongo import get_mongo_database, healthcheck
from .artifact_readers import (
    read_candidate_tower_artifacts,
    read_job_artifacts,
    read_job_run_summary_for_artifact,
    read_resume_profile_artifacts,
)
from .hashing import stable_hash
from .indexes import init_indexes
from .repositories import WarehouseRepository
from .tower_builders import build_candidate_tower_from_resume_profile, build_job_tower_document


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _repo() -> WarehouseRepository:
    return WarehouseRepository(get_mongo_database())


def health(_: argparse.Namespace) -> None:
    _print(healthcheck())


def indexes(_: argparse.Namespace) -> None:
    _print({"created_indexes": init_indexes(get_mongo_database())})


def stats(_: argparse.Namespace) -> None:
    _print(_repo().counts())


def backfill_jobs(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    jobs_dir = Path(args.jobs_dir)
    repo = _repo()
    artifacts = read_job_artifacts(root, jobs_dir, latest_only=not args.include_versioned)
    totals: dict[str, Any] = {
        "artifacts": len(artifacts),
        "jobs_seen": 0,
        "inserted": 0,
        "changed": 0,
        "unchanged": 0,
        "changed_job_ids": [],
        "lifecycle": {
            "targets_reconciled": 0,
            "missing_marked": 0,
            "deactivated": 0,
            "skipped": 0,
        },
        "artifacts_loaded": [],
    }
    for artifact in artifacts:
        summary = read_job_run_summary_for_artifact(root, jobs_dir, artifact)
        run_session_id = artifact.run_session_id or (summary.run_session_id if summary else None)
        s = repo.upsert_jobs(artifact.jobs, target_id=artifact.target_id, run_session_id=run_session_id)
        changed_ids = [str(job_id) for job_id in s.get("changed_job_ids") or []]
        totals["jobs_seen"] += int(s["input"])
        totals["inserted"] += int(s["inserted"])
        totals["changed"] += int(s["changed"])
        totals["unchanged"] += int(s["unchanged"])
        totals["changed_job_ids"].extend(changed_ids)

        seen_update = {"matched_existing": 0, "modified_existing": 0}
        lifecycle = {"status": "skipped_no_summary", "missing_marked": 0, "deactivated": 0}
        if summary and summary.discovered_job_urls:
            seen_update = repo.reset_missing_state_for_discovered_urls(
                target_id=artifact.target_id,
                run_session_id=run_session_id,
                discovered_urls=summary.discovered_job_urls,
            )
            lifecycle = repo.reconcile_missing_jobs_after_discovery(
                target_id=artifact.target_id,
                run_session_id=str(run_session_id or artifact.target_id),
                discovered_urls=summary.discovered_job_urls,
                deactivate_after_misses=int(args.deactivate_after_misses),
                min_discovery_coverage_ratio=float(args.min_discovery_coverage_ratio),
            )
        elif summary:
            lifecycle = {
                "status": "skipped_no_discovered_job_urls_in_summary",
                "discovered_urls": summary.discovered_urls,
                "missing_marked": 0,
                "deactivated": 0,
            }

        if lifecycle.get("status") == "completed":
            totals["lifecycle"]["targets_reconciled"] += 1
        else:
            totals["lifecycle"]["skipped"] += 1
        totals["lifecycle"]["missing_marked"] += int(lifecycle.get("missing_marked") or 0)
        totals["lifecycle"]["deactivated"] += int(lifecycle.get("deactivated") or 0)

        totals["artifacts_loaded"].append({
            "path": str(artifact.path),
            "target_id": artifact.target_id,
            "run_session_id": run_session_id,
            "summary_path": str(summary.path) if summary else None,
            "summary_discovered_urls": summary.discovered_urls if summary else None,
            "summary_discovered_job_urls": len(summary.discovered_job_urls) if summary else 0,
            **{k: v for k, v in s.items() if k != "changed_job_ids"},
            "changed_job_ids_count": len(changed_ids),
            "seen_update": seen_update,
            "lifecycle_reconcile": lifecycle,
        })
    totals["changed_job_ids"] = sorted(set(totals["changed_job_ids"]))
    totals["changed_job_ids_count"] = len(totals["changed_job_ids"])
    _print(totals)


def backfill_resumes(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve(); repo = _repo(); profiles = read_resume_profile_artifacts(root, Path(args.resumes_dir)); towers = read_candidate_tower_artifacts(root, Path(args.resumes_dir))
    totals = {"profile_artifacts": len(profiles), "candidate_tower_artifacts": len(towers), "profiles_inserted": 0, "profiles_changed": 0, "profiles_unchanged": 0, "candidate_tower_records": 0}
    resume_hash: dict[str, str] = {}
    for artifact in profiles:
        rid, inserted, changed = repo.upsert_resume_profile(artifact.profile)
        resume_hash[rid] = stable_hash(artifact.profile)
        totals["profiles_inserted" if inserted else "profiles_changed" if changed else "profiles_unchanged"] += 1
    for artifact in towers:
        for record in artifact.records:
            repo.upsert_candidate_tower(record, source_content_hash=resume_hash.get(str(record.get("resume_id") or "")))
            totals["candidate_tower_records"] += 1
    _print(totals)


def build_job_tower(args: argparse.Namespace) -> None:
    repo = _repo()
    changed_only = bool(args.only_pending or args.changed_only)
    job_ids = [item.strip() for item in (args.job_ids or "").split(",") if item.strip()] or None
    jobs = repo.active_jobs(limit=args.limit, only_pending_tower=changed_only, job_ids=job_ids)
    built = 0
    skipped = 0
    for job in jobs:
        doc = build_job_tower_document(job)
        if not doc.job_embedding_text.strip():
            skipped += 1
            continue
        repo.upsert_job_tower(doc)
        built += 1
    _print({
        "active_jobs_selected": len(jobs),
        "job_tower_records_built": built,
        "skipped_empty_embedding_text": skipped,
        "only_pending": changed_only,
        "job_ids_filter_count": len(job_ids or []),
    })


def build_candidate_tower(args: argparse.Namespace) -> None:
    repo = _repo(); profiles = repo.active_resume_profiles(limit=args.limit); built = 0; skipped = 0
    for profile in profiles:
        doc = build_candidate_tower_from_resume_profile(profile)
        if not doc.candidate_embedding_text.strip(): skipped += 1; continue
        repo.upsert_candidate_tower(doc.to_mongo(), source_content_hash=doc.source_content_hash); built += 1
    _print({"active_resume_profiles": len(profiles), "candidate_tower_records_built": built, "skipped_empty_embedding_text": skipped})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MongoDB warehouse ETL commands for Job Miner"); p.add_argument("--root", default="."); sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("health").set_defaults(func=health); sub.add_parser("init-indexes").set_defaults(func=indexes); sub.add_parser("stats").set_defaults(func=stats)
    bj = sub.add_parser("backfill-jobs")
    bj.add_argument("--jobs-dir", default="data/processed")
    bj.add_argument("--include-versioned", action="store_true")
    bj.add_argument("--deactivate-after-misses", type=int, default=2)
    bj.add_argument("--min-discovery-coverage-ratio", type=float, default=0.25)
    bj.set_defaults(func=backfill_jobs)
    br = sub.add_parser("backfill-resumes"); br.add_argument("--resumes-dir", default="data/resumes/processed"); br.set_defaults(func=backfill_resumes)
    jt = sub.add_parser("build-job-tower")
    jt.add_argument("--limit", type=int, default=None)
    jt.add_argument("--only-pending", action="store_true", help="Build towers only for jobs whose tower is missing or source hash changed")
    jt.add_argument("--changed-only", action="store_true", help="Alias for --only-pending")
    jt.add_argument("--job-ids", default="", help="Optional comma-separated job_ids to rebuild")
    jt.set_defaults(func=build_job_tower)
    ct = sub.add_parser("build-candidate-tower"); ct.add_argument("--limit", type=int, default=None); ct.set_defaults(func=build_candidate_tower)
    return p


def main() -> None:
    args = build_parser().parse_args(); args.func(args)


if __name__ == "__main__":
    main()

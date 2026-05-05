from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..infrastructure.mongo import get_mongo_database, healthcheck
from .artifact_readers import read_candidate_tower_artifacts, read_job_artifacts, read_resume_profile_artifacts
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
    root = Path(args.root).resolve(); repo = _repo(); artifacts = read_job_artifacts(root, Path(args.jobs_dir), latest_only=not args.include_versioned)
    totals = {"artifacts": len(artifacts), "jobs_seen": 0, "inserted": 0, "changed": 0, "unchanged": 0, "artifacts_loaded": []}
    for artifact in artifacts:
        s = repo.upsert_jobs(artifact.jobs, target_id=artifact.target_id, run_session_id=artifact.run_session_id)
        totals["jobs_seen"] += s["input"]; totals["inserted"] += s["inserted"]; totals["changed"] += s["changed"]; totals["unchanged"] += s["unchanged"]
        totals["artifacts_loaded"].append({"path": str(artifact.path), "target_id": artifact.target_id, "run_session_id": artifact.run_session_id, **s})
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
    repo = _repo(); jobs = repo.active_jobs(limit=args.limit); built = 0; skipped = 0
    for job in jobs:
        doc = build_job_tower_document(job)
        if not doc.job_embedding_text.strip(): skipped += 1; continue
        repo.upsert_job_tower(doc); built += 1
    _print({"active_jobs": len(jobs), "job_tower_records_built": built, "skipped_empty_embedding_text": skipped})


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
    bj = sub.add_parser("backfill-jobs"); bj.add_argument("--jobs-dir", default="data/processed"); bj.add_argument("--include-versioned", action="store_true"); bj.set_defaults(func=backfill_jobs)
    br = sub.add_parser("backfill-resumes"); br.add_argument("--resumes-dir", default="data/resumes/processed"); br.set_defaults(func=backfill_resumes)
    jt = sub.add_parser("build-job-tower"); jt.add_argument("--limit", type=int, default=None); jt.set_defaults(func=build_job_tower)
    ct = sub.add_parser("build-candidate-tower"); ct.add_argument("--limit", type=int, default=None); ct.set_defaults(func=build_candidate_tower)
    return p


def main() -> None:
    args = build_parser().parse_args(); args.func(args)


if __name__ == "__main__":
    main()

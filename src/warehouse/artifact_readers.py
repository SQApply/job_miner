from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class JobArtifact:
    path: Path
    target_id: str
    run_session_id: str | None
    jobs: list[dict[str, Any]]


@dataclass(slots=True)
class JobRunSummaryArtifact:
    path: Path
    target_id: str
    run_session_id: str | None
    discovered_job_urls: list[str]
    discovered_urls: int = 0
    attempted_urls: int = 0
    extracted_jobs: int = 0
    rescrape_plan: dict[str, Any] | None = None
    lifecycle_reconcile: dict[str, Any] | None = None


@dataclass(slots=True)
class ResumeArtifact:
    path: Path
    profile: dict[str, Any]


@dataclass(slots=True)
class CandidateTowerArtifact:
    path: Path
    records: list[dict[str, Any]]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _output_mapping(root: Path) -> dict[str, str]:
    path = root / "blueprints" / "site_registry.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {Path(site["output_file"]).stem: site["id"] for site in raw.get("sites", []) if site.get("id") and site.get("output_file")}


def _is_job_json(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    name = path.name.lower()
    blocked_exact_parts = {
        "summaries",
        "failed",
        "logs",
        "resumes",
        "matching",
        "matching_llm",
        "candidate_tower",
        "job_tower",
    }
    if path.suffix.lower() != ".json":
        return False
    if parts.intersection(blocked_exact_parts):
        return False
    if any(part.startswith("matching") for part in parts):
        return False
    if name.endswith("_summary.json") or name.startswith("candidate_job_matches"):
        return False
    return True


def _infer(path: Path, mapping: dict[str, str]) -> tuple[str, str | None]:
    stem = path.stem
    if stem in mapping:
        return mapping[stem], None
    for out_stem, target_id in sorted(mapping.items(), key=lambda x: len(x[0]), reverse=True):
        if stem.startswith(out_stem + "_"):
            return target_id, stem[len(out_stem) + 1 :]
    cleaned = re.sub(r"_\d{8}T\d{6}Z$", "", stem)
    return cleaned, None if cleaned == stem else stem


def read_job_artifacts(root: Path, jobs_dir: Path, *, latest_only: bool = True) -> list[JobArtifact]:
    base = jobs_dir if jobs_dir.is_absolute() else root / jobs_dir
    mapping = _output_mapping(root)
    known_target_ids = set(mapping.values())
    out: list[JobArtifact] = []
    for path in sorted(base.rglob("*.json")):
        if not _is_job_json(path):
            continue
        target_id, run_id = _infer(path, mapping)
        # Be strict: backfill-jobs should only load known scrape output files,
        # never matching/recommendation artifacts or arbitrary JSON dumps.
        if target_id not in known_target_ids:
            continue
        if latest_only and run_id is not None:
            continue
        try:
            payload = _read_json(path)
        except Exception:
            continue
        if isinstance(payload, list):
            jobs = [x for x in payload if isinstance(x, dict)]
            # Keep empty latest artifacts too. They carry a run_session_id via the
            # paired summary and allow safe missing-job reconciliation to skip or
            # complete explicitly.
            out.append(JobArtifact(path, target_id, run_id, jobs))
    return out


def _summary_path_for_artifact(base: Path, artifact: JobArtifact) -> Path | None:
    summaries = base / "summaries"
    if not summaries.exists():
        return None
    if artifact.run_session_id:
        candidates = [summaries / f"{artifact.target_id}_{artifact.run_session_id}_summary.json"]
    else:
        candidates = [summaries / f"{artifact.target_id}_latest_summary.json"]
    for path in candidates:
        if path.exists():
            return path
    return None


def read_job_run_summary_for_artifact(root: Path, jobs_dir: Path, artifact: JobArtifact) -> JobRunSummaryArtifact | None:
    base = jobs_dir if jobs_dir.is_absolute() else root / jobs_dir
    path = _summary_path_for_artifact(base, artifact)
    if path is None:
        return None
    try:
        payload = _read_json(path)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    discovered_job_urls = [str(url) for url in payload.get("discovered_job_urls") or [] if str(url).strip()]
    if not discovered_job_urls:
        # Backward compatibility for summaries created before this patch.  In
        # those runs every discovered URL was also detail-scraped, so the job
        # artifact URLs are the best available discovery set.
        for job in artifact.jobs:
            url = str(job.get("job_url") or job.get("source_url") or job.get("url") or "").strip()
            if url:
                discovered_job_urls.append(url)
    return JobRunSummaryArtifact(
        path=path,
        target_id=str(payload.get("target_id") or artifact.target_id),
        run_session_id=str(payload.get("run_session_id") or artifact.run_session_id or "") or None,
        discovered_job_urls=discovered_job_urls,
        discovered_urls=int(payload.get("discovered_urls") or len(discovered_job_urls)),
        attempted_urls=int(payload.get("attempted_urls") or 0),
        extracted_jobs=int(payload.get("extracted_jobs") or 0),
        rescrape_plan=payload.get("rescrape_plan") if isinstance(payload.get("rescrape_plan"), dict) else {},
        lifecycle_reconcile=payload.get("lifecycle_reconcile") if isinstance(payload.get("lifecycle_reconcile"), dict) else {},
    )


def read_resume_profile_artifacts(root: Path, resumes_dir: Path) -> list[ResumeArtifact]:
    base = resumes_dir if resumes_dir.is_absolute() else root / resumes_dir
    out: list[ResumeArtifact] = []
    for path in sorted(base.rglob("*_profile.json")):
        try:
            payload = _read_json(path)
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("resume_id") and payload.get("sha256"):
            out.append(ResumeArtifact(path, payload))
    return out


def read_candidate_tower_artifacts(root: Path, resumes_dir: Path) -> list[CandidateTowerArtifact]:
    base = resumes_dir if resumes_dir.is_absolute() else root / resumes_dir
    out: list[CandidateTowerArtifact] = []
    for path in sorted(base.rglob("candidate_tower/*.jsonl")):
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
        if records:
            out.append(CandidateTowerArtifact(path, records))
    return out

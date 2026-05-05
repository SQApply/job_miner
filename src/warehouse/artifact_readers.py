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
    return path.suffix.lower() == ".json" and "summaries" not in parts and "failed" not in parts and "logs" not in parts and "job_tower" not in parts and "matching" not in parts and "resumes" not in parts and not name.endswith("_summary.json")


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
    out: list[JobArtifact] = []
    for path in sorted(base.rglob("*.json")):
        if not _is_job_json(path):
            continue
        target_id, run_id = _infer(path, mapping)
        if latest_only and run_id is not None:
            continue
        try:
            payload = _read_json(path)
        except Exception:
            continue
        if isinstance(payload, list):
            jobs = [x for x in payload if isinstance(x, dict)]
            if jobs:
                out.append(JobArtifact(path, target_id, run_id, jobs))
    return out


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

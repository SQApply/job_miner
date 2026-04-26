from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from ..schemas import CandidateTowerRecord, OcrDocument, ResumeProfile
from ..utils import ensure_dir, slugify


def resume_output_dir(base_output_dir: Path, resume_id: str, file_name: str) -> Path:
    return ensure_dir(base_output_dir / "resumes" / f"{resume_id}_{slugify(Path(file_name).stem)}")


def save_ocr_document(output_dir: Path, doc: OcrDocument) -> dict[str, Path]:
    out_dir = resume_output_dir(output_dir, doc.resume_id, doc.file_name)
    markdown_path = out_dir / f"{doc.resume_id}_ocr.md"
    json_path = out_dir / f"{doc.resume_id}_ocr.json"
    raw_path = out_dir / f"{doc.resume_id}_ocr_raw.json"

    markdown_path.write_text(doc.markdown, encoding="utf-8")
    json_path.write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    if doc.raw_result is not None:
        raw_path.write_text(json.dumps(doc.raw_result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    return {"markdown_path": markdown_path, "ocr_json_path": json_path, "raw_json_path": raw_path}


def save_resume_profile(output_dir: Path, profile: ResumeProfile) -> Path:
    out_dir = resume_output_dir(output_dir, profile.resume_id, profile.source_file_name)
    path = out_dir / f"{profile.resume_id}_profile.json"
    path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    return path


def append_jsonl(path: Path, rows: Iterable[dict]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def save_candidate_tower_records(output_dir: Path, records: list[CandidateTowerRecord], run_session_id: str) -> tuple[Path, Path]:
    tower_dir = ensure_dir(output_dir / "candidate_tower")
    versioned_path = tower_dir / f"candidates_{run_session_id}.jsonl"
    latest_path = tower_dir / "candidates_latest.jsonl"
    rows = [record.model_dump() for record in records]

    versioned_path.write_text("", encoding="utf-8")
    latest_path.write_text("", encoding="utf-8")
    append_jsonl(versioned_path, rows)
    append_jsonl(latest_path, rows)
    return versioned_path, latest_path


def save_failed_payload(failed_dir: Path, resume_id: str, file_name: str, stage: str, payload: object) -> Path:
    out_dir = ensure_dir(failed_dir / "resume_ocr" / f"{resume_id}_{slugify(Path(file_name).stem)}")
    path = out_dir / f"{stage}.txt"
    path.write_text(str(payload), encoding="utf-8")
    return path


def save_run_summary(output_dir: Path, payload: dict, run_session_id: str) -> Path:
    summaries_dir = ensure_dir(output_dir / "summaries")
    versioned_path = summaries_dir / f"resume_ocr_{run_session_id}_summary.json"
    latest_path = summaries_dir / "resume_ocr_latest_summary.json"
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    versioned_path.write_text(text, encoding="utf-8")
    latest_path.write_text(text, encoding="utf-8")
    return versioned_path


def load_processed_hashes(output_dir: Path) -> set[str]:
    hashes: set[str] = set()
    resumes_dir = output_dir / "resumes"
    if not resumes_dir.exists():
        return hashes
    for profile_path in resumes_dir.rglob("*_profile.json"):
        try:
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            if data.get("sha256"):
                hashes.add(str(data["sha256"]))
        except Exception:
            continue
    return hashes

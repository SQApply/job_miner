from __future__ import annotations

import json
from pathlib import Path

from ..schemas import JobPosting
from ..utils import ensure_dir


def save_jobs_json(output_dir: Path, output_file: str, jobs: list[JobPosting], run_session_id: str) -> Path:
    ensure_dir(output_dir)

    stem = Path(output_file).stem
    suffix = Path(output_file).suffix or ".json"

    versioned_path = output_dir / f"{stem}_{run_session_id}{suffix}"
    versioned_path.write_text(
        json.dumps([job.model_dump() for job in jobs], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    latest_path = output_dir / output_file
    latest_path.write_text(
        json.dumps([job.model_dump() for job in jobs], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return versioned_path


def save_run_summary(
    output_dir: Path,
    *,
    target_id: str,
    run_session_id: str,
    discovered_urls: int,
    extracted_jobs: int,
    output_path: str,
    log_path: str,
    total_elapsed_seconds: float,
) -> Path:
    summaries_dir = ensure_dir(output_dir / "summaries")

    payload = {
        "target_id": target_id,
        "run_session_id": run_session_id,
        "discovered_urls": discovered_urls,
        "extracted_jobs": extracted_jobs,
        "output_path": output_path,
        "log_path": log_path,
        "total_elapsed_seconds": total_elapsed_seconds,
    }

    versioned_summary_path = summaries_dir / f"{target_id}_{run_session_id}_summary.json"
    versioned_summary_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    latest_summary_path = summaries_dir / f"{target_id}_latest_summary.json"
    latest_summary_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return versioned_summary_path
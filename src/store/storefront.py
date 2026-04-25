from __future__ import annotations

import json
from pathlib import Path
import csv

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

def save_jobs_csv(output_dir: str, output_file: str, jobs, run_session_id: str):
    base = Path(output_dir)
    ensure_dir(base)

    stem = Path(output_file).stem
    versioned_path = base / f"{stem}_{run_session_id}.csv"
    latest_path = base / f"{stem}.csv"

    rows = [job.model_dump() for job in jobs]

    fieldnames = [
        "title",
        "company",
        "location_text",
        "employment_type",
        "salary_text",
        "posted_date",
        "description",
        "apply_url",
        "job_url",
    ]

    for path in (versioned_path, latest_path):
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    return versioned_path


def save_jobs_markdown(output_dir: str, output_file: str, jobs, run_session_id: str):
    base = Path(output_dir)
    ensure_dir(base)

    stem = Path(output_file).stem
    versioned_path = base / f"{stem}_{run_session_id}.md"
    latest_path = base / f"{stem}.md"

    lines = [f"# {stem} scraped jobs", ""]

    for index, job in enumerate(jobs, start=1):
        data = job.model_dump()

        lines.extend(
            [
                f"## {index}. {data.get('title') or 'Untitled'}",
                "",
                f"- Company: {data.get('company') or ''}",
                f"- Location: {data.get('location_text') or ''}",
                f"- Employment Type: {data.get('employment_type') or ''}",
                f"- Salary: {data.get('salary_text') or ''}",
                f"- Posted Date: {data.get('posted_date') or ''}",
                f"- Apply URL: {data.get('apply_url') or ''}",
                f"- Job URL: {data.get('job_url') or ''}",
                "",
                "### Description",
                "",
                data.get("description") or "",
                "",
                "---",
                "",
            ]
        )

    content = "\n".join(lines)

    for path in (versioned_path, latest_path):
        path.write_text(content, encoding="utf-8")

    return versioned_path
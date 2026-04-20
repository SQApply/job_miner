from __future__ import annotations

from ..schemas import JobPosting


def is_valid_job(job: JobPosting | None) -> bool:
    return job is not None and bool(job.title) and bool(job.job_url)
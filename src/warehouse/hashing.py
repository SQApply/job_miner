from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def compact_text(value: str | None) -> str:
    return " ".join((value or "").replace("\x00", " ").split())


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_hash(value: Any) -> str:
    return sha256_text(canonical_json(value))


def normalize_url(url: str | None) -> str:
    return compact_text(url).rstrip("/")


def make_job_identity_key(job: dict[str, Any], *, target_id: str) -> str:
    url = normalize_url(job.get("job_url") or job.get("source_url") or job.get("url") or job.get("apply_url"))
    if url:
        return f"{target_id}|url|{url}"
    return "|".join([
        target_id,
        "fallback",
        compact_text(job.get("title")),
        compact_text(job.get("company")),
        compact_text(job.get("location_text") or job.get("location")),
        compact_text(job.get("job_reference") or job.get("id")),
    ])


def make_job_id(job: dict[str, Any], *, target_id: str) -> str:
    return "job_" + sha256_text(make_job_identity_key(job, target_id=target_id))[:24]


def make_resume_id_from_sha(sha256: str) -> str:
    return "res_" + sha256[:24]


def make_candidate_id(sha256: str) -> str:
    return "cand_" + sha256_text(sha256)[:24]

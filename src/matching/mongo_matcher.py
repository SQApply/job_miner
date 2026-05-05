from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..warehouse.documents import CandidateJobMatchDocument
from ..warehouse.repositories import WarehouseRepository
from .embeddings import OllamaEmbedder
from .qdrant_store import QdrantVectorStore

SKILL_TERMS = {"python", "java", "scala", "sql", "spark", "pyspark", "databricks", "snowflake", "redshift", "aws", "azure", "gcp", "airflow", "kafka", "hadoop", "hive", "glue", "emr", "s3", "lambda", "terraform", "docker", "kubernetes", "fastapi", "pytorch", "tensorflow", "scikit-learn", "xgboost", "lightgbm", "mlflow", "langchain", "rag", "llm", "nlp", "opencv", "mongodb", "postgresql", "mysql", "oracle", "qdrant", "pinecone", "faiss"}
SYN = {"pyspark": "spark", "apache spark": "spark", "amazon web services": "aws", "machine learning": "ml", "retrieval augmented generation": "rag"}


def norm(s: str) -> str:
    s = re.sub(r"\s+", " ", s.lower().strip().strip(".,:;()[]{}")); return SYN.get(s, s)


def as_list(v: Any) -> list[str]:
    if v is None: return []
    if isinstance(v, list): return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str): return [x.strip() for x in re.split(r"[,;|]\s*", v) if x.strip()]
    return [str(v).strip()] if str(v).strip() else []


def terms_from_text(text: str) -> set[str]:
    low = " " + re.sub(r"[^a-zA-Z0-9+#./-]+", " ", text.lower()) + " "; found = set()
    for t in SKILL_TERMS:
        if re.search(r"(?<![a-zA-Z0-9+#.-])" + re.escape(t) + r"(?![a-zA-Z0-9+#.-])", low): found.add(norm(t))
    return found


def skills(record: dict[str, Any]) -> set[str]:
    out = set()
    for k in ("primary_skills", "secondary_skills", "domains", "required_skills", "preferred_skills"):
        out.update(norm(x) for x in as_list(record.get(k)))
    out.update(terms_from_text(json.dumps(record, ensure_ascii=False, default=str)))
    return {x for x in out if x}


def location_score(c: str | None, j: str | None) -> float:
    if not c or not j: return 0.0
    c = c.lower(); j = j.lower()
    if c in j or j in c: return 1.0
    ct = {x for x in re.split(r"[^a-zA-Z]+", c) if len(x) >= 3}; jt = {x for x in re.split(r"[^a-zA-Z]+", j) if len(x) >= 3}
    return min(1.0, len(ct & jt) / max(1, min(len(ct), len(jt)))) if ct and jt else 0.0


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows: f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def match_candidates_from_mongo(*, repo: WarehouseRepository, store: QdrantVectorStore, embedder: OllamaEmbedder, jobs_collection: str, top_n: int, output_dir: Path | None = None, limit: int | None = None) -> dict[str, Any]:
    candidates = repo.candidate_towers(limit=limit); run_id = "matching_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    grouped = []; flat = []; docs = []
    for cand in candidates:
        text = str(cand.get("candidate_embedding_text") or "")
        if not text.strip(): continue
        results = store.search(jobs_collection, query_vector=embedder.embed_one(text), top_n=top_n)
        cskills = skills(cand); matches = []
        for rank, r in enumerate(results, 1):
            payload = r.payload; job_id = str(payload.get("job_id") or ""); job = repo.get_job(job_id) or payload
            jskills = skills(job); matched = sorted(cskills & jskills); loc = location_score(cand.get("location"), job.get("location_text"))
            skill_score = len(matched) / max(1, len(jskills)) if jskills else 0.0
            score = round(0.82 * r.score + 0.13 * min(1.0, skill_score) + 0.05 * loc, 6)
            row = {"match_run_id": run_id, "candidate_id": cand.get("candidate_id"), "resume_id": cand.get("resume_id"), "candidate_name": cand.get("full_name"), "rank": rank, "job_id": job_id, "title": job.get("title"), "company": job.get("company"), "location_text": job.get("location_text"), "job_url": job.get("job_url"), "apply_url": job.get("apply_url"), "score": score, "vector_score": round(r.score, 6), "evidence": {"matched_skills": matched, "candidate_location": cand.get("location"), "job_location": job.get("location_text"), "location_match_score": round(loc, 4), "skill_overlap_score": round(skill_score, 4)}}
            matches.append(row); flat.append(row); docs.append(CandidateJobMatchDocument(_id=f"{run_id}:{cand.get('candidate_id')}:{job_id}", **row))
        grouped.append({"candidate_id": cand.get("candidate_id"), "resume_id": cand.get("resume_id"), "candidate_name": cand.get("full_name"), "top_n": top_n, "matches": matches})
    repo.upsert_matches(docs)
    vals = [x["score"] for x in flat]
    summary = {"match_run_id": run_id, "candidate_count": len(candidates), "matched_candidate_count": len(grouped), "total_match_count": len(flat), "top_n": top_n, "embedding_model": embedder.model, "jobs_collection": jobs_collection, "average_score": round(sum(vals) / len(vals), 6) if vals else 0.0, "max_score": round(max(vals), 6) if vals else 0.0, "top_matched_titles": Counter(x.get("title") or "UNKNOWN" for x in flat).most_common(10)}
    if output_dir:
        write_json(output_dir / "candidate_job_matches_latest.json", grouped); write_jsonl(output_dir / "candidate_job_matches_latest.jsonl", flat); write_json(output_dir / "candidate_job_matches_latest_summary.json", summary)
    return summary

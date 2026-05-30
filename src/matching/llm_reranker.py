from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json(text: str) -> Any:
    text = _strip_code_fences(text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"(\{.*\})", text, flags=re.DOTALL)

    if not match:
        return None

    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    if isinstance(value, str):
        parts = re.split(r"[,;|]\s*", value)
        return [part.strip() for part in parts if part.strip()]

    return [str(value).strip()] if str(value).strip() else []


def _compact(value: Any, *, max_chars: int = 1500) -> str:
    if value is None:
        return ""

    if isinstance(value, list):
        text = "; ".join(_compact(item, max_chars=max_chars) for item in value)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)

    text = " ".join(text.split())

    if len(text) <= max_chars:
        return text

    return text[:max_chars].rstrip() + "..."


def _chunked(items: list[dict[str, Any]], chunk_size: int) -> list[list[dict[str, Any]]]:
    safe_chunk_size = max(1, int(chunk_size or 1))
    return [items[i : i + safe_chunk_size] for i in range(0, len(items), safe_chunk_size)]


@dataclass(slots=True)
class LLMRerankResult:
    job_id: str
    llm_match_score: int
    decision: str
    matched_skills: list[str]
    missing_skills: list[str]
    risk_flags: list[str]
    reason: str


@dataclass(slots=True)
class LLMJobReranker:
    model: str = "qwen2.5:3b"
    base_url: str = "http://localhost:11434"
    timeout_seconds: int = 600
    temperature: float = 0.0
    num_ctx: int = 8192
    num_predict: int = 4096
    chunk_size: int = 3
    retry_missing_jobs: bool = True
    max_missing_retries: int = 2

    def rerank(
        self,
        *,
        candidate: dict[str, Any],
        jobs: list[dict[str, Any]],
    ) -> list[LLMRerankResult]:
        """Score candidate-job fit with an LLM.

        This method is strict about job coverage:
        - It accepts only job_ids that were present in the input batch.
        - It retries any job_ids missing from the LLM JSON response.
        - It does not create synthetic/fallback LLM scores. The orchestration layer can
          decide whether to drop unscored jobs or show diagnostics.
        """
        if not jobs:
            return []

        results_by_job_id: dict[str, LLMRerankResult] = {}
        jobs_by_id = {str(job.get("job_id") or "").strip(): job for job in jobs if str(job.get("job_id") or "").strip()}

        for chunk in _chunked(list(jobs_by_id.values()), self.chunk_size):
            chunk_results = self._rerank_chunk(candidate=candidate, jobs=chunk)
            valid_ids = {str(job.get("job_id") or "").strip() for job in chunk}
            for item in chunk_results:
                if item.job_id in valid_ids:
                    results_by_job_id[item.job_id] = item

            if not self.retry_missing_jobs:
                continue

            missing_ids = [job_id for job_id in valid_ids if job_id not in results_by_job_id]
            if missing_ids:
                logger.warning(
                    "LLM skipped job_ids in chunk. Retrying missing jobs count=%s ids=%s",
                    len(missing_ids),
                    missing_ids[:10],
                )
                self._retry_missing(candidate=candidate, jobs_by_id=jobs_by_id, missing_ids=missing_ids, results_by_job_id=results_by_job_id)

        missing_final = [job_id for job_id in jobs_by_id if job_id not in results_by_job_id]
        if missing_final:
            logger.warning(
                "LLM still did not score some jobs after retries. Dropping from optimized output count=%s ids=%s",
                len(missing_final),
                missing_final[:20],
            )

        return sorted(
            results_by_job_id.values(),
            key=lambda item: item.llm_match_score,
            reverse=True,
        )

    def _retry_missing(
        self,
        *,
        candidate: dict[str, Any],
        jobs_by_id: dict[str, dict[str, Any]],
        missing_ids: list[str],
        results_by_job_id: dict[str, LLMRerankResult],
    ) -> None:
        remaining = list(missing_ids)
        for attempt in range(1, max(1, self.max_missing_retries) + 1):
            if not remaining:
                return
            logger.info("Retrying missing LLM job scores attempt=%s remaining=%s", attempt, len(remaining))
            next_remaining: list[str] = []
            # Retry one job at a time. This is slower but much more reliable for local small LLMs.
            for job_id in remaining:
                if job_id in results_by_job_id:
                    continue
                job = jobs_by_id.get(job_id)
                if not job:
                    continue
                retry_results = self._rerank_chunk(candidate=candidate, jobs=[job])
                match = next((item for item in retry_results if item.job_id == job_id), None)
                if match:
                    results_by_job_id[job_id] = match
                else:
                    next_remaining.append(job_id)
            remaining = next_remaining

    def _rerank_chunk(
        self,
        *,
        candidate: dict[str, Any],
        jobs: list[dict[str, Any]],
    ) -> list[LLMRerankResult]:
        if not jobs:
            return []

        expected_ids = {str(job.get("job_id") or "").strip() for job in jobs if str(job.get("job_id") or "").strip()}
        prompt = self._build_prompt(candidate=candidate, jobs=jobs)
        response_text = self._call_ollama(prompt)
        payload = _extract_json(response_text)

        if not isinstance(payload, dict):
            logger.warning("LLM response was not valid JSON object. expected_job_ids=%s", sorted(expected_ids))
            return []

        results = payload.get("results")
        if not isinstance(results, list):
            logger.warning("LLM JSON did not contain results list. expected_job_ids=%s", sorted(expected_ids))
            return []

        parsed: list[LLMRerankResult] = []
        seen: set[str] = set()

        for item in results:
            if not isinstance(item, dict):
                continue

            job_id = str(item.get("job_id") or "").strip()
            if not job_id or job_id not in expected_ids or job_id in seen:
                continue
            seen.add(job_id)

            try:
                score = int(float(item.get("llm_match_score", 0)))
            except Exception:
                score = 0

            score = max(0, min(100, score))

            parsed.append(
                LLMRerankResult(
                    job_id=job_id,
                    llm_match_score=score,
                    decision=str(item.get("decision") or "unknown"),
                    matched_skills=_as_list(item.get("matched_skills")),
                    missing_skills=_as_list(item.get("missing_skills")),
                    risk_flags=_as_list(item.get("risk_flags")),
                    reason=str(item.get("reason") or "").strip(),
                )
            )

        missing_ids = sorted(expected_ids - {item.job_id for item in parsed})
        if missing_ids:
            logger.warning("LLM chunk missing job_ids=%s", missing_ids)

        return parsed

    def _candidate_brief(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "candidate_id": candidate.get("candidate_id"),
            "resume_id": candidate.get("resume_id"),
            "full_name": candidate.get("full_name") or candidate.get("candidate_name"),
            "headline": candidate.get("headline"),
            "current_title": candidate.get("current_title"),
            "current_company": candidate.get("current_company"),
            "total_experience_years": candidate.get("total_experience_years"),
            "skills": _as_list(candidate.get("skills")),
            "domains": _as_list(candidate.get("domains")),
            "location": candidate.get("location"),
            "identity_text": _compact(candidate.get("identity_text"), max_chars=1200),
            "skills_text": _compact(candidate.get("skills_text"), max_chars=1800),
            "experience_text": _compact(candidate.get("experience_text"), max_chars=2500),
            "education_text": _compact(candidate.get("education_text"), max_chars=1000),
        }

    def _job_brief(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "baseline_rank": job.get("baseline_rank"),
            "baseline_score": job.get("baseline_score"),
            "vector_score": job.get("vector_score"),
            "job_id": job.get("job_id"),
            "title": job.get("title"),
            "company": job.get("company"),
            "location_text": job.get("location_text"),
            "employment_type": job.get("employment_type"),
            "compensation_text": job.get("compensation_text"),
            "job_url": job.get("job_url"),
            "required_skills": _as_list(job.get("required_skills")),
            "preferred_skills": _as_list(job.get("preferred_skills")),
            "summary": _compact(job.get("summary"), max_chars=1200),
            "responsibilities": _compact(job.get("responsibilities"), max_chars=1600),
            "raw_payload": _compact(job.get("raw_payload"), max_chars=1000),
        }

    def _build_prompt(
        self,
        *,
        candidate: dict[str, Any],
        jobs: list[dict[str, Any]],
    ) -> str:
        candidate_brief = self._candidate_brief(candidate)
        job_briefs = [self._job_brief(job) for job in jobs]
        job_ids = [brief.get("job_id") for brief in job_briefs]

        return f"""
You are an expert technical recruiter and job-matching reranker.

Task:
Score the provided jobs for the candidate.

Critical output rule:
- Return exactly one result object for every job_id in this list: {json.dumps(job_ids, ensure_ascii=False)}.
- Do not omit any job_id.
- Do not create new job_ids.
- If a job is a poor fit, still return it with a low score and explanation.
- Return only valid JSON.

Important:
- These jobs were already retrieved by vector search.
- Your job is to judge final candidate-job fit.
- Use only the information provided.
- Do not invent candidate experience or job requirements.

Score rubric:
90-100 = excellent match; candidate strongly fits role, skills, and experience.
80-89 = strong match; candidate fits well with minor gaps.
70-79 = good match; relevant but has clear gaps.
60-69 = weak match; some overlap but not ideal.
0-59 = poor match; not recommended.

Evaluate:
1. Role/title alignment.
2. Must-have skills match.
3. Relevant experience.
4. Domain match.
5. Seniority match.
6. Location/remote compatibility if visible.
7. Missing critical skills.
8. Risk flags.

Candidate:
{json.dumps(candidate_brief, indent=2, ensure_ascii=False)}

Jobs:
{json.dumps(job_briefs, indent=2, ensure_ascii=False)}

Return JSON in exactly this shape:
{{
  "results": [
    {{
      "job_id": "job id from input",
      "llm_match_score": 92,
      "decision": "excellent_match | strong_match | good_match | weak_match | poor_match",
      "matched_skills": ["skill1", "skill2"],
      "missing_skills": ["skill1"],
      "risk_flags": ["risk1"],
      "reason": "Short explanation of why this job is or is not a good match."
    }}
  ]
}}
""".strip()

    def _call_ollama(self, prompt: str) -> str:
        url = self.base_url.rstrip("/") + "/api/generate"

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
        }

        response = requests.post(
            url,
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

        data = response.json()
        return data.get("response") or ""

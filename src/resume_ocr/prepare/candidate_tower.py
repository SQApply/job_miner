from __future__ import annotations

import hashlib

from ..schemas import CandidateTowerRecord, ResumeProfile
from ..utils import compact_text
from ...warehouse.skill_utils import build_canonical_candidate_skills


def _join(items: list[str]) -> str:
    return ", ".join(item for item in items if item)


def _end_date(exp) -> str:
    if exp.is_current:
        return "present"
    return exp.end_date or ""


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)

    return result


def _infer_domains_from_profile(profile: ResumeProfile) -> list[str]:
    """Infer recommendation/search domains when the LLM leaves domains empty.

    Domains are search facets, not always explicit resume fields. Keep this
    deterministic fallback in the candidate tower layer so profile extraction can
    still succeed when title, experience, and skills are valid but domains=[] .
    """
    text_parts: list[str] = [
        profile.current_title or "",
        profile.current_company or "",
        profile.headline or "",
        profile.summary or "",
        _join(profile.primary_skills or []),
        _join(profile.secondary_skills or []),
        _join(profile.tools_and_platforms or []),
        _join(profile.programming_languages or []),
        _join(profile.certifications or []),
    ]

    for exp in profile.experience or []:
        text_parts.extend([
            exp.title or "",
            exp.company or "",
            _join(exp.technologies or []),
            " ".join(exp.responsibilities or []),
        ])

    for project in profile.projects or []:
        text_parts.extend([
            project.name or "",
            project.description or "",
            _join(project.technologies or []),
        ])

    text = " ".join(text_parts).lower()

    rules: list[tuple[str, tuple[str, ...]]] = [
        ("Generative AI/RAG", ("rag", "langchain", "llm", "genai", "openai", "prompt engineering", "bedrock")),
        ("Data Science", ("data scientist", "data science", "machine learning", "pytorch", "tensorflow", "scikit", "opencv", "pyspark", "mlflow", "model")),
        ("MLOps", ("mlflow", "dvc", "bentoml", "zenml", "docker", "kubernetes", "github actions", "jenkins", "terraform", "torch-serve")),
        ("Cloud AI", ("aws", "azure", "azure ai", "aws bedrock", "hugging face", "cloud")),
        ("Banking/Financial Services", ("bank", "banking", "sbi", "sbi-card", "bank of baroda", "fraud", "financial")),
        ("Full Stack Web Development", ("react", "next.js", "node", "express", "fastapi", "mongodb", "frontend", "backend")),
        ("Frontend Engineering", ("react", "next.js", "javascript", "typescript", "tailwind", "frontend")),
        ("Backend Engineering", ("fastapi", "node", "express", "api", "mongodb", "mysql", "backend")),
    ]

    domains: list[str] = []
    for domain, keywords in rules:
        if any(keyword in text for keyword in keywords):
            domains.append(domain)

    return _dedupe(domains)


def build_candidate_id(sha256: str) -> str:
    return "cand_" + hashlib.sha256(sha256.encode("utf-8")).hexdigest()[:24]


def build_candidate_tower_record(profile: ResumeProfile) -> CandidateTowerRecord:
    contact = profile.contact
    identity_text = compact_text("\n".join([
        contact.full_name or "",
        profile.headline or "",
        profile.current_title or "",
        profile.current_company or "",
        contact.location or "",
        f"Total experience: {profile.total_experience_years} years" if profile.total_experience_years is not None else "",
    ]))

    profile_payload = profile.model_dump(mode="python")
    skills = build_canonical_candidate_skills(profile_payload)
    domains = profile.domains or _infer_domains_from_profile(profile)
    skills_text = compact_text("\n".join([
        "Skills: " + _join(skills),
        "Domains: " + _join(domains),
        "Certifications: " + _join(profile.certifications),
    ]))

    exp_lines: list[str] = []
    for exp in profile.experience:
        exp_lines.append(compact_text(" | ".join([
            exp.title or "",
            exp.company or "",
            exp.location or "",
            f"{exp.start_date or ''} - {_end_date(exp)}",
            "Technologies: " + _join(exp.technologies),
            "Responsibilities: " + " ".join(exp.responsibilities),
        ])))
    experience_text = compact_text("\n".join(exp_lines))

    edu_lines: list[str] = []
    for edu in profile.education:
        edu_lines.append(compact_text(" | ".join([
            edu.degree or "",
            edu.field_of_study or "",
            edu.institution or "",
            edu.score_or_grade or "",
            f"{edu.start_date or ''} - {edu.end_date or ''}",
        ])))
    education_text = compact_text("\n".join(edu_lines))

    candidate_embedding_text = compact_text("\n\n".join([
        identity_text,
        profile.summary or "",
        skills_text,
        experience_text,
        education_text,
    ]))

    return CandidateTowerRecord(
        candidate_id=build_candidate_id(profile.sha256),
        resume_id=profile.resume_id,
        source_file_name=profile.source_file_name,
        sha256=profile.sha256,
        full_name=contact.full_name,
        email=contact.email,
        phone=contact.phone,
        location=contact.location,
        current_title=profile.current_title,
        current_company=profile.current_company,
        total_experience_years=profile.total_experience_years,
        skills=skills,
        primary_skills=[],
        secondary_skills=[],
        domains=domains,
        identity_text=identity_text,
        skills_text=skills_text,
        experience_text=experience_text,
        education_text=education_text,
        candidate_embedding_text=candidate_embedding_text,
    )

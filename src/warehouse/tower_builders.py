from __future__ import annotations

from typing import Any

from .documents import CandidateTowerDocument, JobTowerDocument
from .hashing import make_candidate_id, stable_hash
from .serializers import as_str_list


def _join(items: list[str]) -> str:
    return ", ".join(item for item in items if item)


def _compact(parts: list[str | None]) -> str:
    return "\n".join(" ".join((part or "").split()) for part in parts if (part or "").strip())


def build_job_tower_document(job: dict[str, Any]) -> JobTowerDocument:
    job_id = str(job.get("job_id") or job.get("_id"))
    required = as_str_list(job.get("required_skills"))
    preferred = as_str_list(job.get("preferred_skills"))
    responsibilities = as_str_list(job.get("responsibilities"))
    title_text = _compact([
        f"Title: {job.get('title')}" if job.get("title") else None,
        f"Company: {job.get('company')}" if job.get("company") else None,
        f"Location: {job.get('location_text')}" if job.get("location_text") else None,
        f"Employment type: {job.get('employment_type')}" if job.get("employment_type") else None,
        f"Duration: {job.get('duration')}" if job.get("duration") else None,
    ])
    req_text = _compact([
        "Required skills: " + _join(required) if required else None,
        "Preferred skills: " + _join(preferred) if preferred else None,
        "Summary: " + str(job.get("summary")) if job.get("summary") else None,
    ])
    resp_text = "Responsibilities: " + " ".join(responsibilities) if responsibilities else ""
    comp_text = "Compensation: " + str(job.get("compensation_text")) if job.get("compensation_text") else ""
    embedding_text = _compact([title_text, req_text, resp_text, comp_text])
    return JobTowerDocument(
        _id="jobtw_" + job_id,
        job_tower_id="jobtw_" + job_id,
        job_id=job_id,
        target_id=str(job.get("target_id") or "unknown"),
        title=job.get("title"),
        company=job.get("company"),
        job_url=job.get("job_url"),
        apply_url=job.get("apply_url"),
        location_text=job.get("location_text"),
        employment_type=job.get("employment_type"),
        duration=job.get("duration"),
        compensation_text=job.get("compensation_text"),
        summary=job.get("summary"),
        required_skills=required,
        preferred_skills=preferred,
        responsibilities=responsibilities,
        title_company_location_text=title_text,
        requirements_text=req_text,
        responsibilities_text=resp_text,
        compensation_embedding_text=comp_text,
        job_embedding_text=embedding_text,
        source_content_hash=str(job.get("content_hash") or stable_hash(job)),
    )


def build_candidate_tower_from_resume_profile(profile: dict[str, Any]) -> CandidateTowerDocument:
    contact = profile.get("contact") or {}
    sha256 = str(profile.get("sha256") or "")
    candidate_id = make_candidate_id(sha256)
    identity_text = _compact([
        contact.get("full_name"), profile.get("headline"), profile.get("current_title"),
        profile.get("current_company"), contact.get("location"),
        f"Total experience: {profile.get('total_experience_years')} years" if profile.get("total_experience_years") is not None else None,
    ])
    primary = as_str_list(profile.get("primary_skills"))
    secondary = as_str_list(profile.get("secondary_skills"))
    tools = as_str_list(profile.get("tools_and_platforms"))
    langs = as_str_list(profile.get("programming_languages"))
    domains = as_str_list(profile.get("domains"))
    certs = as_str_list(profile.get("certifications"))
    skills_text = _compact([
        "Primary skills: " + _join(primary) if primary else None,
        "Secondary skills: " + _join(secondary) if secondary else None,
        "Programming languages: " + _join(langs) if langs else None,
        "Tools and platforms: " + _join(tools) if tools else None,
        "Domains: " + _join(domains) if domains else None,
        "Certifications: " + _join(certs) if certs else None,
    ])
    exp_lines = []
    for exp in profile.get("experience") or []:
        if isinstance(exp, dict):
            exp_lines.append(_compact([exp.get("title"), exp.get("company"), "Responsibilities: " + " ".join(as_str_list(exp.get("responsibilities"))) if exp.get("responsibilities") else None]))
    edu_lines = []
    for edu in profile.get("education") or []:
        if isinstance(edu, dict):
            edu_lines.append(_compact([edu.get("degree"), edu.get("field_of_study"), edu.get("institution")]))
    experience_text = _compact(exp_lines)
    education_text = _compact(edu_lines)
    embedding_text = _compact([identity_text, profile.get("summary"), skills_text, experience_text, education_text])
    return CandidateTowerDocument(
        _id=candidate_id,
        candidate_id=candidate_id,
        resume_id=str(profile.get("resume_id") or ""),
        source_file_name=str(profile.get("source_file_name") or ""),
        sha256=sha256,
        full_name=contact.get("full_name"),
        email=contact.get("email"),
        phone=contact.get("phone"),
        location=contact.get("location"),
        current_title=profile.get("current_title"),
        current_company=profile.get("current_company"),
        total_experience_years=profile.get("total_experience_years"),
        primary_skills=primary,
        secondary_skills=secondary,
        domains=domains,
        identity_text=identity_text,
        skills_text=skills_text,
        experience_text=experience_text,
        education_text=education_text,
        candidate_embedding_text=embedding_text,
        source_content_hash=str(profile.get("content_hash") or stable_hash(profile)),
    )

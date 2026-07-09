from __future__ import annotations

import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _first(*values: Any) -> str | None:
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return None


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in re.split(r"[,;|]", value) if part.strip()]
    return []


def candidate_contact(candidate_profile: dict[str, Any] | None, resume_profile: dict[str, Any] | None) -> dict[str, str | None]:
    candidate_profile = candidate_profile or {}
    resume_profile = resume_profile or {}
    effective = candidate_profile.get("effective_profile") or {}
    contact = resume_profile.get("contact") or {}
    full_name = _first(effective.get("full_name"), candidate_profile.get("full_name"), contact.get("full_name"), resume_profile.get("full_name"))
    email = _first(effective.get("email"), candidate_profile.get("email"), contact.get("email"), resume_profile.get("email"))
    phone = _first(effective.get("phone"), candidate_profile.get("phone"), contact.get("phone"), resume_profile.get("phone"))
    location = _first(effective.get("location"), candidate_profile.get("location"), contact.get("location"), resume_profile.get("location"))
    first_name = None
    last_name = None
    if full_name:
        parts = [part for part in full_name.split() if part]
        if parts:
            first_name = parts[0]
            last_name = " ".join(parts[1:]) or parts[0]
    return {
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "phone": phone,
        "location": location,
    }


def resolve_resume_file(
    *,
    candidate_id: str,
    candidate_profile: dict[str, Any] | None,
    resume_profile: dict[str, Any] | None,
) -> Path:
    """Return an uploadable resume file.

    Prefer the original candidate-uploaded file when available. For local demos
    where the source file path is missing, create a small TXT resume from the
    parsed profile because the StrategicStaff form accepts txt/doc/docx/pdf.
    """
    for source in (candidate_profile or {}, resume_profile or {}):
        for key in ("active_resume_local_path", "local_path", "source_path", "resume_local_path"):
            raw = _clean(source.get(key))
            if raw:
                path = Path(raw)
                if path.exists() and path.is_file():
                    return path

    generated_dir = PROJECT_ROOT / "data" / "resumes" / "application_agent" / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    target = generated_dir / f"{candidate_id}_application_profile.txt"
    target.write_text(_build_resume_text(candidate_profile or {}, resume_profile or {}), encoding="utf-8")
    return target


def _build_resume_text(candidate_profile: dict[str, Any], resume_profile: dict[str, Any]) -> str:
    contact = candidate_contact(candidate_profile, resume_profile)
    effective = candidate_profile.get("effective_profile") or {}
    skills = _as_list(effective.get("skills")) or _as_list(candidate_profile.get("skills")) or _as_list(candidate_profile.get("primary_skills")) or _as_list(resume_profile.get("primary_skills"))
    domains = _as_list(effective.get("domains")) or _as_list(candidate_profile.get("domains")) or _as_list(resume_profile.get("domains"))
    summary = _first(effective.get("summary"), resume_profile.get("summary"), candidate_profile.get("summary"))
    title = _first(effective.get("current_title"), candidate_profile.get("current_title"), resume_profile.get("current_title"), resume_profile.get("headline"))
    company = _first(effective.get("current_company"), candidate_profile.get("current_company"), resume_profile.get("current_company"))
    experience_years = _first(effective.get("total_experience_years"), candidate_profile.get("total_experience_years"), resume_profile.get("total_experience_years"))

    lines = [
        contact.get("full_name") or "Candidate",
        f"Email: {contact.get('email') or 'Not available'}",
    ]
    if contact.get("phone"):
        lines.append(f"Phone: {contact['phone']}")
    if contact.get("location"):
        lines.append(f"Location: {contact['location']}")
    if title:
        lines.extend(["", "Current Role", title + (f" at {company}" if company else "")])
    if experience_years:
        lines.append(f"Experience: {experience_years} years")
    if summary:
        lines.extend(["", "Summary", summary])
    if skills:
        lines.extend(["", "Skills", ", ".join(skills)])
    if domains:
        lines.extend(["", "Domains", ", ".join(domains)])

    experience = resume_profile.get("experience") or []
    if isinstance(experience, list) and experience:
        lines.extend(["", "Experience Details"])
        for item in experience[:5]:
            if not isinstance(item, dict):
                continue
            role = _first(item.get("title"), item.get("role"), item.get("position"))
            org = _first(item.get("company"), item.get("organization"), item.get("employer"))
            desc = _first(item.get("description"), item.get("summary"))
            if role or org:
                lines.append(f"- {role or 'Role'}" + (f" at {org}" if org else ""))
            if desc:
                lines.append(f"  {desc[:600]}")
    return "\n".join(lines).strip() + "\n"

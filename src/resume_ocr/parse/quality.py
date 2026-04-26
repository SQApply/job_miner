from __future__ import annotations

import re


def estimate_ocr_quality(markdown: str) -> dict[str, float | int | bool]:
    text = markdown or ""
    total_chars = len(text)
    alnum_chars = sum(1 for ch in text if ch.isalnum())
    replacement_chars = text.count("�")
    email_hits = len(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text))
    phone_hits = len(re.findall(r"(?:\+?\d[\d\s().-]{7,}\d)", text))
    section_hits = len(re.findall(r"\b(experience|education|skills|projects|summary|certifications)\b", text, flags=re.I))
    alnum_ratio = alnum_chars / total_chars if total_chars else 0.0
    return {
        "total_chars": total_chars,
        "alnum_ratio": round(alnum_ratio, 4),
        "replacement_chars": replacement_chars,
        "email_hits": email_hits,
        "phone_hits": phone_hits,
        "section_hits": section_hits,
        "probably_good": total_chars >= 200 and alnum_ratio >= 0.45 and section_hits >= 1,
    }

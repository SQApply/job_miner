from __future__ import annotations

import html as html_module
import json
import re
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urljoin

from ..schemas import JobPosting


class _JsonLdParser(HTMLParser):
    """Collect JSON-LD script bodies without adding an HTML dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capturing = False
        self._chunks: list[str] = []
        self.documents: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = {key.lower(): (value or "") for key, value in attrs}
        content_type = attributes.get("type", "").lower().split(";", 1)[0].strip()
        if content_type == "application/ld+json":
            self._capturing = True
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing:
            self.documents.append("".join(self._chunks).strip())
            self._capturing = False
            self._chunks = []


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.parts.append(value)


def _plain_text(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parser = _TextParser()
    try:
        parser.feed(html_module.unescape(raw))
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html_module.unescape(raw))
    normalized = " ".join(text.split())
    return normalized or None


def _schema_types(value: Any) -> set[str]:
    values = value if isinstance(value, list) else [value]
    return {
        str(item).strip().rstrip("/").rsplit("/", 1)[-1].lower()
        for item in values
        if str(item or "").strip()
    }


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _organization_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return _plain_text(value.get("name") or value.get("legalName"))
    return _plain_text(value)


def _country_text(value: Any) -> str | None:
    if isinstance(value, dict):
        return _plain_text(value.get("name") or value.get("@id"))
    return _plain_text(value)


def _address_text(address: Any) -> str | None:
    if not isinstance(address, dict):
        return _plain_text(address)
    parts = [
        _plain_text(address.get("streetAddress")),
        _plain_text(address.get("addressLocality")),
        _plain_text(address.get("addressRegion")),
        _plain_text(address.get("postalCode")),
        _country_text(address.get("addressCountry")),
    ]
    return ", ".join(dict.fromkeys(part for part in parts if part)) or None


def _location_text(payload: dict[str, Any]) -> str | None:
    values = payload.get("jobLocation")
    locations = values if isinstance(values, list) else [values]
    rendered: list[str] = []
    for location in locations:
        if isinstance(location, dict):
            value = _address_text(location.get("address")) or _plain_text(location.get("name"))
        else:
            value = _plain_text(location)
        if value:
            rendered.append(value)

    location_type = str(payload.get("jobLocationType") or "").lower()
    if "telecommute" in location_type or "remote" in location_type:
        rendered.append("Remote")
    return " | ".join(dict.fromkeys(rendered)) or None


def _identifier_text(value: Any) -> str | None:
    if isinstance(value, dict):
        return _plain_text(value.get("value") or value.get("name") or value.get("@id"))
    return _plain_text(value)


def _salary_text(value: Any) -> str | None:
    if not isinstance(value, dict):
        return _plain_text(value)

    currency = _plain_text(value.get("currency"))
    salary_value = value.get("value")
    if isinstance(salary_value, dict):
        minimum = salary_value.get("minValue")
        maximum = salary_value.get("maxValue")
        exact = salary_value.get("value")
        unit = _plain_text(salary_value.get("unitText"))
        if minimum is not None and maximum is not None:
            amount = f"{minimum}-{maximum}"
        elif exact is not None:
            amount = str(exact)
        else:
            amount = str(minimum if minimum is not None else maximum or "").strip()
        parts = [currency, amount or None, f"per {unit.lower()}" if unit else None]
        return " ".join(part for part in parts if part) or None
    return _plain_text(salary_value)


def _job_from_schema(payload: dict[str, Any], fallback_url: str) -> JobPosting | None:
    title = _plain_text(payload.get("title") or payload.get("name"))
    if not title:
        return None

    description = _plain_text(payload.get("description"))
    source_url = _plain_text(payload.get("url") or payload.get("sameAs"))
    url = urljoin(fallback_url, source_url) if source_url else fallback_url
    employment_type = payload.get("employmentType")
    if isinstance(employment_type, list):
        employment_type = ", ".join(str(item) for item in employment_type if item)

    try:
        return JobPosting(
            title=title,
            job_url=url,
            apply_url=url,
            company=_organization_name(payload.get("hiringOrganization")),
            location_text=_location_text(payload),
            employment_type=_plain_text(employment_type),
            compensation_text=_salary_text(payload.get("baseSalary") or payload.get("estimatedSalary")),
            posted_date=_plain_text(payload.get("datePosted")),
            summary=description,
            job_reference=_identifier_text(payload.get("identifier")),
        )
    except Exception:
        return None


def extract_job_from_html(html: str | None, fallback_url: str) -> JobPosting | None:
    """Extract a Schema.org JobPosting without invoking an LLM."""
    if not html or not str(html).strip():
        return None

    parser = _JsonLdParser()
    try:
        parser.feed(str(html))
    except Exception:
        return None

    candidates: list[JobPosting] = []
    for document in parser.documents:
        try:
            decoded = json.loads(document)
        except (TypeError, json.JSONDecodeError):
            continue
        for payload in _walk(decoded):
            if "jobposting" not in _schema_types(payload.get("@type")):
                continue
            job = _job_from_schema(payload, fallback_url)
            if job:
                candidates.append(job)

    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: sum(
            bool(value)
            for value in (
                item.title,
                item.company,
                item.location_text,
                item.employment_type,
                item.compensation_text,
                item.posted_date,
                item.summary,
                item.job_reference,
            )
        ),
    )


def extract_job_from_result(result: Any, fallback_url: str) -> JobPosting | None:
    for field in ("html", "cleaned_html"):
        job = extract_job_from_html(getattr(result, field, None), fallback_url)
        if job:
            return job
    return None

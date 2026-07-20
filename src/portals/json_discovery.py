from __future__ import annotations

import hashlib
import html as html_module
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

from ..crawl.browser_evidence import BrowserEvidenceReport
from ..schemas import JobPosting
from .contracts import (
    CompletenessState,
    DiscoveryBatch,
    DiscoveryCandidate,
    DiscoveryCandidateKind,
    ScrapeStrategy,
)


JSON_DISCOVERY_CONTRACT_VERSION = "1.0"
_KEY_CLEANER = re.compile(r"[^a-z0-9]+")
_JOB_CONTEXT_KEY = re.compile(
    r"(?:^|[^a-z])(job|jobs|jobposting|jobpostings|opening|openings|position|"
    r"positions|requisition|requisitions|vacancy|vacancies|career|careers)(?:[^a-z]|$)",
    re.I,
)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_GENERIC_TITLES = {
    "about us",
    "careers",
    "contact us",
    "job search",
    "jobs",
    "open positions",
    "opportunities",
    "resources",
    "search jobs",
}

_STRONG_TITLE_KEYS = (
    "jobtitle",
    "positiontitle",
    "postingtitle",
    "requisitiontitle",
    "vacancytitle",
    "roletitle",
)
_TITLE_KEYS = (*_STRONG_TITLE_KEYS, "title", "name")
_STRONG_URL_KEYS = (
    "joburl",
    "jobdetailurl",
    "detailurl",
    "postingurl",
    "positionurl",
    "requisitionurl",
    "canonicalurl",
    "externalurl",
    "externalpath",
    "detailpath",
    "detailuri",
    "joburi",
    "postinguri",
    "positionuri",
    "jobpath",
)
_URL_KEYS = (*_STRONG_URL_KEYS, "url", "href", "uri", "path")
_APPLY_URL_KEYS = ("applyurl", "applicationurl", "applyuri", "applylink")
_ID_KEYS = (
    "jobid",
    "jobnumber",
    "postingid",
    "positionid",
    "requisitionid",
    "requisitionnumber",
    "reqid",
    "vacancyid",
    "reference",
    "referencenumber",
    "identifier",
    "id",
)
_LOCATION_KEYS = (
    "locationtext",
    "locationsText",
    "joblocation",
    "locationname",
    "addresslocality",
    "addressregion",
    "locations",
    "location",
    "city",
)
_SUMMARY_KEYS = (
    "jobdescription",
    "description",
    "summary",
    "shortdescription",
    "teaser",
)
_COMPANY_KEYS = ("companyname", "hiringorganization", "organization", "company")
_EMPLOYMENT_KEYS = ("employmenttype", "jobtype", "worktype", "timeType")
_POSTED_KEYS = ("dateposted", "posteddate", "postedon", "publicationdate")


def _normalized_key(value: Any) -> str:
    return _KEY_CLEANER.sub("", str(value or "").lower())


def _bounded_text(value: Any, *, limit: int = 20_000) -> str | None:
    if value is None or isinstance(value, (bool, int, float)):
        rendered = "" if value is None else str(value)
    elif isinstance(value, str):
        rendered = value
    elif isinstance(value, dict):
        preferred = (
            "name",
            "displayName",
            "formattedAddress",
            "addressLocality",
            "addressRegion",
            "city",
            "state",
            "value",
            "label",
        )
        parts = [_bounded_text(value.get(key), limit=limit) for key in preferred if key in value]
        rendered = ", ".join(dict.fromkeys(part for part in parts if part))
    elif isinstance(value, (list, tuple)):
        parts = [_bounded_text(item, limit=limit) for item in list(value)[:20]]
        rendered = ", ".join(dict.fromkeys(part for part in parts if part))
    else:
        rendered = str(value)
    rendered = html_module.unescape(_TAG_PATTERN.sub(" ", rendered))
    normalized = " ".join(rendered.split())
    return normalized[:limit] or None


def _http_url(value: Any, *, base_url: str) -> str | None:
    rendered = _bounded_text(value, limit=4_096)
    if not rendered or rendered.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    candidate = urljoin(base_url, rendered)
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    return candidate[:4_096]


@dataclass(frozen=True)
class JsonDiscoveryOptions:
    max_depth: int = 10
    max_values_scanned: int = 30_000
    max_candidates: int = 1_000
    minimum_confidence: float = 0.76
    evidence_preservation_confidence: float = 0.82

    def __post_init__(self) -> None:
        if not 2 <= self.max_depth <= 30:
            raise ValueError("max_depth must be between 2 and 30")
        if not 100 <= self.max_values_scanned <= 200_000:
            raise ValueError("max_values_scanned must be between 100 and 200000")
        if not 1 <= self.max_candidates <= 10_000:
            raise ValueError("max_candidates must be between 1 and 10000")
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be between 0 and 1")
        if not self.minimum_confidence <= self.evidence_preservation_confidence <= 1.0:
            raise ValueError(
                "evidence_preservation_confidence must be at least minimum_confidence"
            )


@dataclass(frozen=True)
class _Field:
    key: str
    normalized_key: str
    value: Any


class JsonCandidateDiscoverer:
    """Discover grounded jobs in captured public XHR, GraphQL, and inline JSON.

    The walker does not know a vendor schema.  It recursively inspects bounded
    records and requires a title, an openable HTTP URL, and independent job
    semantics. Generic ``name``/``url`` marketing cards are intentionally
    rejected. Raw payloads are never copied into candidate evidence.
    """

    def __init__(self, options: JsonDiscoveryOptions | None = None) -> None:
        self.options = options or JsonDiscoveryOptions()

    def discover(self, report: BrowserEvidenceReport) -> DiscoveryBatch:
        candidates: list[DiscoveryCandidate] = []
        metrics: dict[str, Any] = {
            "contract_version": JSON_DISCOVERY_CONTRACT_VERSION,
            "network_documents": 0,
            "inline_documents": 0,
            "values_scanned": 0,
            "objects_scanned": 0,
            "arrays_scanned": 0,
            "records_rejected": 0,
            "candidate_records": 0,
            "bounded": False,
        }

        documents: list[tuple[str, str, str, Any]] = []
        for response in report.network_json:
            if response.payload is None or response.truncated or response.parse_error:
                continue
            metrics["network_documents"] += 1
            documents.append(
                ("network_json_record", response.url, response.response_id, response.payload)
            )
        for frame in report.frames:
            for inline in frame.inline_json:
                if inline.payload is None or inline.truncated or inline.parse_error:
                    continue
                metrics["inline_documents"] += 1
                document_id = inline.script_id or inline.sample_sha256[:16]
                documents.append(
                    ("inline_json_record", frame.frame_url, document_id, inline.payload)
                )

        remaining = self.options.max_values_scanned
        for origin, base_url, document_id, payload in documents:
            if remaining <= 0 or len(candidates) >= self.options.max_candidates:
                metrics["bounded"] = True
                break
            found, scanned = self._walk_document(
                payload,
                origin=origin,
                base_url=base_url,
                document_id=document_id,
                budget=remaining,
                candidate_budget=self.options.max_candidates - len(candidates),
                metrics=metrics,
            )
            candidates.extend(found)
            remaining -= scanned

        deduplicated = self._deduplicate(candidates)
        selected = deduplicated[: self.options.max_candidates]
        if len(selected) < len(deduplicated):
            metrics["bounded"] = True
        metrics.update(
            {
                "candidate_records": len(candidates),
                "candidates": len(selected),
                "preextracted_jobs": sum(
                    candidate.preextracted_job is not None for candidate in selected
                ),
                "network_candidates": sum(
                    candidate.evidence.get("origin") == "network_json_record"
                    for candidate in selected
                ),
                "inline_candidates": sum(
                    candidate.evidence.get("origin") == "inline_json_record"
                    for candidate in selected
                ),
            }
        )
        strategy = (
            ScrapeStrategy.NETWORK_JSON
            if any(
                candidate.evidence.get("origin") == "network_json_record"
                for candidate in selected
            )
            else ScrapeStrategy.INLINE_JSON
        )
        reasons = [
            "Captured structured evidence is bounded and cannot prove complete pagination."
        ]
        if not selected:
            reasons.append("No independently grounded job records were found in captured JSON.")
        return DiscoveryBatch(
            strategy=strategy,
            completeness=CompletenessState.PARTIAL,
            candidates=selected,
            pages_visited=1,
            pagination_complete=False,
            reasons=reasons,
            metrics=metrics,
        )

    def _walk_document(
        self,
        payload: Any,
        *,
        origin: str,
        base_url: str,
        document_id: str,
        budget: int,
        candidate_budget: int,
        metrics: dict[str, Any],
    ) -> tuple[list[DiscoveryCandidate], int]:
        found: list[DiscoveryCandidate] = []
        scanned = 0
        stack: list[tuple[Any, tuple[str, ...], int]] = [(payload, (), 0)]
        while stack and scanned < budget and len(found) < candidate_budget:
            value, path, depth = stack.pop()
            scanned += 1
            metrics["values_scanned"] += 1
            if isinstance(value, dict):
                metrics["objects_scanned"] += 1
                candidate = self._candidate_from_record(
                    value,
                    path=path,
                    origin=origin,
                    base_url=base_url,
                    document_id=document_id,
                )
                if candidate is not None:
                    found.append(candidate)
                elif self._looks_candidate_shaped(value):
                    metrics["records_rejected"] += 1
                if depth < self.options.max_depth:
                    for key, child in reversed(list(value.items())):
                        if isinstance(child, (dict, list, tuple)):
                            stack.append((child, (*path, str(key)[:200]), depth + 1))
            elif isinstance(value, (list, tuple)):
                metrics["arrays_scanned"] += 1
                if depth < self.options.max_depth:
                    for child in reversed(list(value)[:5_000]):
                        if isinstance(child, (dict, list, tuple)):
                            stack.append((child, (*path, "[*]"), depth + 1))
        if stack:
            metrics["bounded"] = True
        return found, scanned

    def _candidate_from_record(
        self,
        record: dict[str, Any],
        *,
        path: tuple[str, ...],
        origin: str,
        base_url: str,
        document_id: str,
    ) -> DiscoveryCandidate | None:
        fields = self._flatten_fields(record)
        by_key: dict[str, list[_Field]] = {}
        for field in fields:
            by_key.setdefault(field.normalized_key, []).append(field)

        schema_type = _bounded_text(
            record.get("@type") or record.get("type") or record.get("__typename"),
            limit=200,
        )
        schema_job = bool(schema_type and "jobposting" in _normalized_key(schema_type))
        path_text = " ".join(path)
        path_job_context = bool(_JOB_CONTEXT_KEY.search(path_text))

        title_field = self._first_field(by_key, _TITLE_KEYS)
        title = _bounded_text(title_field.value, limit=1_000) if title_field else None
        if not title or title.lower().strip(" -|:") in _GENERIC_TITLES:
            return None

        detail_field = self._first_field(by_key, _URL_KEYS)
        apply_field = self._first_field(by_key, _APPLY_URL_KEYS)
        detail_url = _http_url(detail_field.value, base_url=base_url) if detail_field else None
        apply_url = _http_url(apply_field.value, base_url=base_url) if apply_field else None
        detail_url = detail_url or apply_url
        if not detail_url:
            return None

        id_field = self._first_field(by_key, _ID_KEYS)
        source_job_id = _bounded_text(id_field.value, limit=500) if id_field else None
        location_field = self._first_field(by_key, _LOCATION_KEYS)
        location = _bounded_text(location_field.value, limit=1_000) if location_field else None
        summary_field = self._first_field(by_key, _SUMMARY_KEYS)
        summary = _bounded_text(summary_field.value, limit=20_000) if summary_field else None
        company_field = self._first_field(by_key, _COMPANY_KEYS)
        company = _bounded_text(company_field.value, limit=1_000) if company_field else None
        employment_field = self._first_field(by_key, _EMPLOYMENT_KEYS)
        employment = _bounded_text(employment_field.value, limit=500) if employment_field else None
        posted_field = self._first_field(by_key, _POSTED_KEYS)
        posted = _bounded_text(posted_field.value, limit=500) if posted_field else None

        title_specific = bool(
            title_field and title_field.normalized_key in set(_STRONG_TITLE_KEYS)
        )
        url_specific = bool(
            detail_field and detail_field.normalized_key in set(_STRONG_URL_KEYS)
        )
        independent_signals = sum(
            bool(value)
            for value in (source_job_id, location, summary, company, employment, posted)
        )
        structured_job_record = bool(
            schema_job
            or (path_job_context and (independent_signals >= 1 or title_specific or url_specific))
            or (title_specific and url_specific)
            or (source_job_id and independent_signals >= 2)
        )
        if not structured_job_record:
            return None

        confidence = 0.70
        confidence += 0.12 if schema_job else 0.0
        confidence += 0.06 if path_job_context else 0.0
        confidence += 0.04 if title_specific else 0.0
        confidence += 0.04 if url_specific else 0.0
        confidence += 0.04 if source_job_id else 0.0
        confidence += 0.03 if location else 0.0
        confidence += 0.03 if summary else 0.0
        confidence = round(min(0.99, confidence), 4)
        if confidence < self.options.minimum_confidence:
            return None

        preextracted_job: JobPosting | None = None
        if independent_signals:
            preextracted_job = JobPosting(
                title=title,
                job_url=detail_url,
                apply_url=apply_url,
                company=company,
                location_text=location,
                employment_type=employment,
                posted_date=posted,
                summary=summary,
                job_reference=source_job_id,
            )

        stable_identity = f"{source_job_id or ''}|{detail_url}"
        candidate_id = "api_record_" + hashlib.sha256(
            f"{origin}:{stable_identity}".encode("utf-8")
        ).hexdigest()[:24]
        evidence = {
            "origin": origin,
            "contract_version": JSON_DISCOVERY_CONTRACT_VERSION,
            "document_id": document_id[:200],
            "record_path": list(path[-12:]),
            "schema_type": schema_type,
            "title_key": title_field.key if title_field else None,
            "url_key": detail_field.key if detail_field else None,
            "id_key": id_field.key if id_field else None,
            "path_job_context": path_job_context,
            "independent_signals": independent_signals,
            "structured_job_record": structured_job_record,
            "evidence_preserving": (
                confidence >= self.options.evidence_preservation_confidence
            ),
        }
        return DiscoveryCandidate(
            candidate_id=candidate_id,
            kind=DiscoveryCandidateKind.API_RECORD,
            detail_url=detail_url,
            apply_url=apply_url,
            source_job_id=source_job_id,
            title_hint=title,
            location_hint=location,
            confidence=confidence,
            evidence=evidence,
            preextracted_job=preextracted_job,
        )

    @staticmethod
    def _flatten_fields(record: dict[str, Any]) -> list[_Field]:
        fields: list[_Field] = []
        stack: list[tuple[str, Any, int]] = [
            (str(key), value, 0) for key, value in reversed(list(record.items()))
        ]
        while stack and len(fields) < 500:
            key, value, depth = stack.pop()
            fields.append(_Field(key=key[:300], normalized_key=_normalized_key(key), value=value))
            if depth >= 2 or not isinstance(value, dict):
                continue
            for child_key, child in reversed(list(value.items())):
                stack.append((str(child_key), child, depth + 1))
        return fields

    @staticmethod
    def _first_field(
        fields: dict[str, list[_Field]],
        preferred_keys: Iterable[str],
    ) -> _Field | None:
        for key in preferred_keys:
            values = fields.get(_normalized_key(key)) or []
            for field in values:
                if _bounded_text(field.value):
                    return field
        return None

    @staticmethod
    def _looks_candidate_shaped(record: dict[str, Any]) -> bool:
        keys = {_normalized_key(key) for key in record}
        return bool(keys & set(_TITLE_KEYS)) and bool(keys & set(_URL_KEYS))

    @staticmethod
    def _deduplicate(candidates: list[DiscoveryCandidate]) -> list[DiscoveryCandidate]:
        selected: dict[str, DiscoveryCandidate] = {}
        order: dict[str, int] = {}
        for position, candidate in enumerate(candidates):
            key = candidate.detail_url or candidate.identity_key
            existing = selected.get(key)
            replace_existing = existing is None or (
                candidate.preextracted_job is not None
                and existing.preextracted_job is None
            ) or candidate.confidence > existing.confidence
            if replace_existing:
                selected[key] = candidate
                order.setdefault(key, position)
        return sorted(
            selected.values(),
            key=lambda candidate: (
                -int(candidate.preextracted_job is not None),
                -candidate.confidence,
                order[candidate.detail_url or candidate.identity_key],
            ),
        )

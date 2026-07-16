from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any, Iterable
from urllib.parse import urljoin

from ..extract.deterministic_lane import extract_job_from_result
from .detector import detect_portal, infer_listing_url
from .result_evidence import detection_html, result_page_quality
from .url_intelligence import (
    assess_llm_eligibility,
    promote_trusted_detail_url,
    rank_job_candidate_urls,
)


_SAFE_HEADER_NAMES = {
    "cache-control",
    "content-language",
    "content-type",
    "location",
    "server",
    "vary",
    "x-frame-options",
}


def _public_value(value: Any, *, depth: int = 0, max_items: int = 1_000) -> Any:
    """Convert Crawl4AI/Pydantic containers into bounded JSON-safe values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if depth >= 6:
        return f"<{type(value).__name__}>"
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    elif hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json")
        except TypeError:
            value = value.model_dump()
    elif hasattr(value, "dict") and callable(value.dict):
        try:
            value = value.dict()
        except Exception:
            pass

    if isinstance(value, dict):
        return {
            str(key): _public_value(item, depth=depth + 1, max_items=max_items)
            for key, item in list(value.items())[:max_items]
            if not str(key).startswith("_")
        }
    if isinstance(value, (list, tuple, set)):
        return [
            _public_value(item, depth=depth + 1, max_items=max_items)
            for item in list(value)[:max_items]
        ]
    if hasattr(value, "__dict__"):
        return _public_value(vars(value), depth=depth + 1, max_items=max_items)
    return str(value)


def normalize_link_buckets(value: Any, *, max_links: int = 2_000) -> dict[str, list[dict[str, str]]]:
    """Normalize all known Crawl4AI link container shapes for diagnostics."""
    serialized = _public_value(value, max_items=max_links)
    if not isinstance(serialized, dict):
        serialized = {}

    buckets: dict[str, list[dict[str, str]]] = {"internal": [], "external": []}
    for bucket_name in buckets:
        raw_bucket = serialized.get(bucket_name)
        if not isinstance(raw_bucket, list):
            raw_bucket = []
        for raw in raw_bucket[:max_links]:
            if isinstance(raw, str):
                href, text, title = raw, "", ""
            elif isinstance(raw, dict):
                href = str(raw.get("href") or raw.get("url") or "").strip()
                text = " ".join(str(raw.get("text") or "").split())
                title = " ".join(str(raw.get("title") or "").split())
            else:
                continue
            if href:
                buckets[bucket_name].append({"href": href, "text": text, "title": title})
    return buckets


class _SurfaceParser(HTMLParser):
    def __init__(self, *, max_items: int = 2_000) -> None:
        super().__init__(convert_charrefs=True)
        self.max_items = max_items
        self.title_chunks: list[str] = []
        self._in_title = False
        self._anchor: dict[str, str] | None = None
        self._anchor_chunks: list[str] = []
        self.anchors: list[dict[str, str]] = []
        self.iframes: list[dict[str, str]] = []
        self.forms: list[dict[str, str]] = []
        self.scripts: list[dict[str, str]] = []
        self.canonical_urls: list[str] = []
        self.json_ld_scripts = 0

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).lower(): str(value or "").strip() for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        values = self._attrs(attrs)
        if lowered == "title":
            self._in_title = True
        elif lowered == "a" and len(self.anchors) < self.max_items:
            href = values.get("href", "")
            self._anchor = {"href": href, "title": values.get("title", "")}
            self._anchor_chunks = []
        elif lowered == "iframe" and len(self.iframes) < self.max_items:
            self.iframes.append(
                {"src": values.get("src", ""), "title": values.get("title", "")}
            )
        elif lowered == "form" and len(self.forms) < self.max_items:
            self.forms.append(
                {"action": values.get("action", ""), "method": values.get("method", "get").lower()}
            )
        elif lowered == "script" and len(self.scripts) < self.max_items:
            script_type = values.get("type", "").lower().split(";", 1)[0]
            self.scripts.append({"src": values.get("src", ""), "type": script_type})
            if script_type == "application/ld+json":
                self.json_ld_scripts += 1
        elif lowered == "link":
            rel = values.get("rel", "").lower().split()
            href = values.get("href", "")
            if "canonical" in rel and href and len(self.canonical_urls) < 20:
                self.canonical_urls.append(href)

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if not value:
            return
        if self._in_title:
            self.title_chunks.append(value)
        if self._anchor is not None:
            self._anchor_chunks.append(value)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "title":
            self._in_title = False
        elif lowered == "a" and self._anchor is not None:
            self.anchors.append(
                {**self._anchor, "text": " ".join(self._anchor_chunks)[:1_000]}
            )
            self._anchor = None
            self._anchor_chunks = []


def inspect_html_surface(html: str | None, *, max_items: int = 2_000) -> dict[str, Any]:
    parser = _SurfaceParser(max_items=max_items)
    try:
        parser.feed(str(html or ""))
    except Exception as exc:
        parse_error: str | None = f"{type(exc).__name__}: {exc}"
    else:
        parse_error = None
    return {
        "title": " ".join(parser.title_chunks)[:500] or None,
        "anchors": parser.anchors,
        "iframes": parser.iframes,
        "forms": parser.forms,
        "scripts": parser.scripts,
        "canonical_urls": parser.canonical_urls,
        "json_ld_scripts": parser.json_ld_scripts,
        "parse_error": parse_error,
    }


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raw_markdown = getattr(value, "raw_markdown", None)
    if raw_markdown is not None:
        return str(raw_markdown)
    return str(value)


def _safe_headers(value: Any) -> dict[str, str]:
    serialized = _public_value(value, max_items=100)
    if not isinstance(serialized, dict):
        return {}
    return {
        str(key).lower(): str(item)
        for key, item in serialized.items()
        if str(key).lower() in _SAFE_HEADER_NAMES
    }


def _all_link_urls(
    requested_url: str,
    dom: dict[str, Any],
    links: dict[str, list[dict[str, str]]],
) -> list[str]:
    values: list[str] = []
    for anchor in dom.get("anchors") or []:
        values.append(str(anchor.get("href") or ""))
    for iframe in dom.get("iframes") or []:
        values.append(str(iframe.get("src") or ""))
    for bucket in ("internal", "external"):
        for link in links.get(bucket) or []:
            values.append(str(link.get("href") or ""))
    return list(
        dict.fromkeys(
            urljoin(requested_url, value.strip())
            for value in values
            if value and value.strip()
        )
    )


def _failure_stage(
    *,
    mode: str,
    success: bool,
    blocked: bool,
    candidates: list[str],
    inferred_listing: str | None,
    requested_url: str,
    job: Any,
    llm_eligible: bool,
    dom: dict[str, Any],
    html_length: int,
    pipeline_inferred_listing: str | None,
    promoted_detail_url: str | None,
) -> str:
    if not success:
        return "acquisition_failed"
    # A parsed Schema.org job is stronger evidence than generic CAPTCHA text
    # appearing in a footer, privacy notice, or unrelated script.
    if mode == "detail" and job is not None:
        return "deterministic_extraction_succeeded"
    if blocked:
        return "access_control_detected"
    if mode == "listing":
        if (
            inferred_listing
            and inferred_listing.rstrip("/") != requested_url.rstrip("/")
            and not pipeline_inferred_listing
        ):
            return "pipeline_evidence_gap"
        if inferred_listing and inferred_listing.rstrip("/") != requested_url.rstrip("/"):
            return "listing_route_transition_detected"
        return "listing_candidates_found" if candidates else "zero_discovery"
    if promoted_detail_url:
        return "embedded_detail_transition_detected"
    if dom.get("iframes"):
        return "embedded_detail_document_suspected"
    if not llm_eligible:
        return "non_job_detail_document"
    return "deterministic_miss_llm_eligible"


def build_surface_report(result: Any, *, requested_url: str, mode: str) -> dict[str, Any]:
    if mode not in {"listing", "detail"}:
        raise ValueError("mode must be 'listing' or 'detail'")

    html = _text(getattr(result, "html", None))
    cleaned_html = _text(getattr(result, "cleaned_html", None))
    markdown = _text(getattr(result, "markdown", None))
    fit_markdown = _text(getattr(result, "fit_markdown", None))
    primary_html = cleaned_html or html or markdown
    dom = inspect_html_surface(html or primary_html)
    normalized_links = normalize_link_buckets(getattr(result, "links", None))
    link_urls = _all_link_urls(requested_url, dom, normalized_links)
    pipeline_detection_html = detection_html(result, primary_html)
    normalized_detection_html = detection_html(
        SimpleNamespace(links=normalized_links),
        primary_html,
    )
    pipeline_detection = detect_portal(
        listing_url=requested_url,
        html=pipeline_detection_html,
        text_content=markdown,
    )
    detection = detect_portal(
        listing_url=requested_url,
        html=normalized_detection_html,
        text_content=markdown,
    )
    pipeline_inferred_listing = infer_listing_url(requested_url, pipeline_detection_html)
    inferred_listing = infer_listing_url(requested_url, normalized_detection_html)
    candidates, ranking = rank_job_candidate_urls(
        link_urls,
        listing_url=requested_url,
        platform_hint=detection.source_platform,
    )
    job = extract_job_from_result(result, requested_url) if mode == "detail" else None
    promotion_detection = (
        pipeline_detection
        if pipeline_detection.acquisition_hints
        else detection
    )
    promoted_detail_url: str | None = None
    if mode == "detail":
        promoted = promote_trusted_detail_url(
            requested_url,
            platform_hint=promotion_detection.source_platform,
            acquisition_hints=promotion_detection.acquisition_hints,
            page_content="\n".join((html, cleaned_html, markdown)),
        )
        if promoted != requested_url:
            promoted_detail_url = promoted
    llm_eligible, llm_reason = (
        assess_llm_eligibility(result, requested_url)
        if mode == "detail"
        else (False, "not evaluated for a listing surface")
    )
    success = bool(getattr(result, "success", False))
    page_quality = result_page_quality(result)

    return {
        "contract_version": "1.0",
        "requested_url": requested_url,
        "mode": mode,
        "result": {
            "success": success,
            "status_code": getattr(result, "status_code", None),
            "url": str(getattr(result, "url", "") or "") or None,
            "redirected_url": str(getattr(result, "redirected_url", "") or "") or None,
            "error_message": str(getattr(result, "error_message", "") or "") or None,
            "response_headers": _safe_headers(getattr(result, "response_headers", None)),
            "html_length": len(html),
            "cleaned_html_length": len(cleaned_html),
            "markdown_length": len(markdown),
            "fit_markdown_length": len(fit_markdown),
            "links_container_type": type(getattr(result, "links", None)).__name__,
            "link_counts": {key: len(value) for key, value in normalized_links.items()},
        },
        "dom": dom,
        "links": normalized_links,
        "detection": detection.to_dict(),
        "pipeline_detection": pipeline_detection.to_dict(),
        "page_quality": page_quality.to_dict(),
        "pipeline_inferred_listing_url": pipeline_inferred_listing,
        "inferred_listing_url": inferred_listing,
        "evidence_gap": {
            "present": bool(inferred_listing and not pipeline_inferred_listing),
            "reason": (
                "normalized Crawl4AI links expose a route that the current pipeline evidence omits"
                if inferred_listing and not pipeline_inferred_listing
                else None
            ),
        },
        "candidate_ranking": ranking,
        "selected_candidate_urls": candidates[:100],
        "promoted_detail_url": promoted_detail_url,
        "deterministic_job": job.model_dump(mode="json") if job is not None else None,
        "llm_gate": {"eligible": llm_eligible, "reason": llm_reason, "invoked": False},
        "failure_stage": _failure_stage(
            mode=mode,
            success=success,
            blocked=bool(detection.blocked or page_quality.blocked),
            candidates=candidates,
            inferred_listing=inferred_listing,
            requested_url=requested_url,
            job=job,
            llm_eligible=llm_eligible,
            dom=dom,
            html_length=len(html),
            pipeline_inferred_listing=pipeline_inferred_listing,
            promoted_detail_url=promoted_detail_url,
        ),
    }


def report_summary(report: dict[str, Any]) -> str:
    result = report.get("result") or {}
    ranking = report.get("candidate_ranking") or {}
    page_quality = report.get("page_quality") or {}
    return " ".join(
        (
            f"stage={report.get('failure_stage')}",
            f"status={result.get('status_code')}",
            f"html={result.get('html_length')}",
            f"structured_links={sum((result.get('link_counts') or {}).values())}",
            f"candidates={ranking.get('selected_urls', 0)}",
            f"platform={(report.get('detection') or {}).get('source_platform')}",
            f"quality={'blocked' if page_quality.get('blocked') else 'usable'}",
            f"detail_transition={'true' if report.get('promoted_detail_url') else 'false'}",
        )
    )


def dump_report(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)

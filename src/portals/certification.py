from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import posixpath
import re
import time
import uuid
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

from ..blueprint_hub import BlueprintHub
from ..crawl.browser_lane import build_browser_config, listing_run_config
from ..gpu_monitor import query_gpu_snapshot
from ..schemas import JobPosting
from .acquisition import (
    AcquisitionContext,
    AcquisitionOutcome,
    AcquisitionRegistry,
    default_acquisition_registry,
)
from .blueprint import build_portal_blueprint
from .detector import (
    KNOWN_BROWSER_ATS_HOSTS,
    PortalDetection,
    detect_portal,
    known_browser_ats_platform,
)
from .orchestrator import (
    ScrapeExecutionOptions,
    ScrapeOrchestrator,
    ScrapeOrchestratorHooks,
)
from .page_quality import classify_crawler_failure
from .result_evidence import detection_html, result_page_quality
from .route_resolver import (
    ListingRouteResolution,
    hosts_are_related,
    resolve_listing_route,
    route_acquisition_hints,
    route_host_is_trusted,
)
from .safety import (
    PortalUrlSafetyError,
    default_allowed_hosts,
    validate_public_http_url,
)
from .url_intelligence import (
    assess_certification_job,
    assess_llm_eligibility,
    assess_llm_job_grounding,
    canonicalize_candidate_url,
    rank_job_candidate_urls,
)


CERTIFICATION_CONTRACT_VERSION = "1.3"
_HTTP_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", flags=re.IGNORECASE)
_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_slug(value: str, *, maximum: int = 70) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return normalized[:maximum] or "portal"


def _normalize_inventory_url(value: str) -> str:
    raw = str(value or "").strip().rstrip(").,;]}")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Portal inventory URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Portal inventory URLs cannot contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Portal inventory URL has an invalid port") from exc
    if port not in {None, 80, 443}:
        raise ValueError("Portal inventory URL must use a standard HTTP(S) port")
    hostname = parsed.hostname.lower().rstrip(".")
    netloc = hostname
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, parsed.fragment))


def _source_id(url: str) -> str:
    hostname = urlsplit(url).hostname or "portal"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"cert_{_safe_slug(hostname, maximum=55)}_{digest}"


@dataclass(frozen=True)
class PortalInventoryEntry:
    source_id: str
    display_name: str
    listing_url: str
    source_row: int


def _extract_urls(values: Iterable[Any]) -> list[str]:
    urls: list[str] = []
    for value in values:
        for match in _HTTP_URL_PATTERN.findall(str(value or "")):
            try:
                normalized = _normalize_inventory_url(match)
            except ValueError:
                continue
            if normalized not in urls:
                urls.append(normalized)
    return urls


def _display_name(values: Iterable[Any], url: str) -> str:
    ignored = {"url", "website", "portal", "portal url", "job portal", "portal name", "name"}
    for value in values:
        text = " ".join(str(value or "").split()).strip()
        if not text or text.lower() in ignored or _HTTP_URL_PATTERN.search(text):
            continue
        return text[:300]
    return (urlsplit(url).hostname or "Job portal")[:300]


def _relationship_targets(archive: zipfile.ZipFile, relationship_path: str) -> dict[str, str]:
    if relationship_path not in archive.namelist():
        return {}
    root = ElementTree.fromstring(archive.read(relationship_path))
    return {
        str(node.attrib.get("Id") or ""): str(node.attrib.get("Target") or "")
        for node in root.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
        if node.attrib.get("Id") and node.attrib.get("Target")
    }


def _zip_target(base_path: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_path), target))


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read(path))
    return [
        "".join(node.text or "" for node in item.findall(f".//{{{_XLSX_MAIN_NS}}}t"))
        for item in root.findall(f"{{{_XLSX_MAIN_NS}}}si")
    ]


def _xlsx_cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = str(cell.attrib.get("t") or "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(f".//{{{_XLSX_MAIN_NS}}}t"))
    value_node = cell.find(f"{{{_XLSX_MAIN_NS}}}v")
    raw = str(value_node.text or "") if value_node is not None else ""
    if cell_type == "s" and raw:
        try:
            return shared_strings[int(raw)]
        except (IndexError, TypeError, ValueError):
            return ""
    return raw


def _xlsx_rows(path: Path) -> list[list[str]]:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"Invalid XLSX workbook: {path}") from exc

    with archive:
        workbook_path = "xl/workbook.xml"
        if workbook_path not in archive.namelist():
            raise ValueError("XLSX workbook is missing xl/workbook.xml")
        workbook = ElementTree.fromstring(archive.read(workbook_path))
        relationships = _relationship_targets(archive, "xl/_rels/workbook.xml.rels")
        shared_strings = _xlsx_shared_strings(archive)
        rows: list[list[str]] = []

        for sheet in workbook.findall(f".//{{{_XLSX_MAIN_NS}}}sheet"):
            relationship_id = str(sheet.attrib.get(f"{{{_XLSX_REL_NS}}}id") or "")
            target = relationships.get(relationship_id)
            if not target:
                continue
            sheet_path = _zip_target(workbook_path, target)
            if sheet_path not in archive.namelist():
                continue
            sheet_root = ElementTree.fromstring(archive.read(sheet_path))
            for row in sheet_root.findall(f".//{{{_XLSX_MAIN_NS}}}row"):
                values = [_xlsx_cell_value(cell, shared_strings) for cell in row.findall(f"{{{_XLSX_MAIN_NS}}}c")]
                if any(str(value).strip() for value in values):
                    rows.append(values)

            relationship_path = posixpath.join(
                posixpath.dirname(sheet_path),
                "_rels",
                f"{posixpath.basename(sheet_path)}.rels",
            )
            for target_url in _relationship_targets(archive, relationship_path).values():
                if str(target_url).lower().startswith(("http://", "https://")):
                    rows.append([target_url])
        return rows


def _delimited_rows(path: Path) -> list[list[str]]:
    if path.suffix.lower() == ".txt":
        return [[line] for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [list(row) for row in csv.reader(handle)]


def read_portal_inventory(path: Path) -> list[PortalInventoryEntry]:
    source = Path(path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Portal inventory does not exist: {source}")
    if source.suffix.lower() == ".xlsx":
        rows = _xlsx_rows(source)
    elif source.suffix.lower() in {".csv", ".txt"}:
        rows = _delimited_rows(source)
    else:
        raise ValueError("Portal inventory must be an .xlsx, .csv, or .txt file")

    entries: list[PortalInventoryEntry] = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=1):
        for url in _extract_urls(row):
            if url in seen:
                continue
            seen.add(url)
            entries.append(
                PortalInventoryEntry(
                    source_id=_source_id(url),
                    display_name=_display_name(row, url),
                    listing_url=url,
                    source_row=row_number,
                )
            )
    if not entries:
        raise ValueError("Portal inventory contained no valid HTTP(S) URLs")
    return entries


@dataclass(frozen=True)
class CertificationOptions:
    max_jobs: int = 10
    max_pages: int = 3
    detail_concurrency: int = 1
    detail_retry_attempts: int = 1
    requests_per_minute: int = 30
    source_timeout_seconds: int = 600
    acquisition_timeout_seconds: float = 20.0
    allow_unknown_cross_domain_redirects: bool = False
    allow_llm_fallback: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.max_jobs <= 10:
            raise ValueError("Certification max_jobs must be between 1 and 10")
        if self.max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        if not 1 <= self.detail_concurrency <= 2:
            raise ValueError("detail_concurrency must be 1 or 2 for bounded local-GPU certification")
        if not 0 <= self.detail_retry_attempts <= 2:
            raise ValueError("detail_retry_attempts must be between 0 and 2")
        if self.requests_per_minute < 1:
            raise ValueError("requests_per_minute must be at least 1")
        if self.source_timeout_seconds < 30:
            raise ValueError("source_timeout_seconds must be at least 30")


@dataclass(frozen=True)
class PortalCertificationRecord:
    contract_version: str
    run_id: str
    attempt_number: int
    source_id: str
    display_name: str
    provided_url: str
    effective_listing_url: str
    status: str
    certification_status: str
    stage: str
    detected_platform: str | None
    detected_profile: str | None
    discovered_urls: int
    attempted_urls: int
    extracted_jobs: int
    sample_jobs: list[dict[str, Any]]
    acquisition: dict[str, Any]
    detail_failures: list[dict[str, Any]]
    rejected_urls: int
    event_counts: dict[str, int]
    gpu_before: dict[str, Any]
    gpu_after: dict[str, Any]
    started_at: str
    completed_at: str
    elapsed_seconds: float
    error_type: str | None = None
    error_message: str | None = None
    ranked_candidates: int = 0
    ranking_rejected_urls: int = 0
    llm_skipped_non_job_pages: int = 0
    llm_grounding_rejections: int = 0
    surface_kind: str | None = None
    resolved_route_url: str | None = None
    route_resolution: dict[str, Any] = field(default_factory=dict)
    discovery_quality: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ProbeResult:
    effective_listing_url: str
    detection: PortalDetection
    allowed_hosts: tuple[str, ...]
    acquisition_outcome: AcquisitionOutcome
    route_resolution: dict[str, Any] = field(default_factory=dict)


class PortalCertificationBlocked(RuntimeError):
    pass


class PortalCertificationRedirectReview(RuntimeError):
    pass


class PortalCertificationJavaScriptShell(RuntimeError):
    pass


def _result_html(result: Any) -> str:
    primary = ""
    for name in ("cleaned_html", "html", "markdown"):
        value = getattr(result, name, None)
        if value:
            primary = str(value)
            break
    return detection_html(result, primary)


def _result_text(result: Any) -> str:
    for name in ("markdown", "fit_markdown", "text"):
        value = getattr(result, name, None)
        if value:
            return str(value)
    return ""


def _result_url(result: Any, fallback: str) -> str:
    for name in ("url", "final_url", "redirected_url"):
        value = getattr(result, name, None)
        if value:
            return str(value)
    return fallback


def _same_site(first: str, second: str) -> bool:
    return hosts_are_related(first, second)


def _with_route_hints(
    detection: PortalDetection,
    resolution: ListingRouteResolution,
) -> PortalDetection:
    if resolution.selected is None:
        return detection
    hints = {**detection.acquisition_hints, **route_acquisition_hints(resolution)}
    reason = (
        f"Evidence-bound route resolver selected {resolution.selected.url} "
        f"with score {resolution.selected.score}."
    )
    return replace(
        detection,
        confidence=max(detection.confidence, min(0.98, resolution.selected.score / 30.0)),
        reasons=[*detection.reasons, reason],
        acquisition_hints=hints,
    )


def _approved_hosts_for_url(url: str) -> list[str]:
    hostname = str(urlsplit(url).hostname or "").lower()
    approved = list(default_allowed_hosts(hostname))
    platform = known_browser_ats_platform(url)
    if platform:
        for suffix in KNOWN_BROWSER_ATS_HOSTS[platform]:
            approved.extend((suffix, f"*.{suffix}"))
    return list(dict.fromkeys(approved))


def _safe_discovered_urls(urls: list[str], approved_hosts: Iterable[str]) -> tuple[list[str], int]:
    valid: list[str] = []
    rejected = 0
    for raw in urls:
        try:
            checked = validate_public_http_url(raw, allowed_hosts=approved_hosts)
        except PortalUrlSafetyError:
            rejected += 1
            continue
        canonical = canonicalize_candidate_url(checked.normalized_url)
        if canonical and canonical not in valid:
            valid.append(canonical)
    return valid, rejected


def _classify_error(exc: BaseException) -> tuple[str, str]:
    message = str(exc).lower()
    if isinstance(exc, asyncio.TimeoutError):
        return "source_timeout", "failed"
    if isinstance(exc, PortalCertificationJavaScriptShell):
        return "javascript_shell", "needs_repair"
    if isinstance(exc, PortalCertificationBlocked) or any(
        marker in message
        for marker in (
            "captcha",
            "access denied",
            "cloudflare js challenge",
            "http 403",
            "verify you are human",
            "robot check",
        )
    ):
        return "access_blocked", "access_blocked"
    if isinstance(exc, PortalCertificationRedirectReview):
        return "cross_domain_redirect_review", "needs_repair"
    if isinstance(exc, PortalUrlSafetyError):
        return "unsafe_url", "failed"
    if "zero valid job urls" in message or "zero valid job" in message:
        return "zero_discovery", "needs_repair"
    if any(marker in message for marker in ("timed out", "timeout", "connection", "dns", "resolve")):
        return "network_error", "needs_repair"
    return type(exc).__name__, "needs_repair"


class PortalFleetCertifier:
    def __init__(
        self,
        *,
        root: Path,
        output_dir: Path,
        options: CertificationOptions,
        acquisition_registry: AcquisitionRegistry | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.options = options
        self.hub = BlueprintHub(self.root)
        self.acquisition_registry = acquisition_registry or default_acquisition_registry()

    async def _acquire(
        self,
        *,
        listing_url: str,
        detection: PortalDetection,
    ) -> AcquisitionOutcome:
        return await self.acquisition_registry.acquire(
            AcquisitionContext(
                listing_url=listing_url,
                source_platform_hint=detection.source_platform,
                acquisition_hints=detection.acquisition_hints,
                max_pages=self.options.max_pages,
                timeout_seconds=self.options.acquisition_timeout_seconds,
                require_complete=False,
            )
        )

    async def _probe(self, entry: PortalInventoryEntry) -> _ProbeResult:
        checked = validate_public_http_url(entry.listing_url)
        direct_detection = detect_portal(listing_url=checked.normalized_url, html="", text_content="")
        direct_outcome = await self._acquire(
            listing_url=checked.normalized_url,
            detection=direct_detection,
        )
        if direct_outcome.selected is not None:
            selected = direct_outcome.selected
            selected_listing = str(selected.metadata.get("listing_url") or "").strip()
            effective_checked = checked
            if selected_listing:
                candidate_checked = validate_public_http_url(selected_listing)
                if candidate_checked.hostname not in selected.trusted_hosts:
                    raise PortalCertificationRedirectReview(
                        "Acquisition returned a listing host outside its trusted-host contract"
                    )
                effective_checked = candidate_checked
            effective_detection = detect_portal(
                listing_url=effective_checked.normalized_url,
                html="",
                text_content="",
            )
            return _ProbeResult(
                effective_listing_url=effective_checked.normalized_url,
                detection=effective_detection,
                allowed_hosts=tuple(
                    dict.fromkeys([*checked.allowed_hosts, *selected.trusted_hosts])
                ),
                acquisition_outcome=direct_outcome,
            )

        from crawl4ai import AsyncWebCrawler

        browser_config = build_browser_config(self.hub.system.browser)
        session_id = f"cert_probe_{entry.source_id[-20:]}"
        async with AsyncWebCrawler(config=browser_config) as crawler:
            result = await crawler.arun(
                url=checked.normalized_url,
                config=listing_run_config(self.hub.system.browser, session_id, None),
            )
        if not getattr(result, "success", False):
            failure_message = str(getattr(result, "error_message", "unknown browser error"))
            failure_kind = classify_crawler_failure(failure_message)
            if failure_kind == "confirmed_access_control":
                raise PortalCertificationBlocked(f"Listing probe blocked: {failure_message}")
            if failure_kind == "javascript_shell":
                return _ProbeResult(
                    effective_listing_url=checked.normalized_url,
                    detection=replace(direct_detection, surface_kind="javascript_shell"),
                    allowed_hosts=checked.allowed_hosts,
                    acquisition_outcome=direct_outcome,
                )
            raise RuntimeError(
                f"Listing probe failed: {failure_message}"
            )

        page_quality = result_page_quality(result)
        if page_quality.blocked:
            raise PortalCertificationBlocked(
                f"The listing document was suppressed by access control: {page_quality.reason}"
            )

        final_checked = validate_public_http_url(_result_url(result, checked.normalized_url))
        page_html = _result_html(result)
        page_text = _result_text(result)
        route_resolution = resolve_listing_route(
            source_url=checked.normalized_url,
            final_url=final_checked.normalized_url,
            html=page_html,
            structured_links=getattr(result, "links", None),
        )

        original_host = checked.hostname
        final_host = final_checked.hostname
        redirect_known = bool(known_browser_ats_platform(final_checked.normalized_url))
        redirect_trusted = route_host_is_trusted(route_resolution, final_host)
        if (
            final_host != original_host
            and not _same_site(original_host, final_host)
            and not redirect_known
            and not redirect_trusted
            and not self.options.allow_unknown_cross_domain_redirects
        ):
            raise PortalCertificationRedirectReview(
                f"Listing redirected from {original_host} to unclassified host {final_host}"
            )

        resolved_checked = final_checked
        if route_resolution.selected is not None:
            resolved_checked = validate_public_http_url(route_resolution.selected.url)
        detection = detect_portal(
            listing_url=resolved_checked.normalized_url,
            html=page_html,
            text_content=page_text,
        )
        detection = _with_route_hints(detection, route_resolution)
        if detection.blocked:
            raise PortalCertificationBlocked("The rendered listing page contains an access-control indicator")
        if detection.surface_kind == "javascript_shell" and not detection.acquisition_hints:
            shell_outcome = await self._acquire(
                listing_url=final_checked.normalized_url,
                detection=detection,
            )
            selected_hosts = (
                list(shell_outcome.selected.trusted_hosts)
                if shell_outcome.selected is not None
                else []
            )
            return _ProbeResult(
                effective_listing_url=final_checked.normalized_url,
                detection=detection,
                allowed_hosts=tuple(
                    dict.fromkeys(
                        [
                            *checked.allowed_hosts,
                            *route_resolution.trusted_hosts,
                            *selected_hosts,
                        ]
                    )
                ),
                acquisition_outcome=shell_outcome,
                route_resolution=route_resolution.to_dict(),
            )

        effective_url = resolved_checked.normalized_url
        hinted_url = str(detection.acquisition_hints.get("listing_url") or "").strip()
        if hinted_url:
            hinted_checked = validate_public_http_url(hinted_url)
            hinted_known = bool(known_browser_ats_platform(hinted_checked.normalized_url))
            hinted_trusted = route_host_is_trusted(route_resolution, hinted_checked.hostname)
            if (
                hinted_checked.hostname != final_checked.hostname
                and not _same_site(hinted_checked.hostname, final_checked.hostname)
                and not hinted_known
                and not hinted_trusted
                and not self.options.allow_unknown_cross_domain_redirects
            ):
                raise PortalCertificationRedirectReview(
                    f"Rendered listing linked to unclassified host {hinted_checked.hostname}"
                )
            effective_url = hinted_checked.normalized_url

        # A branded careers shell can expose the real results route without
        # rendering any jobs itself. Follow that rendered, safety-checked route
        # once and redetect the page so pagination/ATS behavior is learned
        # automatically rather than encoded as a site-specific selector.
        if effective_url != final_checked.normalized_url:
            inferred_outcome = await self._acquire(listing_url=effective_url, detection=detection)
            if inferred_outcome.selected is not None:
                approved_hosts = [
                    *checked.allowed_hosts,
                    *route_resolution.trusted_hosts,
                    *_approved_hosts_for_url(final_checked.normalized_url),
                    *_approved_hosts_for_url(effective_url),
                    *inferred_outcome.selected.trusted_hosts,
                ]
                return _ProbeResult(
                    effective_listing_url=effective_url,
                    detection=detection,
                    allowed_hosts=tuple(dict.fromkeys(approved_hosts)),
                    acquisition_outcome=inferred_outcome,
                    route_resolution=route_resolution.to_dict(),
                )
            async with AsyncWebCrawler(config=browser_config) as crawler:
                hinted_result = await crawler.arun(
                    url=effective_url,
                    config=listing_run_config(self.hub.system.browser, f"{session_id}_listing", None),
                )
            if not getattr(hinted_result, "success", False):
                failure_message = str(
                    getattr(hinted_result, "error_message", "unknown browser error")
                )
                failure_kind = classify_crawler_failure(failure_message)
                if failure_kind == "confirmed_access_control":
                    raise PortalCertificationBlocked(
                        f"Inferred listing route blocked: {failure_message}"
                    )
                if failure_kind == "javascript_shell":
                    raise PortalCertificationJavaScriptShell(
                        "Inferred listing route is a JavaScript shell requiring API discovery: "
                        f"{failure_message}"
                    )
                raise RuntimeError(
                    f"Inferred jobs-listing route failed: {failure_message}"
                )
            hinted_quality = result_page_quality(hinted_result)
            if hinted_quality.blocked:
                raise PortalCertificationBlocked(
                    "The inferred listing document was suppressed by access control: "
                    f"{hinted_quality.reason}"
                )
            hinted_final = validate_public_http_url(_result_url(hinted_result, effective_url))
            if (
                hinted_final.hostname != final_checked.hostname
                and not _same_site(hinted_final.hostname, final_checked.hostname)
                and not known_browser_ats_platform(hinted_final.normalized_url)
                and not self.options.allow_unknown_cross_domain_redirects
            ):
                raise PortalCertificationRedirectReview(
                    f"Inferred listing redirected to unclassified host {hinted_final.hostname}"
                )
            final_checked = hinted_final
            effective_url = hinted_final.normalized_url
            detection = detect_portal(
                listing_url=effective_url,
                html=_result_html(hinted_result),
                text_content=_result_text(hinted_result),
            )
            if detection.blocked:
                raise PortalCertificationBlocked(
                    "The inferred jobs-listing page contains an access-control indicator"
                )

        acquisition_outcome = await self._acquire(listing_url=effective_url, detection=detection)
        approved_hosts = [
            *checked.allowed_hosts,
            *route_resolution.trusted_hosts,
            *_approved_hosts_for_url(final_checked.normalized_url),
            *_approved_hosts_for_url(effective_url),
        ]
        if acquisition_outcome.selected is not None:
            approved_hosts.extend(acquisition_outcome.selected.trusted_hosts)
        return _ProbeResult(
            effective_listing_url=effective_url,
            detection=detection,
            allowed_hosts=tuple(dict.fromkeys(approved_hosts)),
            acquisition_outcome=acquisition_outcome,
            route_resolution=route_resolution.to_dict(),
        )

    def _failed_payload_writer(self, source_id: str) -> Callable[[str, Any], None]:
        failed_dir = self.output_dir / "failures" / source_id

        def save(job_url: str, payload: Any) -> None:
            failed_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(str(job_url).encode("utf-8")).hexdigest()[:16]
            content = str(payload)
            (failed_dir / f"{digest}.txt").write_text(content[:1_000_000], encoding="utf-8")

        return save

    async def certify(
        self,
        entry: PortalInventoryEntry,
        *,
        run_id: str,
        attempt_number: int,
    ) -> PortalCertificationRecord:
        started_at = _utc_now()
        started = time.perf_counter()
        stage = "probe"
        probe: _ProbeResult | None = None
        event_counts: Counter[str] = Counter()
        gpu_before = query_gpu_snapshot()

        try:
            probe = await asyncio.wait_for(
                self._probe(entry),
                timeout=self.options.source_timeout_seconds,
            )
            stage = "bounded_extraction"
            portal = {
                "id": entry.source_id,
                "target_id": entry.source_id,
                "display_name": entry.display_name,
                "listing_url": probe.effective_listing_url,
                "canonical_listing_url": probe.effective_listing_url,
                "profile_name": probe.detection.profile_name,
                "allowed_hosts": list(probe.allowed_hosts),
                "max_pages_per_run": self.options.max_pages,
                "configuration_json": {},
            }
            blueprint = build_portal_blueprint(
                root=self.root,
                portal=portal,
                run_session_id=run_id,
            )
            orchestrator = ScrapeOrchestrator(
                blueprint=blueprint,
                system_config=self.hub.system,
                run_session_id=run_id,
                source_platform_hint=probe.detection.source_platform,
                acquisition_hints=probe.detection.acquisition_hints,
                acquisition_registry=self.acquisition_registry,
            )
            approved_hosts = list(probe.allowed_hosts)

            def validate_certification_job(job: JobPosting, source_url: str) -> tuple[bool, str]:
                try:
                    validate_public_http_url(
                        str(job.job_url or source_url),
                        allowed_hosts=approved_hosts,
                    )
                except PortalUrlSafetyError as exc:
                    return False, f"unsafe extracted job URL: {exc}"
                return assess_certification_job(job, source_url)

            def should_attempt_certification_llm(result: Any, source_url: str) -> tuple[bool, str]:
                if not self.options.allow_llm_fallback:
                    return False, "LLM fallback disabled for deterministic fleet certification"
                return assess_llm_eligibility(result, source_url)

            remaining_timeout = max(
                1.0,
                self.options.source_timeout_seconds - (time.perf_counter() - started),
            )
            orchestration = await asyncio.wait_for(
                orchestrator.run(
                    options=ScrapeExecutionOptions(
                        detail_concurrency=self.options.detail_concurrency,
                        detail_retry_attempts=self.options.detail_retry_attempts,
                        requests_per_minute=self.options.requests_per_minute,
                        max_jobs=self.options.max_jobs,
                        # Certification must retain the adaptive acquisition
                        # diagnostics even when no candidate survives. The
                        # explicit classification below still marks the source
                        # failed and never enables reconciliation.
                        fail_on_zero_discovery=False,
                        session_prefix=f"cert_detail_{entry.source_id[-16:]}",
                        prefer_platform_api=True,
                        max_acquisition_pages=self.options.max_pages,
                        acquisition_timeout_seconds=self.options.acquisition_timeout_seconds,
                        require_complete_acquisition=False,
                        prefer_static_detail_html=True,
                        enable_adaptive_dom_fallback=True,
                        enable_rendered_detail_fallback=True,
                        adaptive_dom_max_candidates=1_000,
                    ),
                    hooks=ScrapeOrchestratorHooks(
                        normalize_discovered_urls=lambda urls: _safe_discovered_urls(urls, approved_hosts),
                        normalize_acquired_urls=lambda urls, trusted: _safe_discovered_urls(
                            urls,
                            [*approved_hosts, *trusted],
                        ),
                        rank_discovered_urls=lambda urls: rank_job_candidate_urls(
                            urls,
                            listing_url=probe.effective_listing_url,
                            platform_hint=probe.detection.source_platform,
                        ),
                        validate_detail_url=lambda url: validate_public_http_url(
                            url,
                            allowed_hosts=approved_hosts,
                        ).normalized_url,
                        is_rejected_error=lambda exc: isinstance(exc, PortalUrlSafetyError),
                        validate_extracted_job=validate_certification_job,
                        should_attempt_llm=should_attempt_certification_llm,
                        validate_llm_extracted_job=assess_llm_job_grounding,
                        on_event=lambda event, payload: event_counts.update([event]),
                        on_failed_payload=self._failed_payload_writer(entry.source_id),
                    ),
                    acquisition_outcome=probe.acquisition_outcome,
                ),
                timeout=remaining_timeout,
            )

            extracted = len(orchestration.jobs)
            failures = len(orchestration.detail_failures)
            discovered_candidate_count = (
                len(orchestration.discovered_candidates)
                or len(orchestration.discovered_job_urls)
            )
            linkless_candidate_count = orchestration.linkless_candidate_count
            error_type: str | None = None
            error_message: str | None = None
            if extracted == 0:
                status = "failed"
                certification_status = "needs_repair"
                if discovered_candidate_count == 0:
                    error_type = "zero_discovery"
                    error_message = (
                        "Adaptive/API/DOM discovery selected zero grounded candidates; "
                        "acquisition diagnostics were retained for repair."
                    )
                elif not orchestration.discovered_job_urls and linkless_candidate_count:
                    error_type = "linkless_interaction_unresolved"
                    error_message = (
                        f"Discovery retained {linkless_candidate_count} grounded linkless "
                        "candidates, but bounded public interaction exposed no unique detail URL."
                    )
                else:
                    error_type = "zero_valid_jobs"
                    error_message = (
                        f"Discovery selected {discovered_candidate_count} candidates "
                        f"({len(orchestration.discovered_job_urls)} URL-backed, "
                        f"{linkless_candidate_count} linkless), "
                        + (
                            "but grounded deterministic/LLM extraction produced no certifiable jobs"
                            if self.options.allow_llm_fallback
                            else "but deterministic extraction produced no certifiable jobs; LLM fallback was disabled"
                        )
                    )
            elif failures:
                status = "partial"
                certification_status = "needs_repair"
                error_type = "detail_extraction_shortfall"
                error_message = f"{failures} of {len(orchestration.attempted_job_urls)} attempted details failed"
            elif len(orchestration.discovered_job_urls) < self.options.max_jobs:
                status = "success"
                certification_status = "source_exhausted"
            else:
                status = "success"
                certification_status = "passed"

            completed_at = _utc_now()
            discovery_quality = dict(orchestration.rescrape_plan.get("url_ranking") or {})
            discovery_quality.update(
                {
                    "discovered_candidates": discovered_candidate_count,
                    "url_backed_candidates": len(orchestration.discovered_job_urls),
                    "linkless_candidates": linkless_candidate_count,
                }
            )
            return PortalCertificationRecord(
                contract_version=CERTIFICATION_CONTRACT_VERSION,
                run_id=run_id,
                attempt_number=attempt_number,
                source_id=entry.source_id,
                display_name=entry.display_name,
                provided_url=entry.listing_url,
                effective_listing_url=probe.effective_listing_url,
                status=status,
                certification_status=certification_status,
                stage="complete",
                detected_platform=probe.detection.source_platform,
                detected_profile=probe.detection.profile_name,
                discovered_urls=len(orchestration.discovered_job_urls),
                attempted_urls=len(orchestration.attempted_job_urls),
                extracted_jobs=extracted,
                sample_jobs=[job.model_dump(mode="json") for job in orchestration.jobs[: self.options.max_jobs]],
                acquisition=orchestration.acquisition,
                detail_failures=orchestration.detail_failures[:25],
                rejected_urls=orchestration.rejected_urls,
                event_counts=dict(event_counts),
                gpu_before=gpu_before,
                gpu_after=query_gpu_snapshot(),
                started_at=started_at,
                completed_at=completed_at,
                elapsed_seconds=round(time.perf_counter() - started, 3),
                error_type=error_type,
                error_message=error_message,
                ranked_candidates=int(discovery_quality.get("selected_urls") or 0),
                ranking_rejected_urls=int(discovery_quality.get("rejected_urls") or 0),
                llm_skipped_non_job_pages=int(event_counts.get("llm_fallback_skipped_non_job") or 0),
                llm_grounding_rejections=int(event_counts.get("llm_grounding_failed") or 0),
                surface_kind=probe.detection.surface_kind,
                resolved_route_url=(
                    str((probe.route_resolution.get("selected") or {}).get("url") or "")
                    or None
                ),
                route_resolution=probe.route_resolution,
                discovery_quality=discovery_quality,
            )
        except Exception as exc:
            error_type, certification_status = _classify_error(exc)
            return PortalCertificationRecord(
                contract_version=CERTIFICATION_CONTRACT_VERSION,
                run_id=run_id,
                attempt_number=attempt_number,
                source_id=entry.source_id,
                display_name=entry.display_name,
                provided_url=entry.listing_url,
                effective_listing_url=(probe.effective_listing_url if probe else entry.listing_url),
                status="blocked" if certification_status == "access_blocked" else "failed",
                certification_status=certification_status,
                stage=stage,
                detected_platform=(probe.detection.source_platform if probe else None),
                detected_profile=(probe.detection.profile_name if probe else None),
                discovered_urls=0,
                attempted_urls=0,
                extracted_jobs=0,
                sample_jobs=[],
                acquisition=(probe.acquisition_outcome.metrics() if probe else {}),
                detail_failures=[],
                rejected_urls=0,
                event_counts=dict(event_counts),
                gpu_before=gpu_before,
                gpu_after=query_gpu_snapshot(),
                started_at=started_at,
                completed_at=_utc_now(),
                elapsed_seconds=round(time.perf_counter() - started, 3),
                error_type=error_type,
                error_message=str(exc)[:2000],
                surface_kind=(
                    probe.detection.surface_kind
                    if probe
                    else "javascript_shell"
                    if error_type == "javascript_shell"
                    else "confirmed_access_control"
                    if error_type == "access_blocked"
                    else None
                ),
                resolved_route_url=(
                    str(((probe.route_resolution if probe else {}).get("selected") or {}).get("url") or "")
                    or None
                ),
                route_resolution=(probe.route_resolution if probe else {}),
            )


class CertificationReportStore:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.output_dir / "portal_certification.jsonl"
        self.summary_path = self.output_dir / "portal_certification_summary.json"
        self.csv_path = self.output_dir / "portal_certification_summary.csv"
        self.jobs_dir = self.output_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def load_latest(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        if not self.jsonl_path.exists():
            return latest
        for line in self.jsonl_path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            source_id = str(payload.get("source_id") or "")
            if source_id:
                latest[source_id] = payload
        return latest

    def append(self, record: PortalCertificationRecord) -> None:
        payload = record.to_dict()
        with self.jsonl_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._atomic_json(self.jobs_dir / f"{record.source_id}.json", record.sample_jobs)

    def write_summary(
        self,
        *,
        inventory_count: int,
        run_id: str,
        latest: dict[str, dict[str, Any]],
        options: CertificationOptions,
        input_path: Path,
    ) -> None:
        records = [latest[key] for key in sorted(latest)]
        status_counts = dict(Counter(str(record.get("status") or "unknown") for record in records))
        platform_counts = dict(
            Counter(str(record.get("detected_platform") or "unknown") for record in records)
        )
        payload = {
            "contract_version": CERTIFICATION_CONTRACT_VERSION,
            "run_id": run_id,
            "generated_at": _utc_now(),
            "input_path": str(Path(input_path).resolve()),
            "input_sha256": hashlib.sha256(Path(input_path).read_bytes()).hexdigest(),
            "inventory_count": inventory_count,
            "latest_result_count": len(records),
            "status_counts": status_counts,
            "platform_counts": platform_counts,
            "options": asdict(options),
            "failed_source_ids": [
                record["source_id"] for record in records if record.get("status") != "success"
            ],
            "records": records,
        }
        self._atomic_json(self.summary_path, payload)
        self._atomic_csv(records)

    @staticmethod
    def _atomic_json(path: Path, payload: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def _atomic_csv(self, records: list[dict[str, Any]]) -> None:
        fields = [
            "source_id",
            "display_name",
            "provided_url",
            "effective_listing_url",
            "status",
            "certification_status",
            "stage",
            "detected_platform",
            "detected_profile",
            "discovered_urls",
            "attempted_urls",
            "extracted_jobs",
            "ranked_candidates",
            "ranking_rejected_urls",
            "llm_skipped_non_job_pages",
            "llm_grounding_rejections",
            "surface_kind",
            "resolved_route_url",
            "elapsed_seconds",
            "error_type",
            "error_message",
        ]
        temporary = self.csv_path.with_suffix(self.csv_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
        os.replace(temporary, self.csv_path)


ProgressCallback = Callable[[PortalCertificationRecord, int, int], None]


async def certify_portal_inventory(
    *,
    input_path: Path,
    root: Path,
    output_dir: Path,
    options: CertificationOptions,
    resume: bool = False,
    only_failed: bool = False,
    limit: int | None = None,
    source_ids: set[str] | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[PortalCertificationRecord]:
    entries = read_portal_inventory(input_path)
    store = CertificationReportStore(output_dir)
    inventory_source_ids = {entry.source_id for entry in entries}
    latest = {
        source_id: payload
        for source_id, payload in store.load_latest().items()
        if source_id in inventory_source_ids
    }
    if only_failed and not latest:
        raise ValueError("--only-failed requires an existing portal_certification.jsonl report")

    selected: list[PortalInventoryEntry] = []
    for entry in entries:
        previous = latest.get(entry.source_id)
        if source_ids and entry.source_id not in source_ids:
            continue
        if only_failed and (previous is None or previous.get("status") == "success"):
            continue
        if resume and previous is not None and previous.get("status") == "success":
            continue
        selected.append(entry)
    if limit is not None:
        selected = selected[: max(0, int(limit))]

    run_id = f"cert_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    certifier = PortalFleetCertifier(root=root, output_dir=output_dir, options=options)
    completed: list[PortalCertificationRecord] = []
    total = len(selected)
    for position, entry in enumerate(selected, start=1):
        attempt_number = int((latest.get(entry.source_id) or {}).get("attempt_number") or 0) + 1
        record = await certifier.certify(
            entry,
            run_id=run_id,
            attempt_number=attempt_number,
        )
        store.append(record)
        latest[entry.source_id] = record.to_dict()
        store.write_summary(
            inventory_count=len(entries),
            run_id=run_id,
            latest=latest,
            options=options,
            input_path=input_path,
        )
        completed.append(record)
        if on_progress is not None:
            on_progress(record, position, total)
    if not selected:
        store.write_summary(
            inventory_count=len(entries),
            run_id=run_id,
            latest=latest,
            options=options,
            input_path=input_path,
        )
    return completed

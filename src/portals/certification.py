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
from dataclasses import asdict, dataclass, field
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
from .safety import (
    PortalUrlSafetyError,
    default_allowed_hosts,
    validate_public_http_url,
)


CERTIFICATION_CONTRACT_VERSION = "1.0"
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ProbeResult:
    effective_listing_url: str
    detection: PortalDetection
    allowed_hosts: tuple[str, ...]
    acquisition_outcome: AcquisitionOutcome


class PortalCertificationBlocked(RuntimeError):
    pass


class PortalCertificationRedirectReview(RuntimeError):
    pass


def _result_html(result: Any) -> str:
    for name in ("cleaned_html", "html", "markdown"):
        value = getattr(result, name, None)
        if value:
            return str(value)
    return ""


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
    first_parts = str(first or "").lower().split(".")
    second_parts = str(second or "").lower().split(".")
    return len(first_parts) >= 2 and len(second_parts) >= 2 and first_parts[-2:] == second_parts[-2:]


def _known_api_platform(detection: PortalDetection) -> bool:
    return detection.source_platform in {"greenhouse", "lever", "ashby", "workday", "jobdiva"}


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
        if checked.normalized_url not in valid:
            valid.append(checked.normalized_url)
    return valid, rejected


def _classify_error(exc: BaseException) -> tuple[str, str]:
    message = str(exc).lower()
    if isinstance(exc, asyncio.TimeoutError):
        return "source_timeout", "failed"
    if isinstance(exc, PortalCertificationBlocked) or any(
        marker in message for marker in ("captcha", "access denied", "verify you are human", "robot check")
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
            return _ProbeResult(
                effective_listing_url=checked.normalized_url,
                detection=direct_detection,
                allowed_hosts=tuple(
                    dict.fromkeys([*checked.allowed_hosts, *direct_outcome.selected.trusted_hosts])
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
            raise RuntimeError(
                f"Listing probe failed: {getattr(result, 'error_message', 'unknown browser error')}"
            )

        final_checked = validate_public_http_url(_result_url(result, checked.normalized_url))
        detection = detect_portal(
            listing_url=final_checked.normalized_url,
            html=_result_html(result),
            text_content=_result_text(result),
        )
        if detection.blocked:
            raise PortalCertificationBlocked("The rendered listing page contains an access-control indicator")

        original_host = checked.hostname
        final_host = final_checked.hostname
        redirect_known = _known_api_platform(detection) or bool(known_browser_ats_platform(final_checked.normalized_url))
        if (
            final_host != original_host
            and not _same_site(original_host, final_host)
            and not redirect_known
            and not self.options.allow_unknown_cross_domain_redirects
        ):
            raise PortalCertificationRedirectReview(
                f"Listing redirected from {original_host} to unclassified host {final_host}"
            )

        effective_url = final_checked.normalized_url
        hinted_url = str(detection.acquisition_hints.get("listing_url") or "").strip()
        if hinted_url:
            effective_url = validate_public_http_url(hinted_url).normalized_url

        acquisition_outcome = await self._acquire(listing_url=effective_url, detection=detection)
        approved_hosts = [
            *checked.allowed_hosts,
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
                        fail_on_zero_discovery=True,
                        session_prefix=f"cert_detail_{entry.source_id[-16:]}",
                        prefer_platform_api=True,
                        max_acquisition_pages=self.options.max_pages,
                        acquisition_timeout_seconds=self.options.acquisition_timeout_seconds,
                        require_complete_acquisition=False,
                    ),
                    hooks=ScrapeOrchestratorHooks(
                        normalize_discovered_urls=lambda urls: _safe_discovered_urls(urls, approved_hosts),
                        normalize_acquired_urls=lambda urls, trusted: _safe_discovered_urls(
                            urls,
                            [*approved_hosts, *trusted],
                        ),
                        validate_detail_url=lambda url: validate_public_http_url(
                            url,
                            allowed_hosts=approved_hosts,
                        ).normalized_url,
                        is_rejected_error=lambda exc: isinstance(exc, PortalUrlSafetyError),
                        on_event=lambda event, payload: event_counts.update([event]),
                        on_failed_payload=self._failed_payload_writer(entry.source_id),
                    ),
                    acquisition_outcome=probe.acquisition_outcome,
                ),
                timeout=remaining_timeout,
            )

            extracted = len(orchestration.jobs)
            failures = len(orchestration.detail_failures)
            if extracted == 0:
                status = "failed"
                certification_status = "needs_repair"
            elif failures:
                status = "partial"
                certification_status = "needs_repair"
            elif len(orchestration.discovered_job_urls) < self.options.max_jobs:
                status = "success"
                certification_status = "source_exhausted"
            else:
                status = "success"
                certification_status = "passed"

            completed_at = _utc_now()
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

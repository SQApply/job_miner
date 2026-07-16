from __future__ import annotations

import asyncio
import html as html_module
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import Request, urlopen

from ..schemas import JobPosting


ACQUISITION_CONTRACT_VERSION = "1.0"
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


class AcquisitionError(RuntimeError):
    """A provider acquisition failed without making browser fallback unsafe."""


class AcquisitionHttpError(AcquisitionError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
        value = " ".join(parser.parts)
    except Exception:
        value = re.sub(r"<[^>]+>", " ", html_module.unescape(raw))
    normalized = " ".join(value.split())
    return normalized or None


def _safe_token(value: Any) -> str | None:
    token = str(value or "").strip().strip("/")
    return token if _TOKEN_PATTERN.fullmatch(token) else None


def _host_matches(hostname: str, allowed_hosts: tuple[str, ...]) -> bool:
    host = str(hostname or "").lower().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)


def _validated_provider_url(value: Any, *, allowed_hosts: tuple[str, ...]) -> str | None:
    raw = str(value or "").strip()
    if not raw or len(raw) > 4096:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        if parsed.port not in {None, 80, 443}:
            return None
    except ValueError:
        return None
    if not _host_matches(parsed.hostname, allowed_hosts):
        return None
    return raw


def _epoch_milliseconds(value: Any) -> str | None:
    try:
        timestamp = float(value) / 1000.0
    except (TypeError, ValueError):
        return _plain_text(value)
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _joined_text(*values: Any) -> str | None:
    rendered = [_plain_text(value) for value in values]
    return " ".join(dict.fromkeys(value for value in rendered if value)) or None


class AsyncJsonClient(Protocol):
    async def request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout_seconds: float = 20.0,
    ) -> Any: ...


class UrlLibJsonClient:
    """Small dependency-free JSON client for fixed, public ATS endpoints."""

    def __init__(self, *, max_response_bytes: int = 20 * 1024 * 1024, max_attempts: int = 3) -> None:
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self.max_attempts = max(1, min(int(max_attempts), 5))

    async def request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout_seconds: float = 20.0,
    ) -> Any:
        last_error: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await asyncio.to_thread(
                    self._request_once,
                    url,
                    method,
                    payload,
                    max(1.0, float(timeout_seconds)),
                )
            except AcquisitionHttpError as exc:
                last_error = exc
                retryable = exc.status_code in {408, 425, 429, 500, 502, 503, 504}
                if not retryable or attempt >= self.max_attempts:
                    raise
            except (TimeoutError, URLError) as exc:
                last_error = exc
                if attempt >= self.max_attempts:
                    break
            await asyncio.sleep(min(2 ** (attempt - 1), 4))
        raise AcquisitionHttpError(f"ATS endpoint request failed: {last_error}") from last_error

    def _request_once(
        self,
        url: str,
        method: str,
        payload: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            url,
            data=body,
            method=str(method or "GET").upper(),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "JobMiner/1.0 (+public-job-feed-client)",
            },
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
        except HTTPError as exc:
            raise AcquisitionHttpError(
                f"ATS endpoint returned HTTP {exc.code}",
                status_code=int(exc.code),
            ) from exc
        if len(raw) > self.max_response_bytes:
            raise AcquisitionHttpError("ATS endpoint response exceeded the configured size limit")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcquisitionHttpError("ATS endpoint did not return valid JSON") from exc


@dataclass(frozen=True)
class AcquisitionContext:
    listing_url: str
    source_platform_hint: str | None = None
    acquisition_hints: dict[str, str] = field(default_factory=dict)
    max_pages: int = 50
    timeout_seconds: float = 20.0
    require_complete: bool = True

    def __post_init__(self) -> None:
        if self.max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class ProviderMatch:
    platform: str
    token: str
    listing_url: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AcquisitionResult:
    platform: str
    strategy: str
    discovered_urls: list[str]
    preextracted_jobs: dict[str, JobPosting]
    trusted_hosts: tuple[str, ...]
    complete: bool
    pages_visited: int
    endpoint_requests: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def metrics(self) -> dict[str, Any]:
        return {
            "contract_version": ACQUISITION_CONTRACT_VERSION,
            "platform": self.platform,
            "strategy": self.strategy,
            "complete": self.complete,
            "reconciliation_safe": self.complete,
            "discovered_urls": len(self.discovered_urls),
            "preextracted_jobs": len(self.preextracted_jobs),
            "pages_visited": self.pages_visited,
            "endpoint_requests": self.endpoint_requests,
            "trusted_hosts": list(self.trusted_hosts),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AcquisitionOutcome:
    selected: AcquisitionResult | None
    attempts: list[dict[str, Any]]

    def metrics(self) -> dict[str, Any]:
        if self.selected is None:
            return {
                "contract_version": ACQUISITION_CONTRACT_VERSION,
                "selected": False,
                "strategy": "browser_fallback",
                "attempts": list(self.attempts),
            }
        return {
            "selected": True,
            **self.selected.metrics(),
            "attempts": list(self.attempts),
        }


class AcquisitionProvider(Protocol):
    platform: str

    def match(self, context: AcquisitionContext) -> ProviderMatch | None: ...

    async def acquire(
        self,
        match: ProviderMatch,
        context: AcquisitionContext,
        client: AsyncJsonClient,
    ) -> AcquisitionResult: ...


def _candidate_urls(context: AcquisitionContext) -> list[str]:
    values = [context.listing_url]
    values.extend(
        value
        for key, value in context.acquisition_hints.items()
        if key.endswith("url") and str(value or "").strip()
    )
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _greenhouse_token(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if not parsed.hostname or not _host_matches(parsed.hostname, ("greenhouse.io",)):
        return None
    query_token = (parse_qs(parsed.query).get("for") or [None])[0]
    if query_token:
        return _safe_token(query_token)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].lower() not in {"embed", "v1"}:
        return _safe_token(parts[0])
    return None


class GreenhouseProvider:
    platform = "greenhouse"
    _job_hosts = ("boards.greenhouse.io", "job-boards.greenhouse.io")

    def match(self, context: AcquisitionContext) -> ProviderMatch | None:
        hinted = _safe_token(context.acquisition_hints.get("board_token"))
        if hinted and str(context.source_platform_hint or "").lower() == self.platform:
            return ProviderMatch(self.platform, hinted, context.listing_url)
        for candidate in _candidate_urls(context):
            token = _greenhouse_token(candidate)
            if token:
                return ProviderMatch(self.platform, token, candidate)
        return None

    async def acquire(
        self,
        match: ProviderMatch,
        context: AcquisitionContext,
        client: AsyncJsonClient,
    ) -> AcquisitionResult:
        endpoint = f"https://boards-api.greenhouse.io/v1/boards/{match.token}/jobs?content=true"
        payload = await client.request_json(endpoint, timeout_seconds=context.timeout_seconds)
        rows = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise AcquisitionError("Greenhouse response did not contain a jobs list")

        jobs: dict[str, JobPosting] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = _validated_provider_url(row.get("absolute_url"), allowed_hosts=self._job_hosts)
            title = _plain_text(row.get("title"))
            if not url or not title:
                continue
            location = row.get("location") or {}
            job = JobPosting(
                title=title,
                job_url=url,
                apply_url=url,
                location_text=_plain_text(location.get("name") if isinstance(location, dict) else location),
                posted_date=_plain_text(row.get("updated_at")),
                summary=_plain_text(row.get("content")),
                job_reference=_plain_text(row.get("id")),
            )
            jobs[url] = job
        return AcquisitionResult(
            platform=self.platform,
            strategy="platform_api",
            discovered_urls=list(jobs),
            preextracted_jobs=jobs,
            trusted_hosts=self._job_hosts,
            complete=True,
            pages_visited=1,
            endpoint_requests=1,
            metadata={"board_token": match.token},
        )


def _lever_token(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if not parsed.hostname or str(parsed.hostname).lower() not in {"jobs.lever.co", "jobs.eu.lever.co"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    return _safe_token(parts[0]) if parts else None


class LeverProvider:
    platform = "lever"
    _job_hosts = ("jobs.lever.co", "jobs.eu.lever.co")

    def match(self, context: AcquisitionContext) -> ProviderMatch | None:
        for candidate in _candidate_urls(context):
            token = _lever_token(candidate)
            if token:
                hostname = str(urlsplit(candidate).hostname or "").lower()
                api_host = "api.eu.lever.co" if hostname == "jobs.eu.lever.co" else "api.lever.co"
                return ProviderMatch(self.platform, token, candidate, metadata={"api_host": api_host})
        hinted = _safe_token(context.acquisition_hints.get("site_token"))
        if hinted and str(context.source_platform_hint or "").lower() == self.platform:
            return ProviderMatch(self.platform, hinted, context.listing_url)
        return None

    async def acquire(
        self,
        match: ProviderMatch,
        context: AcquisitionContext,
        client: AsyncJsonClient,
    ) -> AcquisitionResult:
        api_host = match.metadata.get("api_host") or "api.lever.co"
        endpoint = f"https://{api_host}/v0/postings/{match.token}?mode=json"
        rows = await client.request_json(endpoint, timeout_seconds=context.timeout_seconds)
        if not isinstance(rows, list):
            raise AcquisitionError("Lever response was not a postings list")

        jobs: dict[str, JobPosting] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = _validated_provider_url(row.get("hostedUrl"), allowed_hosts=self._job_hosts)
            title = _plain_text(row.get("text"))
            if not url or not title:
                continue
            categories = row.get("categories") or {}
            lists = row.get("lists") if isinstance(row.get("lists"), list) else []
            list_content = " ".join(
                filter(
                    None,
                    (_plain_text(item.get("content")) for item in lists if isinstance(item, dict)),
                )
            )
            apply_url = _validated_provider_url(row.get("applyUrl"), allowed_hosts=self._job_hosts)
            job = JobPosting(
                title=title,
                job_url=url,
                apply_url=apply_url or url,
                company=_plain_text(match.token),
                location_text=_plain_text(categories.get("location") if isinstance(categories, dict) else None),
                employment_type=_plain_text(categories.get("commitment") if isinstance(categories, dict) else None),
                posted_date=_epoch_milliseconds(row.get("createdAt")),
                summary=_joined_text(row.get("descriptionPlain"), list_content, row.get("additionalPlain")),
                compensation_text=_plain_text(row.get("salaryRange")),
                job_reference=_plain_text(row.get("id")),
            )
            jobs[url] = job
        return AcquisitionResult(
            platform=self.platform,
            strategy="platform_api",
            discovered_urls=list(jobs),
            preextracted_jobs=jobs,
            trusted_hosts=self._job_hosts,
            complete=True,
            pages_visited=1,
            endpoint_requests=1,
            metadata={"site_token": match.token},
        )


def _ashby_token(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if not parsed.hostname or not _host_matches(parsed.hostname, ("jobs.ashbyhq.com",)):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    return _safe_token(parts[0]) if parts else None


class AshbyProvider:
    platform = "ashby"
    _job_hosts = ("jobs.ashbyhq.com",)

    def match(self, context: AcquisitionContext) -> ProviderMatch | None:
        hinted = _safe_token(context.acquisition_hints.get("board_token"))
        if hinted and str(context.source_platform_hint or "").lower() == self.platform:
            return ProviderMatch(self.platform, hinted, context.listing_url)
        for candidate in _candidate_urls(context):
            token = _ashby_token(candidate)
            if token:
                return ProviderMatch(self.platform, token, candidate)
        return None

    async def acquire(
        self,
        match: ProviderMatch,
        context: AcquisitionContext,
        client: AsyncJsonClient,
    ) -> AcquisitionResult:
        endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{match.token}?includeCompensation=true"
        payload = await client.request_json(endpoint, timeout_seconds=context.timeout_seconds)
        rows = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise AcquisitionError("Ashby response did not contain a jobs list")

        jobs: dict[str, JobPosting] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = _validated_provider_url(row.get("jobUrl"), allowed_hosts=self._job_hosts)
            title = _plain_text(row.get("title"))
            if not url or not title:
                continue
            apply_url = _validated_provider_url(row.get("applyUrl"), allowed_hosts=self._job_hosts)
            compensation = row.get("compensation")
            job = JobPosting(
                title=title,
                job_url=url,
                apply_url=apply_url or url,
                company=_plain_text(payload.get("organizationName") if isinstance(payload, dict) else None),
                location_text=_plain_text(row.get("location")),
                employment_type=_plain_text(row.get("employmentType")),
                posted_date=_plain_text(row.get("publishedAt")),
                summary=_plain_text(row.get("descriptionPlain") or row.get("descriptionHtml")),
                compensation_text=_plain_text(compensation),
                job_reference=_plain_text(row.get("id")),
            )
            jobs[url] = job
        return AcquisitionResult(
            platform=self.platform,
            strategy="platform_api",
            discovered_urls=list(jobs),
            preextracted_jobs=jobs,
            trusted_hosts=self._job_hosts,
            complete=True,
            pages_visited=1,
            endpoint_requests=1,
            metadata={"board_token": match.token},
        )


_WORKDAY_HOST_PATTERN = re.compile(
    r"^(?P<tenant>[a-z0-9-]+)(?:\.wd\d+)?\.myworkdayjobs\.com$",
    re.IGNORECASE,
)


def _workday_match(url: str) -> ProviderMatch | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    hostname = str(parsed.hostname or "").lower()
    host_match = _WORKDAY_HOST_PATTERN.fullmatch(hostname)
    parts = [part for part in parsed.path.split("/") if part]
    if not host_match or len(parts) < 2:
        return None
    site = _safe_token(parts[1])
    if not site:
        return None
    return ProviderMatch(
        platform="workday",
        token=site,
        listing_url=url,
        metadata={"hostname": hostname, "tenant": host_match.group("tenant"), "site": site},
    )


class WorkdayProvider:
    platform = "workday"
    page_size = 20

    def match(self, context: AcquisitionContext) -> ProviderMatch | None:
        for candidate in _candidate_urls(context):
            matched = _workday_match(candidate)
            if matched:
                return matched
        return None

    async def acquire(
        self,
        match: ProviderMatch,
        context: AcquisitionContext,
        client: AsyncJsonClient,
    ) -> AcquisitionResult:
        hostname = match.metadata["hostname"]
        tenant = match.metadata["tenant"]
        site = match.metadata["site"]
        endpoint = f"https://{hostname}/wday/cxs/{tenant}/{site}/jobs"
        discovered: list[str] = []
        seen: set[str] = set()
        offset = 0
        total: int | None = None
        complete = False
        requests = 0

        for _ in range(context.max_pages):
            payload = await client.request_json(
                endpoint,
                method="POST",
                payload={
                    "appliedFacets": {},
                    "limit": self.page_size,
                    "offset": offset,
                    "searchText": "",
                },
                timeout_seconds=context.timeout_seconds,
            )
            requests += 1
            if not isinstance(payload, dict):
                raise AcquisitionError("Workday response was not an object")
            rows = payload.get("jobPostings")
            if not isinstance(rows, list):
                raise AcquisitionError("Workday response did not contain jobPostings")
            try:
                reported_total = int(payload.get("total")) if payload.get("total") is not None else None
            except (TypeError, ValueError):
                reported_total = None
            # Some Workday tenants return a decreasing or zero `total` on later
            # pages even while returning a full page of jobs. Keep the largest
            # positive observation and never let a contradictory zero prove
            # that pagination is complete.
            if reported_total is not None and reported_total > 0:
                total = max(total or 0, reported_total)

            for row in rows:
                if not isinstance(row, dict):
                    continue
                path = str(row.get("externalPath") or "").strip()
                url = _validated_provider_url(urljoin(f"https://{hostname}/", path), allowed_hosts=(hostname,))
                if url and url not in seen:
                    seen.add(url)
                    discovered.append(url)

            offset += len(rows)
            reached_positive_total = total is not None and total > 0 and offset >= total
            if not rows or reached_positive_total or len(rows) < self.page_size:
                complete = True
                break

        return AcquisitionResult(
            platform=self.platform,
            strategy="platform_api_discovery",
            discovered_urls=discovered,
            preextracted_jobs={},
            trusted_hosts=(hostname,),
            complete=complete,
            pages_visited=requests,
            endpoint_requests=requests,
            metadata={
                "tenant": tenant,
                "site": site,
                "reported_total": total,
                "page_size": self.page_size,
            },
        )


class AcquisitionRegistry:
    """Try stable public ATS feeds, then let the caller retain browser fallback."""

    def __init__(
        self,
        *,
        client: AsyncJsonClient | None = None,
        providers: tuple[AcquisitionProvider, ...] | None = None,
    ) -> None:
        self.client = client or UrlLibJsonClient()
        self.providers = providers or (
            GreenhouseProvider(),
            LeverProvider(),
            AshbyProvider(),
            WorkdayProvider(),
        )

    async def acquire(self, context: AcquisitionContext) -> AcquisitionOutcome:
        attempts: list[dict[str, Any]] = []
        for provider in self.providers:
            matched = provider.match(context)
            if matched is None:
                continue
            try:
                result = await provider.acquire(matched, context, self.client)
            except Exception as exc:
                attempts.append(
                    {
                        "platform": provider.platform,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:500],
                    }
                )
                continue

            if not result.discovered_urls:
                attempts.append({"platform": provider.platform, "status": "empty"})
                continue
            if context.require_complete and not result.complete:
                attempts.append(
                    {
                        "platform": provider.platform,
                        "status": "incomplete",
                        "discovered_urls": len(result.discovered_urls),
                        "pages_visited": result.pages_visited,
                    }
                )
                continue
            attempts.append(
                {
                    "platform": provider.platform,
                    "status": "selected",
                    "complete": result.complete,
                    "discovered_urls": len(result.discovered_urls),
                }
            )
            return AcquisitionOutcome(selected=result, attempts=attempts)
        return AcquisitionOutcome(selected=None, attempts=attempts)


def default_acquisition_registry() -> AcquisitionRegistry:
    return AcquisitionRegistry()

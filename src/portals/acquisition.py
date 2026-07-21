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
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..schemas import JobPosting
from .route_resolver import resolve_listing_route
from .safety import PortalUrlSafetyError, validate_public_http_url


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


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attributes = {key.lower(): (value or "") for key, value in attrs}
        href = attributes.get("href", "").strip()
        if href:
            self.hrefs.append(href)


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

    async def request_text(
        self,
        url: str,
        *,
        timeout_seconds: float = 20.0,
    ) -> str: ...

    async def request_public_text(
        self,
        url: str,
        *,
        timeout_seconds: float = 20.0,
    ) -> "PublicTextResponse": ...


@dataclass(frozen=True)
class PublicTextResponse:
    """A public HTML response with a fully validated redirect evidence chain."""

    document: str
    final_url: str
    redirect_chain: tuple[str, ...] = ()


def _validated_same_host_redirect(source_url: str, destination_url: str) -> str:
    source = validate_public_http_url(source_url)
    destination = validate_public_http_url(urljoin(source.normalized_url, destination_url))
    if destination.hostname != source.hostname:
        raise AcquisitionHttpError(
            "ATS endpoint attempted an unapproved cross-host redirect"
        )
    return destination.normalized_url


class _PublicSameHostRedirectHandler(HTTPRedirectHandler):
    """Validate every redirect before urllib can make the next request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url = _validated_same_host_redirect(req.full_url, newurl)
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


class _ValidatedPublicRedirectHandler(HTTPRedirectHandler):
    """Follow only public HTTP(S) redirects and retain their exact evidence."""

    def __init__(self) -> None:
        super().__init__()
        self.redirect_chain: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = validate_public_http_url(urljoin(req.full_url, newurl))
        self.redirect_chain.append(destination.normalized_url)
        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            destination.normalized_url,
        )


def _open_public_request(request: Request, *, timeout_seconds: float):
    validate_public_http_url(request.full_url)
    return build_opener(_PublicSameHostRedirectHandler()).open(
        request,
        timeout=timeout_seconds,
    )


def _open_public_html_request(request: Request, *, timeout_seconds: float):
    """Open generic HTML while validating every redirect destination.

    Fixed ATS/API clients continue to use ``_open_public_request`` and therefore
    retain their strict same-host redirect contract. Generic public HTML may
    legitimately move to a new corporate or hosted-jobs domain; each hop is DNS
    checked before urllib is allowed to send the next request.
    """

    validate_public_http_url(request.full_url)
    redirect_handler = _ValidatedPublicRedirectHandler()
    response = build_opener(redirect_handler).open(
        request,
        timeout=timeout_seconds,
    )
    return response, tuple(redirect_handler.redirect_chain)


class UrlLibJsonClient:
    """Small dependency-free client for fixed, public ATS JSON and HTML endpoints."""

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

    async def request_text(
        self,
        url: str,
        *,
        timeout_seconds: float = 20.0,
    ) -> str:
        last_error: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await asyncio.to_thread(
                    self._request_text_once,
                    url,
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

    async def request_public_text(
        self,
        url: str,
        *,
        timeout_seconds: float = 20.0,
    ) -> PublicTextResponse:
        """Fetch generic public HTML and return its validated redirect chain."""

        last_error: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await asyncio.to_thread(
                    self._request_public_text_once,
                    url,
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
        raise AcquisitionHttpError(f"Public HTML request failed: {last_error}") from last_error

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
            with _open_public_request(request, timeout_seconds=timeout_seconds) as response:
                requested_host = str(urlsplit(url).hostname or "").lower()
                final_host = str(urlsplit(response.geturl()).hostname or "").lower()
                if final_host != requested_host:
                    raise AcquisitionHttpError("ATS endpoint redirected to a different host")
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

    def _request_text_once(self, url: str, timeout_seconds: float) -> str:
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "JobMiner/1.0 (+public-job-feed-client)",
            },
        )
        try:
            with _open_public_request(request, timeout_seconds=timeout_seconds) as response:
                requested_host = str(urlsplit(url).hostname or "").lower()
                final_host = str(urlsplit(response.geturl()).hostname or "").lower()
                if final_host != requested_host:
                    raise AcquisitionHttpError("ATS endpoint redirected to a different host")
                raw = response.read(self.max_response_bytes + 1)
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            raise AcquisitionHttpError(
                f"ATS endpoint returned HTTP {exc.code}",
                status_code=int(exc.code),
            ) from exc
        if len(raw) > self.max_response_bytes:
            raise AcquisitionHttpError("ATS endpoint response exceeded the configured size limit")
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")

    def _request_public_text_once(
        self,
        url: str,
        timeout_seconds: float,
    ) -> PublicTextResponse:
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "JobMiner/1.0 (+public-job-feed-client)",
            },
        )
        try:
            response, redirect_chain = _open_public_html_request(
                request,
                timeout_seconds=timeout_seconds,
            )
            with response:
                final = validate_public_http_url(response.geturl())
                raw = response.read(self.max_response_bytes + 1)
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            raise AcquisitionHttpError(
                f"Public HTML endpoint returned HTTP {exc.code}",
                status_code=int(exc.code),
            ) from exc
        if len(raw) > self.max_response_bytes:
            raise AcquisitionHttpError(
                "Public HTML endpoint response exceeded the configured size limit"
            )
        try:
            document = raw.decode(charset, errors="replace")
        except LookupError:
            document = raw.decode("utf-8", errors="replace")
        return PublicTextResponse(
            document=document,
            final_url=final.normalized_url,
            redirect_chain=redirect_chain,
        )


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
_WORKDAY_LOCALE_PATTERN = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.IGNORECASE)


def _workday_site_from_path(parts: list[str]) -> str | None:
    """Return the tenant site for locale-prefixed and one-segment Workday URLs."""

    if not parts:
        return None
    first = str(parts[0] or "").strip()
    if first.lower() in {"job", "wday"}:
        return None
    candidate = (
        parts[1]
        if _WORKDAY_LOCALE_PATTERN.fullmatch(first) and len(parts) >= 2
        else first
    )
    return _safe_token(candidate)


def _workday_match(url: str) -> ProviderMatch | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    hostname = str(parsed.hostname or "").lower()
    host_match = _WORKDAY_HOST_PATTERN.fullmatch(hostname)
    parts = [part for part in parsed.path.split("/") if part]
    if not host_match:
        return None
    site = _workday_site_from_path(parts)
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


def _icims_match(url: str) -> ProviderMatch | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    hostname = str(parsed.hostname or "").lower().rstrip(".")
    if hostname != "icims.com" and not hostname.endswith(".icims.com"):
        return None
    path = re.sub(r"/{2,}", "/", parsed.path or "/jobs").rstrip("/") or "/jobs"
    detail_match = re.match(r"^(?P<listing_path>/.+?/jobs|/jobs)/\d+(?:/|$)", path, flags=re.IGNORECASE)
    if detail_match:
        path = detail_match.group("listing_path")
    if not re.search(r"(?:^|/)jobs$", path, flags=re.IGNORECASE):
        path = "/jobs"
    listing_url = urlunsplit((parsed.scheme.lower() or "https", hostname, path, parsed.query, ""))
    return ProviderMatch(
        platform="icims",
        token=hostname,
        listing_url=listing_url,
        metadata={
            "hostname": hostname,
            "scheme": parsed.scheme.lower() or "https",
            "listing_path": path,
        },
    )


def _icims_job_urls(
    document: str,
    *,
    base_url: str,
    hostname: str,
    listing_path: str = "/jobs",
) -> list[str]:
    parser = _AnchorParser()
    try:
        parser.feed(html_module.unescape(str(document or "")))
    except Exception:
        pass

    hrefs = list(parser.hrefs)
    hrefs.extend(
        match.group("href")
        for match in re.finditer(
            r'''["'](?P<href>(?:https?://[^"']+)?(?:/[A-Za-z0-9_.~-]+)*/jobs/\d+(?:/[^"'?#]*)?)["']''',
            html_module.unescape(str(document or "")),
            flags=re.IGNORECASE,
        )
    )

    normalized_listing_path = re.sub(r"/{2,}", "/", listing_path or "/jobs").rstrip("/")
    detail_pattern = re.compile(
        rf"^{re.escape(normalized_listing_path)}/\d+(?:/|$)",
        flags=re.IGNORECASE,
    )
    discovered: list[str] = []
    for href in hrefs:
        candidate = urljoin(base_url, str(href or "").strip())
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if str(parsed.hostname or "").lower() != hostname:
            continue
        if not detail_pattern.match(parsed.path or ""):
            continue
        normalized = urlunsplit((parsed.scheme.lower(), hostname, parsed.path.rstrip("/"), "", ""))
        if normalized not in discovered:
            discovered.append(normalized)
    return discovered


def _icims_reported_total(document: str) -> int | None:
    plain = _plain_text(document)
    normalized = " ".join(plain.split()) if plain else ""
    patterns = (
        r"showing\s+\d+\s*(?:-|to)\s*\d+\s+of\s+([\d,]+)",
        r"\d+\s*(?:-|–|to)\s*\d+\s+of\s+([\d,]+)\s+total\s+jobs?",
        r"([\d,]+)\s+results?",
        r"([\d,]+)\s+jobs?\s+found",
        r"of\s+([\d,]+)\s+jobs?",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            total = int(match.group(1).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if total >= 0:
            return total
    return None


def _icims_embedded_listing_url(document: str, *, expected_listing_path: str) -> str | None:
    """Find an iCIMS tenant document explicitly embedded by a public wrapper."""
    normalized = html_module.unescape(str(document or "")).replace(r"\/", "/")
    expected = re.sub(r"/{2,}", "/", expected_listing_path or "/jobs").rstrip("/")
    candidates = re.findall(
        r"(?:https?:)?//[^\s\"'<>\\]+",
        normalized,
        flags=re.IGNORECASE,
    )
    for raw in candidates:
        candidate = raw.rstrip("),.;]}")
        if candidate.startswith("//"):
            candidate = f"https:{candidate}"
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        hostname = str(parsed.hostname or "").lower().rstrip(".")
        if not hostname.endswith(".i.icims.com"):
            continue
        if parsed.username is not None or parsed.password is not None:
            continue
        try:
            if parsed.port not in {None, 80, 443}:
                continue
        except ValueError:
            continue
        path = re.sub(r"/{2,}", "/", parsed.path or "/jobs").rstrip("/")
        detail = re.match(r"^(?P<listing>/.+?/jobs|/jobs)/\d+(?:/|$)", path, re.IGNORECASE)
        if detail:
            path = detail.group("listing").rstrip("/")
        if not re.search(r"(?:^|/)jobs$", path, flags=re.IGNORECASE):
            continue
        if path != expected and expected != "/jobs":
            continue
        return urlunsplit(("https", hostname, path, parsed.query, ""))
    return None


class ICIMSProvider:
    """Discover public iCIMS job-detail URLs without rendering each listing page."""

    platform = "icims"

    def match(self, context: AcquisitionContext) -> ProviderMatch | None:
        for candidate in _candidate_urls(context):
            matched = _icims_match(candidate)
            if matched:
                return matched
        return None

    async def acquire(
        self,
        match: ProviderMatch,
        context: AcquisitionContext,
        client: AsyncJsonClient,
    ) -> AcquisitionResult:
        request_text = getattr(client, "request_text", None)
        if not callable(request_text):
            raise AcquisitionError("The configured ATS client does not support HTML acquisition")

        original_hostname = match.metadata["hostname"]
        hostname = original_hostname
        scheme = match.metadata.get("scheme") or "https"
        listing_path = match.metadata.get("listing_path") or "/jobs"
        listing_url = urlunsplit((scheme, hostname, listing_path, urlsplit(match.listing_url).query, ""))
        base_url = f"{scheme}://{hostname}/"
        discovered: list[str] = []
        seen: set[str] = set()
        requests = 0
        complete = False
        reported_total: int | None = None
        first_page_size: int | None = None
        page_index = 0
        promoted_from: str | None = None

        while requests < context.max_pages:
            if listing_path.rstrip("/").lower() == "/jobs":
                endpoint_path = "/jobs/search"
                endpoint_query = urlencode(
                    {
                        "ss": "1",
                        "searchRelation": "keyword_all",
                        "pr": page_index,
                    }
                )
            else:
                endpoint_path = listing_path
                existing_query = parse_qs(urlsplit(listing_url).query, keep_blank_values=True)
                existing_query["page"] = [str(page_index + 1)]
                endpoint_query = urlencode(existing_query, doseq=True)
            endpoint = urlunsplit((scheme, hostname, endpoint_path, endpoint_query, ""))
            document = await request_text(endpoint, timeout_seconds=context.timeout_seconds)
            requests += 1

            if page_index == 0:
                embedded_listing = _icims_embedded_listing_url(
                    document,
                    expected_listing_path=listing_path,
                )
                if embedded_listing:
                    embedded = urlsplit(embedded_listing)
                    embedded_host = str(embedded.hostname or "").lower()
                    if embedded_host and embedded_host != hostname:
                        promoted_from = listing_url
                        hostname = embedded_host
                        scheme = embedded.scheme.lower() or "https"
                        listing_path = re.sub(r"/{2,}", "/", embedded.path).rstrip("/")
                        listing_url = urlunsplit(
                            (scheme, hostname, listing_path, embedded.query, "")
                        )
                        base_url = f"{scheme}://{hostname}/"
                        # The wrapper request consumed part of the bounded
                        # request budget. Fetch page one from the explicitly
                        # embedded tenant host on the next iteration.
                        continue

            page_urls = _icims_job_urls(
                document,
                base_url=base_url,
                hostname=hostname,
                listing_path=listing_path,
            )
            page_total = _icims_reported_total(document)
            if page_total is not None and page_total > 0:
                reported_total = max(reported_total or 0, page_total)

            new_urls = [url for url in page_urls if url not in seen]
            for url in new_urls:
                seen.add(url)
                discovered.append(url)

            if page_index == 0:
                first_page_size = len(page_urls) or None
            reached_total = reported_total is not None and len(discovered) >= reported_total
            exhausted_page = not page_urls or not new_urls
            short_page = (
                page_index > 0
                and first_page_size is not None
                and 0 < len(page_urls) < first_page_size
            )
            if reached_total or short_page:
                complete = True
                break
            if exhausted_page:
                # A tenant may ignore an unsupported pagination parameter and
                # repeat page one. Never let a stalled page prove completeness
                # while its rendered total says more jobs exist.
                complete = reported_total is None or len(discovered) >= reported_total
                break
            page_index += 1

        return AcquisitionResult(
            platform=self.platform,
            strategy="platform_html_discovery",
            discovered_urls=discovered,
            preextracted_jobs={},
            trusted_hosts=tuple(dict.fromkeys((original_hostname, hostname))),
            complete=complete,
            pages_visited=requests,
            endpoint_requests=requests,
            metadata={
                "hostname": hostname,
                "listing_url": listing_url,
                "listing_path": listing_path,
                "reported_total": reported_total,
                "observed_page_size": first_page_size,
                "promoted_from": promoted_from,
            },
        )


_PUBLIC_PAGINATION_KEYS = {
    "currentpage",
    "offset",
    "p",
    "page",
    "pageno",
    "pagenumber",
    "pg",
    "start",
}


def _public_pagination_url(value: str, *, listing_url: str) -> str | None:
    """Accept only rendered, same-host numeric pagination links."""
    try:
        parsed = urlsplit(urljoin(listing_url, str(value or "").strip()))
        listing = urlsplit(listing_url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if str(parsed.hostname).lower() != str(listing.hostname or "").lower():
        return None
    if re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/") != re.sub(
        r"/{2,}", "/", listing.path or "/"
    ).rstrip("/"):
        return None
    pagination_values = [
        item
        for key, item in parse_qs(parsed.query, keep_blank_values=False).items()
        if key.lower() in _PUBLIC_PAGINATION_KEYS
    ]
    if not pagination_values or not all(
        str(value).isdigit() for values in pagination_values for value in values
    ):
        return None
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


class PublicHtmlProvider:
    """Bounded server-rendered HTML discovery before the browser lane.

    This provider is intentionally selector-free. It follows only listing routes
    and numeric pagination links present in the returned document, accepts only
    strong same-host job-detail URLs, and never claims a complete inventory
    without explicit pagination or result-count evidence.
    """

    platform = "public_html"

    def match(self, context: AcquisitionContext) -> ProviderMatch | None:
        explicitly_enabled = str(
            context.acquisition_hints.get("allow_public_html") or ""
        ).strip().lower() in {"1", "true", "yes"}
        if not str(context.source_platform_hint or "").strip() and not explicitly_enabled:
            # Avoid turning a registry miss into an unsolicited network request
            # for legacy/test callers that supplied no portal detection context.
            return None
        try:
            parsed = urlsplit(context.listing_url)
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
        return ProviderMatch(
            platform=self.platform,
            token=str(parsed.hostname).lower(),
            listing_url=context.listing_url,
            metadata={"hostname": str(parsed.hostname).lower()},
        )

    async def acquire(
        self,
        match: ProviderMatch,
        context: AcquisitionContext,
        client: AsyncJsonClient,
    ) -> AcquisitionResult:
        request_text = getattr(client, "request_text", None)
        request_public_text = getattr(client, "request_public_text", None)
        if not callable(request_text) and not callable(request_public_text):
            raise AcquisitionError("The configured acquisition client does not support HTML")

        # Lazy imports keep acquisition providers independent of browser modules
        # and avoid a detector/acquisition import cycle.
        from .detector import infer_listing_url
        from .page_quality import assess_page_quality
        from .url_intelligence import assess_job_candidate_url

        queue = [match.listing_url]
        queued = {match.listing_url}
        visited: set[str] = set()
        discovered: list[str] = []
        discovered_set: set[str] = set()
        requests = 0
        active_listing_url = match.listing_url
        pagination_evidence = False
        reported_total: int | None = None
        blocked_reason: str | None = None
        trusted_hosts: set[str] = {str(match.metadata["hostname"]).lower()}
        route_resolution_chain: list[dict[str, Any]] = []
        redirect_evidence: list[dict[str, Any]] = []

        while queue and requests < context.max_pages:
            page_url = queue.pop(0)
            if page_url in visited:
                continue
            visited.add(page_url)
            if callable(request_public_text):
                public_response = await request_public_text(
                    page_url,
                    timeout_seconds=context.timeout_seconds,
                )
                if not isinstance(public_response, PublicTextResponse):
                    raise AcquisitionError(
                        "The public HTML client returned an invalid response contract"
                    )
            else:
                public_response = PublicTextResponse(
                    document=await request_text(
                        page_url,
                        timeout_seconds=context.timeout_seconds,
                    ),
                    final_url=page_url,
                )
            requests += 1
            document = public_response.document
            if callable(request_public_text):
                effective_checked = validate_public_http_url(
                    public_response.final_url or page_url
                )
                effective_page_url = effective_checked.normalized_url
                effective_host = effective_checked.hostname
            else:
                # Legacy/test text clients do not expose redirect evidence.
                # Keep their historical behavior without performing a second
                # DNS lookup, and never infer or trust a hidden redirect host.
                try:
                    parsed_page = urlsplit(page_url)
                    page_port = parsed_page.port
                except ValueError as exc:
                    raise AcquisitionError("The HTML client received an invalid public URL") from exc
                if (
                    parsed_page.scheme.lower() not in {"http", "https"}
                    or not parsed_page.hostname
                    or parsed_page.username is not None
                    or parsed_page.password is not None
                    or page_port not in {None, 80, 443}
                ):
                    raise AcquisitionError("The HTML client received an invalid public URL")
                effective_host = str(parsed_page.hostname).lower()
                effective_netloc = (
                    effective_host
                    if page_port is None
                    else f"{effective_host}:{page_port}"
                )
                effective_page_url = urlunsplit(
                    (
                        parsed_page.scheme.lower(),
                        effective_netloc,
                        parsed_page.path or "/",
                        parsed_page.query,
                        "",
                    )
                )
            trusted_hosts.add(effective_host)
            visited.add(effective_page_url)
            if not _public_pagination_url(
                effective_page_url,
                listing_url=active_listing_url,
            ):
                active_listing_url = effective_page_url
            if public_response.redirect_chain:
                redirect_hosts: list[str] = []
                for redirect_url in public_response.redirect_chain:
                    redirect_checked = validate_public_http_url(redirect_url)
                    trusted_hosts.add(redirect_checked.hostname)
                    redirect_hosts.append(redirect_checked.hostname)
                redirect_evidence.append(
                    {
                        "source_url": page_url,
                        "final_url": effective_page_url,
                        "redirect_chain": list(public_response.redirect_chain),
                        "trusted_hosts": list(dict.fromkeys(redirect_hosts)),
                    }
                )

            quality = assess_page_quality(html=document)
            if quality.blocked:
                blocked_reason = quality.reason
                continue

            resolution = resolve_listing_route(
                source_url=page_url,
                final_url=effective_page_url,
                html=document,
            )
            resolved_route = resolution.selected
            if resolved_route is not None:
                resolved_host = str(urlsplit(resolved_route.url).hostname or "").lower()
                page_host = effective_host
                if resolved_host != page_host:
                    try:
                        validate_public_http_url(resolved_route.url)
                    except PortalUrlSafetyError:
                        resolved_route = None
                    else:
                        trusted_hosts.add(resolved_host)
                if resolved_route is not None:
                    route_resolution_chain.append(
                        {
                            "source_url": page_url,
                            "selected_url": resolved_route.url,
                            "route_kind": resolved_route.route_kind,
                            "platform": resolved_route.platform,
                            "score": resolved_route.score,
                            "trusted_hosts": list(resolved_route.trusted_hosts),
                        }
                    )

            inferred = (
                resolved_route.url
                if resolved_route is not None
                else infer_listing_url(effective_page_url, document)
            )
            if inferred and _public_pagination_url(inferred, listing_url=effective_page_url):
                # A numbered page is pagination evidence, not a new canonical
                # listing route.
                inferred = None
            if inferred and inferred not in queued and inferred not in visited:
                queued.add(inferred)
                queue.insert(0, inferred)
                active_listing_url = inferred

            parser = _AnchorParser()
            try:
                parser.feed(html_module.unescape(document))
            except Exception:
                pass

            for href in parser.hrefs:
                candidate = urljoin(effective_page_url, href)
                try:
                    candidate_host = str(urlsplit(candidate).hostname or "").lower()
                except ValueError:
                    continue
                if candidate_host not in trusted_hosts:
                    continue
                assessment = assess_job_candidate_url(
                    candidate,
                    listing_url=effective_page_url,
                    platform_hint=context.source_platform_hint,
                )
                if assessment.hard_reject or assessment.score < 8:
                    continue
                if assessment.url not in discovered_set:
                    discovered_set.add(assessment.url)
                    discovered.append(assessment.url)

            for href in parser.hrefs:
                pagination_url = _public_pagination_url(
                    href,
                    listing_url=effective_page_url,
                )
                if not pagination_url:
                    continue
                pagination_evidence = True
                if pagination_url not in queued and pagination_url not in visited:
                    queued.add(pagination_url)
                    queue.append(pagination_url)

            page_total = _icims_reported_total(document)
            if page_total is not None and page_total > 0:
                reported_total = max(reported_total or 0, page_total)

        reached_total = reported_total is not None and len(discovered) >= reported_total
        exhausted_rendered_pagination = pagination_evidence and not queue
        complete = bool(discovered) and (reached_total or exhausted_rendered_pagination)
        return AcquisitionResult(
            platform=self.platform,
            strategy="public_html_discovery",
            discovered_urls=discovered,
            preextracted_jobs={},
            trusted_hosts=tuple(sorted(trusted_hosts)),
            complete=complete,
            pages_visited=requests,
            endpoint_requests=requests,
            metadata={
                "listing_url": active_listing_url,
                "reported_total": reported_total,
                "pagination_evidence": pagination_evidence,
                "request_budget_exhausted": bool(queue),
                "blocked_reason": blocked_reason,
                "route_resolution_chain": route_resolution_chain[:25],
                "redirect_evidence": redirect_evidence[:25],
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
            ICIMSProvider(),
            PublicHtmlProvider(),
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
                attempts.append(
                    {
                        "platform": provider.platform,
                        "status": "empty",
                        "complete": result.complete,
                        "pages_visited": result.pages_visited,
                        "endpoint_requests": result.endpoint_requests,
                        "trusted_hosts": list(result.trusted_hosts),
                        "metadata": dict(result.metadata),
                    }
                )
                continue
            if context.require_complete and not result.complete:
                attempts.append(
                    {
                        "platform": provider.platform,
                        "status": "incomplete",
                        "discovered_urls": len(result.discovered_urls),
                        "pages_visited": result.pages_visited,
                        "endpoint_requests": result.endpoint_requests,
                        "trusted_hosts": list(result.trusted_hosts),
                        "metadata": dict(result.metadata),
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

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterable

from pydantic import BaseModel, ConfigDict, Field

from ..portals.safety import PortalUrlSafetyError, validate_public_http_url
from ..schemas import BrowserSettings
from .dom_snapshot import DOM_SNAPSHOT_SCRIPT, FrameDomSnapshot, frame_snapshot_from_payload


BROWSER_EVIDENCE_CONTRACT_VERSION = "1.0"
_JSON_CONTENT_PATTERN = re.compile(r"(?:application|text)/(?:[a-z0-9.+-]*\+)?json", re.I)
_SENSITIVE_KEY_PATTERN = re.compile(
    r"password|passwd|secret|token|cookie|session|csrf|authorization|api[-_]?key|"
    r"access[-_]?key|private[-_]?key|resume|curriculum|social[-_]?security",
    re.I,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_json_value(
    value: Any,
    *,
    depth: int = 0,
    max_depth: int = 8,
    max_items: int = 2_000,
    max_string_chars: int = 20_000,
) -> Any:
    """Return bounded JSON evidence while dropping likely credentials and PII."""

    if depth >= max_depth:
        return f"<{type(value).__name__}:depth_limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:max_string_chars]
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in list(value.items())[:max_items]:
            rendered_key = str(key)[:300]
            if _SENSITIVE_KEY_PATTERN.search(rendered_key):
                continue
            sanitized[rendered_key] = sanitize_json_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string_chars=max_string_chars,
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [
            sanitize_json_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string_chars=max_string_chars,
            )
            for item in list(value)[:max_items]
        ]
    return str(value)[:max_string_chars]


@dataclass(frozen=True)
class BrowserEvidenceOptions:
    navigation_timeout_ms: int = 60_000
    network_idle_timeout_ms: int = 8_000
    settle_time_ms: int = 1_500
    max_frames: int = 20
    max_nodes_total: int = 20_000
    max_nodes_per_frame: int = 12_000
    max_text_chars: int = 700
    max_attribute_value_chars: int = 300
    max_attributes_per_node: int = 20
    max_inline_scripts_per_frame: int = 30
    max_inline_json_chars: int = 500_000
    max_network_json_responses: int = 120
    max_network_body_bytes: int = 2 * 1024 * 1024
    include_hidden_dom: bool = False

    def __post_init__(self) -> None:
        if not 1_000 <= self.navigation_timeout_ms <= 300_000:
            raise ValueError("navigation_timeout_ms must be between 1000 and 300000")
        if not 0 <= self.network_idle_timeout_ms <= 60_000:
            raise ValueError("network_idle_timeout_ms must be between 0 and 60000")
        if not 0 <= self.settle_time_ms <= 30_000:
            raise ValueError("settle_time_ms must be between 0 and 30000")
        if not 1 <= self.max_frames <= 100:
            raise ValueError("max_frames must be between 1 and 100")
        if not 100 <= self.max_nodes_total <= 100_000:
            raise ValueError("max_nodes_total must be between 100 and 100000")
        if not 100 <= self.max_nodes_per_frame <= 100_000:
            raise ValueError("max_nodes_per_frame must be between 100 and 100000")
        if not 0 <= self.max_network_json_responses <= 1_000:
            raise ValueError("max_network_json_responses must be between 0 and 1000")
        if not 1_024 <= self.max_network_body_bytes <= 20 * 1024 * 1024:
            raise ValueError("max_network_body_bytes must be between 1KB and 20MB")


class BrowserEvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NetworkJsonEvidence(BrowserEvidenceModel):
    response_id: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=4_096)
    method: str = Field(default="GET", max_length=20)
    status: int | None = Field(default=None, ge=100, le=599)
    resource_type: str | None = Field(default=None, max_length=100)
    content_type: str | None = Field(default=None, max_length=300)
    declared_content_length: int | None = Field(default=None, ge=0)
    captured_body_bytes: int = Field(default=0, ge=0)
    body_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    truncated: bool = False
    payload: Any | None = None
    parse_error: str | None = Field(default=None, max_length=1_000)
    skipped_reason: str | None = Field(default=None, max_length=1_000)


class BrowserEvidenceReport(BrowserEvidenceModel):
    contract_version: str = BROWSER_EVIDENCE_CONTRACT_VERSION
    requested_url: str = Field(min_length=1, max_length=4_096)
    final_url: str = Field(min_length=1, max_length=4_096)
    success: bool
    status_code: int | None = Field(default=None, ge=100, le=599)
    title: str | None = Field(default=None, max_length=1_000)
    started_at: str
    completed_at: str
    frames: list[FrameDomSnapshot] = Field(default_factory=list)
    network_json: list[NetworkJsonEvidence] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)

    @property
    def node_count(self) -> int:
        return sum(len(frame.nodes) for frame in self.frames)

    @property
    def linkless_clickable_count(self) -> int:
        return sum(len(frame.linkless_clickable_nodes) for frame in self.frames)


@dataclass
class BrowserEvidenceSession:
    """Live ephemeral page paired with its initial evidence snapshot.

    The session exists only inside the collector's async context. Node tokens
    remain live there, which lets the adaptive lane click a bounded number of
    structurally grounded cards without persisting selectors or browser state.
    """

    collector: "BrowserEvidenceCollector"
    context: Any
    page: Any
    report: BrowserEvidenceReport
    allowed_hosts: tuple[str, ...]


class BrowserEvidenceCollector:
    """Capture bounded live DOM and public JSON evidence with Playwright.

    The collector intentionally uses a new ephemeral browser context and never
    imports cookies or authentication state.  It does not solve CAPTCHAs or
    attempt to bypass protected access controls.
    """

    def __init__(
        self,
        browser_settings: BrowserSettings,
        *,
        options: BrowserEvidenceOptions | None = None,
    ) -> None:
        self.browser_settings = browser_settings
        self.options = options or BrowserEvidenceOptions()

    async def capture(
        self,
        url: str,
        *,
        allowed_hosts: Iterable[str],
    ) -> BrowserEvidenceReport:
        async with self.capture_session(url, allowed_hosts=allowed_hosts) as session:
            return session.report

    @asynccontextmanager
    async def capture_session(
        self,
        url: str,
        *,
        allowed_hosts: Iterable[str],
    ) -> AsyncIterator[BrowserEvidenceSession]:
        validated = validate_public_http_url(url, allowed_hosts=allowed_hosts)
        async with self.open_context() as context:
            page = await context.new_page()
            report = await self.capture_page(
                page,
                validated.normalized_url,
                allowed_hosts=validated.allowed_hosts,
            )
            yield BrowserEvidenceSession(
                collector=self,
                context=context,
                page=page,
                report=report,
                allowed_hosts=validated.allowed_hosts,
            )

    @asynccontextmanager
    async def open_context(self) -> AsyncIterator[Any]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required for browser evidence capture. "
                "Install project requirements and run: playwright install chromium"
            ) from exc

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self.browser_settings.headless,
            )
            context = await browser.new_context(**self._context_options())
            try:
                yield context
            finally:
                await context.close()
                await browser.close()

    def _context_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "ignore_https_errors": False,
            "java_script_enabled": True,
            "accept_downloads": False,
        }
        if self.browser_settings.geolocation_enabled:
            options["geolocation"] = {
                "latitude": self.browser_settings.geolocation_latitude,
                "longitude": self.browser_settings.geolocation_longitude,
                "accuracy": self.browser_settings.geolocation_accuracy,
            }
        return options

    async def capture_page(
        self,
        page: Any,
        url: str,
        *,
        allowed_hosts: Iterable[str],
    ) -> BrowserEvidenceReport:
        started_at = _utc_now()
        validated = validate_public_http_url(url, allowed_hosts=allowed_hosts)
        approved_hosts = validated.allowed_hosts
        pending_response_tasks: set[asyncio.Task[Any]] = set()
        network_results: dict[int, NetworkJsonEvidence] = {}
        response_sequence = 0
        errors: list[str] = []

        def on_response(response: Any) -> None:
            nonlocal response_sequence
            if self.options.max_network_json_responses <= 0:
                return
            request = getattr(response, "request", None)
            resource_type = str(getattr(request, "resource_type", "") or "").lower()
            if resource_type not in {"xhr", "fetch", "document"}:
                return
            if response_sequence >= self.options.max_network_json_responses * 5:
                return
            response_sequence += 1
            sequence = response_sequence
            task = asyncio.create_task(
                self._capture_network_response(
                    response,
                    sequence=sequence,
                    allowed_hosts=approved_hosts,
                )
            )
            pending_response_tasks.add(task)

        page.on("response", on_response)
        main_response: Any = None
        final_url = validated.normalized_url
        title: str | None = None
        frames: list[FrameDomSnapshot] = []
        try:
            try:
                main_response = await page.goto(
                    validated.normalized_url,
                    wait_until="domcontentloaded",
                    timeout=self.options.navigation_timeout_ms,
                )
                if self.options.network_idle_timeout_ms:
                    try:
                        await page.wait_for_load_state(
                            "networkidle",
                            timeout=self.options.network_idle_timeout_ms,
                        )
                    except Exception as exc:
                        errors.append(f"network_idle_timeout: {type(exc).__name__}: {exc}"[:2_000])
                if self.options.settle_time_ms:
                    await page.wait_for_timeout(self.options.settle_time_ms)
            except Exception as exc:
                errors.append(f"navigation_failed: {type(exc).__name__}: {exc}"[:2_000])

            final_url = str(getattr(page, "url", "") or validated.normalized_url)
            final_validated = validate_public_http_url(final_url, allowed_hosts=approved_hosts)
            final_url = final_validated.normalized_url
            try:
                title = str(await page.title())[:1_000] or None
            except Exception as exc:
                errors.append(f"title_failed: {type(exc).__name__}: {exc}"[:2_000])

            frames = await self._capture_frames(
                page,
                final_url=final_url,
                allowed_hosts=approved_hosts,
            )
        finally:
            try:
                page.off("response", on_response)
            except Exception:
                pass
            await self._drain_response_tasks(pending_response_tasks, network_results, errors)

        status_code = getattr(main_response, "status", None)
        network_json = [
            network_results[key]
            for key in sorted(network_results)[: self.options.max_network_json_responses]
        ]
        node_count = sum(len(frame.nodes) for frame in frames)
        inline_json_count = sum(len(frame.inline_json) for frame in frames)
        linkless_clickables = sum(len(frame.linkless_clickable_nodes) for frame in frames)
        truncated_frames = sum(frame.truncated for frame in frames)
        return BrowserEvidenceReport(
            requested_url=validated.normalized_url,
            final_url=final_url,
            success=(
                any(frame.error is None for frame in frames)
                and (status_code is None or 200 <= int(status_code) < 400)
                and not any(error.startswith("navigation_failed") for error in errors)
            ),
            status_code=status_code,
            title=title,
            started_at=started_at,
            completed_at=_utc_now(),
            frames=frames,
            network_json=network_json,
            errors=errors,
            metrics={
                "frames_seen": len(list(getattr(page, "frames", []) or [])),
                "frames_captured": len(frames),
                "nodes_captured": node_count,
                "clickable_nodes": sum(len(frame.clickable_nodes) for frame in frames),
                "linkless_clickable_nodes": linkless_clickables,
                "inline_json_documents": inline_json_count,
                "network_json_responses": len(network_json),
                "truncated_frames": truncated_frames,
                "node_budget": self.options.max_nodes_total,
            },
        )

    async def _capture_frames(
        self,
        page: Any,
        *,
        final_url: str,
        allowed_hosts: Iterable[str],
    ) -> list[FrameDomSnapshot]:
        snapshots: list[FrameDomSnapshot] = []
        remaining_nodes = self.options.max_nodes_total
        frames = list(getattr(page, "frames", []) or [])[: self.options.max_frames]
        for index, frame in enumerate(frames):
            if remaining_nodes <= 0:
                break
            frame_id = f"f{index}"
            raw_frame_url = str(getattr(frame, "url", "") or final_url)
            evidence_url = final_url if raw_frame_url.startswith("about:") else raw_frame_url
            try:
                validated_frame = validate_public_http_url(
                    evidence_url,
                    allowed_hosts=allowed_hosts,
                )
            except PortalUrlSafetyError as exc:
                snapshots.append(
                    FrameDomSnapshot(
                        frame_id=frame_id,
                        frame_url=raw_frame_url,
                        error=f"unapproved_frame: {exc}",
                    )
                )
                continue

            frame_budget = min(self.options.max_nodes_per_frame, remaining_nodes)
            try:
                payload = await frame.evaluate(
                    DOM_SNAPSHOT_SCRIPT,
                    {
                        "maxNodes": frame_budget,
                        "maxTextChars": self.options.max_text_chars,
                        "maxAttributeValueChars": self.options.max_attribute_value_chars,
                        "maxAttributesPerNode": self.options.max_attributes_per_node,
                        "maxInlineScripts": self.options.max_inline_scripts_per_frame,
                        "maxInlineJsonChars": self.options.max_inline_json_chars,
                        "includeHidden": self.options.include_hidden_dom,
                        "tokenPrefix": frame_id,
                    },
                )
                snapshot = frame_snapshot_from_payload(
                    payload,
                    frame_id=frame_id,
                    frame_url=validated_frame.normalized_url,
                    json_sanitizer=sanitize_json_value,
                )
            except Exception as exc:
                snapshot = FrameDomSnapshot(
                    frame_id=frame_id,
                    frame_url=validated_frame.normalized_url,
                    error=f"snapshot_failed: {type(exc).__name__}: {exc}"[:2_000],
                )
            snapshots.append(snapshot)
            remaining_nodes -= len(snapshot.nodes)
        return snapshots

    async def _capture_network_response(
        self,
        response: Any,
        *,
        sequence: int,
        allowed_hosts: Iterable[str],
    ) -> tuple[int, NetworkJsonEvidence] | None:
        url = str(getattr(response, "url", "") or "")
        try:
            validated = validate_public_http_url(url, allowed_hosts=allowed_hosts)
        except PortalUrlSafetyError:
            return None

        headers = await self._response_headers(response)
        content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        request = getattr(response, "request", None)
        resource_type = str(getattr(request, "resource_type", "") or "").lower() or None
        is_json = bool(_JSON_CONTENT_PATTERN.search(content_type)) or "graphql" in url.lower()
        if not is_json or resource_type not in {None, "xhr", "fetch", "document"}:
            return None

        declared_length: int | None = None
        try:
            if headers.get("content-length") is not None:
                declared_length = max(0, int(headers["content-length"]))
        except (TypeError, ValueError):
            declared_length = None
        base = {
            "response_id": f"r{sequence:04d}",
            "url": validated.normalized_url,
            "method": str(getattr(request, "method", "GET") or "GET")[:20],
            "status": getattr(response, "status", None),
            "resource_type": resource_type,
            "content_type": content_type or None,
            "declared_content_length": declared_length,
        }
        if declared_length is not None and declared_length > self.options.max_network_body_bytes:
            return sequence, NetworkJsonEvidence(
                **base,
                skipped_reason="declared response body exceeded the configured evidence limit",
            )

        try:
            raw = bytes(await response.body())
        except Exception as exc:
            return sequence, NetworkJsonEvidence(
                **base,
                parse_error=f"body_read_failed: {type(exc).__name__}: {exc}"[:1_000],
            )
        truncated = len(raw) > self.options.max_network_body_bytes
        bounded = raw[: self.options.max_network_body_bytes]
        body_hash = hashlib.sha256(raw).hexdigest()
        parsed_payload: Any | None = None
        parse_error: str | None = None
        if not truncated:
            try:
                parsed_payload = sanitize_json_value(json.loads(bounded.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                parse_error = f"{type(exc).__name__}: {exc}"[:1_000]
        return sequence, NetworkJsonEvidence(
            **base,
            captured_body_bytes=len(bounded),
            body_sha256=body_hash,
            truncated=truncated,
            payload=parsed_payload,
            parse_error=parse_error,
        )

    @staticmethod
    async def _response_headers(response: Any) -> dict[str, str]:
        all_headers = getattr(response, "all_headers", None)
        if callable(all_headers):
            value = all_headers()
            if hasattr(value, "__await__"):
                value = await value
            if isinstance(value, dict):
                return {str(key).lower(): str(item) for key, item in value.items()}
        value = getattr(response, "headers", {})
        return (
            {str(key).lower(): str(item) for key, item in value.items()}
            if isinstance(value, dict)
            else {}
        )

    @staticmethod
    async def _drain_response_tasks(
        pending_tasks: set[asyncio.Task[Any]],
        network_results: dict[int, NetworkJsonEvidence],
        errors: list[str],
    ) -> None:
        while pending_tasks:
            tasks = list(pending_tasks)
            pending_tasks.difference_update(tasks)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    errors.append(
                        f"network_capture_failed: {type(result).__name__}: {result}"[:2_000]
                    )
                elif result is not None:
                    sequence, evidence = result
                    network_results[sequence] = evidence

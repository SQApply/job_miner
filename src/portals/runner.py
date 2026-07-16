from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..blueprint_hub import BlueprintHub
from ..crawl.browser_lane import (
    build_browser_config,
    close_session,
    listing_run_config,
)
from ..infrastructure.mongo import get_mongo_database
from ..router import get_adapter
from ..schemas import JobPosting
from ..warehouse.repositories import WarehouseRepository
from .acquisition import AcquisitionContext, default_acquisition_registry
from .artifacts import result_artifacts, save_portal_artifact
from .blueprint import build_portal_blueprint
from .detector import PortalDetection, detect_portal, known_browser_ats_platform
from .orchestrator import (
    ScrapeExecutionOptions,
    ScrapeOrchestrator,
    ScrapeOrchestratorHooks,
)
from .result_evidence import detection_html, result_page_quality
from .safety import PortalUrlSafetyError, default_allowed_hosts, validate_public_http_url
from .url_intelligence import assess_llm_eligibility


@dataclass(frozen=True)
class PortalProbeResult:
    final_url: str
    detected: PortalDetection
    discovered_urls: int
    sample_job_urls: list[str]
    artifacts: list[dict[str, Any]] | None = None
    acquisition: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["detected"] = self.detected.to_dict()
        return data


@dataclass(frozen=True)
class PortalScrapeResult:
    discovered_urls: int
    attempted_urls: int
    extracted_jobs: list[JobPosting]
    rejected_urls: int
    elapsed_seconds: float
    artifacts: list[dict[str, Any]] | None = None
    detail_failures: list[dict[str, Any]] | None = None
    skipped_existing: int = 0
    rescrape_plan: dict[str, Any] | None = None
    lifecycle_reconcile: dict[str, Any] | None = None
    discovered_job_urls: list[str] | None = None
    acquisition: dict[str, Any] | None = None

    def metrics(self) -> dict[str, Any]:
        return {
            "discovered_urls": self.discovered_urls,
            "attempted_urls": self.attempted_urls,
            "skipped_existing": self.skipped_existing,
            "extracted_jobs": len(self.extracted_jobs),
            "rejected_urls": self.rejected_urls,
            "elapsed_seconds": self.elapsed_seconds,
            "artifact_count": len(self.artifacts or []),
            "detail_failure_count": len(self.detail_failures or []),
            "detail_failures": (self.detail_failures or [])[:25],
            "rescrape_plan": self.rescrape_plan or {},
            "lifecycle_reconcile": self.lifecycle_reconcile or {},
            "acquisition": self.acquisition or {},
        }


def _result_html(result: Any) -> str:
    primary = ""
    for field in ("cleaned_html", "html", "markdown"):
        value = getattr(result, field, None)
        if value:
            primary = str(value)
            break
    return detection_html(result, primary)


def _result_text(result: Any) -> str:
    for field in ("markdown", "fit_markdown", "text"):
        value = getattr(result, field, None)
        if value:
            return str(value)
    return ""


def _result_final_url(result: Any, fallback: str) -> str:
    for field in ("url", "final_url", "redirected_url"):
        value = getattr(result, field, None)
        if value:
            return str(value)
    return fallback


def _same_site(first: str, second: str) -> bool:
    first_parts = str(first or "").lower().rstrip(".").split(".")
    second_parts = str(second or "").lower().rstrip(".").split(".")
    return len(first_parts) >= 2 and len(second_parts) >= 2 and first_parts[-2:] == second_parts[-2:]


def _safe_discovered_urls(urls: list[str], allowed_hosts: list[str]) -> tuple[list[str], int]:
    valid: list[str] = []
    rejected = 0
    seen: set[str] = set()
    for url in urls:
        try:
            checked = validate_public_http_url(url, allowed_hosts=allowed_hosts)
        except PortalUrlSafetyError:
            rejected += 1
            continue
        if checked.normalized_url not in seen:
            seen.add(checked.normalized_url)
            valid.append(checked.normalized_url)
    return valid, rejected


def _portal_acquisition_hints(portal: dict[str, Any]) -> dict[str, str]:
    metadata = portal.get("metadata") or {}
    last_probe = metadata.get("last_probe") if isinstance(metadata, dict) else {}
    detected = last_probe.get("detected") if isinstance(last_probe, dict) else {}
    hints = detected.get("acquisition_hints") if isinstance(detected, dict) else {}
    if not isinstance(hints, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in hints.items()
        if str(key).strip() and str(value).strip()
    }


def _runtime_portal_allowed_hosts(portal: dict[str, Any]) -> list[str]:
    """Add only a safety-checked canonical host learned during probing."""
    configured = [str(value) for value in (portal.get("allowed_hosts") or []) if str(value).strip()]
    canonical_url = str(portal.get("canonical_listing_url") or portal.get("listing_url") or "").strip()
    provided_url = str(portal.get("listing_url") or canonical_url).strip()
    if not canonical_url:
        return configured
    canonical = validate_public_http_url(canonical_url)
    provided = validate_public_http_url(provided_url)
    canonical_is_known_ats = bool(known_browser_ats_platform(canonical.normalized_url))
    if canonical.hostname == provided.hostname or _same_site(canonical.hostname, provided.hostname) or canonical_is_known_ats:
        configured.extend(default_allowed_hosts(canonical.hostname))
    return list(dict.fromkeys(configured))


def _plan_portal_detail_rescrape(
    *,
    target_id: str,
    run_session_id: str,
    safe_urls: list[str],
    incremental_rescrape: bool,
    force_detail_refresh: bool,
    deep_refresh_days: int,
) -> tuple[list[str], dict[str, Any]]:
    if not incremental_rescrape:
        return safe_urls, {
            "status": "disabled",
            "discovered_urls": len(safe_urls),
            "urls_to_extract_count": len(safe_urls),
            "known_skipped": 0,
        }
    try:
        repo = WarehouseRepository(get_mongo_database())
        plan = repo.plan_detail_rescrape(
            target_id=target_id,
            run_session_id=run_session_id,
            discovered_urls=safe_urls,
            force_detail_refresh=force_detail_refresh,
            deep_refresh_days=deep_refresh_days,
        )
        return list(plan.get("urls_to_extract") or []), {**plan, "status": "planned"}
    except Exception as exc:
        return safe_urls, {
            "status": "fallback_full_scrape",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "discovered_urls": len(safe_urls),
            "urls_to_extract_count": len(safe_urls),
        }


async def probe_portal(*, root: Path, portal: dict[str, Any], run_session_id: str) -> PortalProbeResult:
    """Open one listing page, verify redirect safety, detect a profile, and try discovery."""
    from crawl4ai import AsyncWebCrawler

    hub = BlueprintHub(root)
    settings = hub.system.browser
    listing_url = str(portal.get("listing_url") or "")
    checked_url = validate_public_http_url(listing_url, allowed_hosts=portal.get("allowed_hosts") or None)

    probe_session_id = f"portal_probe_{str(portal['id']).replace('-', '')[:16]}"
    artifacts: list[dict[str, Any]] = []

    # Direct ATS URLs do not need a browser just to prove that their public feed
    # is usable. This also lets onboarding succeed when the branded HTML page is
    # protected but the provider's documented public jobs endpoint is available.
    url_detection = detect_portal(listing_url=checked_url.normalized_url, html="", text_content="")
    direct_outcome = await default_acquisition_registry().acquire(
        AcquisitionContext(
            listing_url=checked_url.normalized_url,
            source_platform_hint=url_detection.source_platform,
            acquisition_hints=url_detection.acquisition_hints,
            max_pages=max(1, int(portal.get("max_pages_per_run") or 3)),
            timeout_seconds=min(30.0, max(5.0, float(portal.get("crawl_timeout_seconds") or 30))),
            require_complete=False,
        )
    )
    if direct_outcome.selected is not None:
        selected = direct_outcome.selected
        effective_checked = checked_url
        selected_listing = str(selected.metadata.get("listing_url") or "").strip()
        if selected_listing:
            candidate_checked = validate_public_http_url(selected_listing)
            if candidate_checked.hostname not in selected.trusted_hosts:
                raise RuntimeError(
                    "Acquisition returned a listing host outside its trusted-host contract"
                )
            effective_checked = candidate_checked
        effective_detection = detect_portal(
            listing_url=effective_checked.normalized_url,
            html="",
            text_content="",
        )
        safe_urls, _ = _safe_discovered_urls(
            list(selected.discovered_urls),
            [*checked_url.allowed_hosts, *selected.trusted_hosts],
        )
        artifact = save_portal_artifact(
            root=root,
            portal_id=str(portal["id"]),
            run_session_id=run_session_id,
            category="portal_crawl",
            artifact_type="discovered_urls",
            name="probe_discovered_urls",
            content={"urls": safe_urls[:100], "total": len(safe_urls)},
            mime_type="application/json",
            extension=".json",
            metadata={"stage": "probe", "acquisition": direct_outcome.metrics()},
        )
        if artifact:
            artifacts.append(artifact)
        return PortalProbeResult(
            final_url=effective_checked.normalized_url,
            detected=effective_detection,
            discovered_urls=len(safe_urls),
            sample_job_urls=safe_urls[:5],
            artifacts=artifacts,
            acquisition=direct_outcome.metrics(),
        )

    browser_config = build_browser_config(settings)
    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(
            url=checked_url.normalized_url,
            config=listing_run_config(settings, probe_session_id, None),
        )
        artifacts.extend(result_artifacts(
            root=root,
            portal_id=str(portal["id"]),
            run_session_id=run_session_id,
            prefix="probe_listing",
            result=result,
            metadata={"url": checked_url.normalized_url},
        ))
        if not getattr(result, "success", False):
            raise RuntimeError(f"Portal listing probe failed: {getattr(result, 'error_message', 'unknown error')}")

        final_url = _result_final_url(result, checked_url.normalized_url)
        final_checked = validate_public_http_url(final_url, allowed_hosts=checked_url.allowed_hosts)
        page_quality = result_page_quality(result)
        html = str(getattr(result, "html", "") or "") if page_quality.blocked else _result_html(result)
        detection = detect_portal(listing_url=final_checked.normalized_url, html=html, text_content=_result_text(result))

        inferred_outcome = None
        hinted_url = str(detection.acquisition_hints.get("listing_url") or "").strip()
        if hinted_url:
            hinted_checked = validate_public_http_url(hinted_url)
            if (
                hinted_checked.hostname != final_checked.hostname
                and not _same_site(hinted_checked.hostname, final_checked.hostname)
                and not known_browser_ats_platform(hinted_checked.normalized_url)
            ):
                raise RuntimeError(
                    f"Rendered listing linked to unclassified host {hinted_checked.hostname}"
                )
            if hinted_checked.normalized_url != final_checked.normalized_url:
                inferred_outcome = await default_acquisition_registry().acquire(
                    AcquisitionContext(
                        listing_url=hinted_checked.normalized_url,
                        source_platform_hint=detection.source_platform,
                        acquisition_hints=detection.acquisition_hints,
                        max_pages=max(1, int(portal.get("max_pages_per_run") or 3)),
                        timeout_seconds=min(
                            30.0,
                            max(5.0, float(portal.get("crawl_timeout_seconds") or 30)),
                        ),
                        require_complete=False,
                    )
                )
                if inferred_outcome.selected is not None:
                    final_checked = hinted_checked
                else:
                    hinted_result = await crawler.arun(
                        url=hinted_checked.normalized_url,
                        config=listing_run_config(settings, f"{probe_session_id}_listing", None),
                    )
                    artifacts.extend(result_artifacts(
                        root=root,
                        portal_id=str(portal["id"]),
                        run_session_id=run_session_id,
                        prefix="probe_inferred_listing",
                        result=hinted_result,
                        metadata={"url": hinted_checked.normalized_url},
                    ))
                    if not getattr(hinted_result, "success", False):
                        raise RuntimeError(
                            "Inferred jobs-listing route failed: "
                            f"{getattr(hinted_result, 'error_message', 'unknown error')}"
                        )
                    hinted_quality = result_page_quality(hinted_result)
                    hinted_final = validate_public_http_url(
                        _result_final_url(hinted_result, hinted_checked.normalized_url)
                    )
                    if (
                        hinted_final.hostname != final_checked.hostname
                        and not _same_site(hinted_final.hostname, final_checked.hostname)
                        and not known_browser_ats_platform(hinted_final.normalized_url)
                    ):
                        raise RuntimeError(
                            f"Inferred listing redirected to unclassified host {hinted_final.hostname}"
                        )
                    final_checked = hinted_final
                    detection = detect_portal(
                        listing_url=final_checked.normalized_url,
                        html=(
                            str(getattr(hinted_result, "html", "") or "")
                            if hinted_quality.blocked
                            else _result_html(hinted_result)
                        ),
                        text_content=_result_text(hinted_result),
                    )

        discovered_urls = 0
        samples: list[str] = []
        acquisition: dict[str, Any] = {
            "selected": False,
            "strategy": "browser_fallback",
            "reason": "portal_blocked" if detection.blocked else "no_provider_selected",
            "attempts": [],
        }
        if not detection.blocked:
            portal_for_profile = dict(portal)
            portal_for_profile["canonical_listing_url"] = final_checked.normalized_url
            portal_for_profile["profile_name"] = detection.profile_name
            portal_for_profile["allowed_hosts"] = list(
                dict.fromkeys(
                    [
                        *(portal.get("allowed_hosts") or []),
                        *checked_url.allowed_hosts,
                        *default_allowed_hosts(final_checked.hostname),
                    ]
                )
            )
            blueprint = build_portal_blueprint(root=root, portal=portal_for_profile, run_session_id=run_session_id)
            outcome = inferred_outcome
            if outcome is None or outcome.selected is None:
                outcome = await default_acquisition_registry().acquire(
                    AcquisitionContext(
                        listing_url=blueprint.listing.page_url,
                        source_platform_hint=detection.source_platform,
                        acquisition_hints=detection.acquisition_hints,
                        max_pages=max(1, int(portal.get("max_pages_per_run") or 3)),
                        timeout_seconds=min(30.0, max(5.0, float(portal.get("crawl_timeout_seconds") or 30))),
                        require_complete=False,
                    )
                )
            acquisition = outcome.metrics()
            if outcome.selected is not None:
                urls = list(outcome.selected.discovered_urls)
                approved_hosts = [*blueprint.allowed_hosts, *outcome.selected.trusted_hosts]
            else:
                adapter = get_adapter(blueprint)
                urls = await adapter.discover_job_urls(crawler, blueprint, hub.system)
                approved_hosts = list(blueprint.allowed_hosts)
            safe_urls, _ = _safe_discovered_urls(urls, approved_hosts)
            discovered_urls = len(safe_urls)
            samples = safe_urls[:5]
            artifact = save_portal_artifact(
                root=root,
                portal_id=str(portal["id"]),
                run_session_id=run_session_id,
                category="portal_crawl",
                artifact_type="discovered_urls",
                name="probe_discovered_urls",
                content={"urls": safe_urls[:100], "total": len(safe_urls)},
                mime_type="application/json",
                extension=".json",
                metadata={"stage": "probe", "acquisition": acquisition},
            )
            if artifact:
                artifacts.append(artifact)
        else:
            await close_session(crawler, probe_session_id)

    return PortalProbeResult(
        final_url=final_checked.normalized_url,
        detected=detection,
        discovered_urls=discovered_urls,
        sample_job_urls=samples,
        artifacts=artifacts,
        acquisition=acquisition,
    )


async def scrape_portal(
    *,
    root: Path,
    portal: dict[str, Any],
    run_session_id: str,
    max_jobs: int,
    incremental_rescrape: bool = True,
    force_detail_refresh: bool = False,
    deep_refresh_days: int = 14,
    reconcile_lifecycle: bool = False,
) -> PortalScrapeResult:
    """Run a bounded browser + LLM scrape for an already-probed portal.

    This function discovers listing URLs and extracts details. It intentionally
    does not deactivate missing jobs. Lifecycle reconciliation is a post-ingestion
    decision made by the task after the scrape is known to be successful.
    """
    hub = BlueprintHub(root)
    settings = hub.system.browser
    portal_for_scrape = dict(portal)
    portal_for_scrape["allowed_hosts"] = _runtime_portal_allowed_hosts(portal)
    blueprint = build_portal_blueprint(root=root, portal=portal_for_scrape, run_session_id=run_session_id)

    def discovery_artifacts(safe_urls: list[str]) -> list[dict[str, Any]]:
        discovered_artifact = save_portal_artifact(
            root=root,
            portal_id=str(portal["id"]),
            run_session_id=run_session_id,
            category="portal_crawl",
            artifact_type="discovered_urls",
            name="scrape_discovered_urls",
            content={"urls": safe_urls[:500], "total": len(safe_urls)},
            mime_type="application/json",
            extension=".json",
            metadata={"stage": "scrape"},
        )
        return [discovered_artifact] if discovered_artifact else []

    def failure_artifacts(index: int, job_url: str, attempt: int, result: Any) -> list[dict[str, Any]]:
        return result_artifacts(
            root=root,
            portal_id=str(portal["id"]),
            run_session_id=run_session_id,
            prefix=f"detail_{index}_failed",
            result=result,
            metadata={"job_url": job_url, "attempt": attempt},
        )

    orchestrator = ScrapeOrchestrator(
        blueprint=blueprint,
        system_config=hub.system,
        run_session_id=run_session_id,
        instruction=blueprint.detail.instruction,
        source_platform_hint=str(portal.get("source_platform") or "").strip() or None,
        acquisition_hints=_portal_acquisition_hints(portal),
    )
    orchestration = await orchestrator.run(
        options=ScrapeExecutionOptions(
            detail_concurrency=max(1, min(int(settings.detail_extraction_concurrency), 5)),
            detail_retry_attempts=max(0, min(int(portal.get("detail_retry_attempts") or 0), 5)),
            requests_per_minute=max(1, int(portal.get("request_rate_limit_per_minute") or 1)),
            max_jobs=max(1, int(max_jobs)),
            fail_on_zero_discovery=True,
            session_prefix=f"portal_detail_{str(portal['id']).replace('-', '')[:12]}",
            prefer_platform_api=True,
            max_acquisition_pages=max(1, int(portal.get("max_pages_per_run") or 50)),
            acquisition_timeout_seconds=min(
                30.0,
                max(5.0, float(portal.get("crawl_timeout_seconds") or 30)),
            ),
            require_complete_acquisition=bool(reconcile_lifecycle),
            prefer_static_detail_html=True,
        ),
        hooks=ScrapeOrchestratorHooks(
            normalize_discovered_urls=lambda urls: _safe_discovered_urls(
                urls,
                list(blueprint.allowed_hosts),
            ),
            normalize_acquired_urls=lambda urls, trusted_hosts: _safe_discovered_urls(
                urls,
                [*blueprint.allowed_hosts, *trusted_hosts],
            ),
            plan_detail_urls=lambda safe_urls: _plan_portal_detail_rescrape(
                target_id=str(portal["target_id"]),
                run_session_id=run_session_id,
                safe_urls=safe_urls,
                incremental_rescrape=incremental_rescrape,
                force_detail_refresh=force_detail_refresh,
                deep_refresh_days=deep_refresh_days,
            ),
            validate_detail_url=lambda url: validate_public_http_url(
                url,
                allowed_hosts=blueprint.allowed_hosts,
            ).normalized_url,
            is_rejected_error=lambda exc: isinstance(exc, PortalUrlSafetyError),
            should_attempt_llm=assess_llm_eligibility,
            on_discovery_artifacts=discovery_artifacts,
            on_failure_artifacts=failure_artifacts,
        )
    )

    lifecycle_reconcile = {"status": "pending_post_ingestion" if reconcile_lifecycle else "disabled"}
    return PortalScrapeResult(
        discovered_urls=len(orchestration.discovered_job_urls),
        attempted_urls=len(orchestration.attempted_job_urls),
        extracted_jobs=orchestration.jobs,
        rejected_urls=orchestration.rejected_urls,
        elapsed_seconds=orchestration.elapsed_seconds,
        artifacts=orchestration.artifacts,
        detail_failures=orchestration.detail_failures,
        skipped_existing=orchestration.skipped_existing,
        rescrape_plan=orchestration.rescrape_plan,
        lifecycle_reconcile=lifecycle_reconcile,
        discovered_job_urls=orchestration.discovered_job_urls,
        acquisition=orchestration.acquisition,
    )

from __future__ import annotations

from urllib.parse import urlparse

from ..crawl.browser_lane import interaction_run_config
from ..crawl.interactions import (
    build_back_to_listing_js,
    build_detail_button_click_js,
    build_detail_button_wait_js,
)
from ..utils import unique_keep_order
from .paginated_anchor import PaginatedAnchorAdapter


class DetailButtonCaptureAdapter(PaginatedAnchorAdapter):
    """Adapter for listings where detail URLs must be captured by clicking Details buttons."""

    def _detail_click_js(self, listing, index: int) -> str:
        legacy_template = listing.detail_click_js_template
        if legacy_template:
            return legacy_template.replace("__INDEX__", str(index))
        if listing.detail_button.click_js_template:
            return listing.detail_button.click_js_template.replace("__INDEX__", str(index))
        return build_detail_button_click_js(
            index=index,
            selector=listing.detail_button.selector,
            text_patterns=listing.detail_button.text_patterns or listing.detail_text_patterns,
            exact_text=listing.detail_button.exact_text,
        )

    def _detail_wait_js(self, listing) -> str:
        return (
            listing.detail_wait_for
            or listing.detail_button.wait_for
            or build_detail_button_wait_js()
        )

    def _back_js(self, listing) -> str:
        return (
            listing.back_to_listing_js
            or listing.detail_button.back_to_listing_js
            or build_back_to_listing_js(listing.page_url)
        )

    def _back_wait_for(self, listing) -> str:
        return (
            listing.back_to_listing_wait_for
            or listing.detail_button.back_to_listing_wait_for
            or listing.initial_wait_for
            or "js:() => document.body && document.body.innerText.length > 0"
        )

    async def _collect_by_clicking_detail_buttons(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
        settings = system_config.browser
        listing = blueprint.listing
        urls: list[str] = []

        for idx in range(listing.detail_button.max_buttons):
            try:
                result = await crawler.arun(
                    url=listing.page_url,
                    config=interaction_run_config(
                        settings,
                        listing.session_id,
                        self._detail_click_js(listing, idx),
                        self._detail_wait_js(listing),
                    ),
                )
            except Exception as exc:
                self._log(
                    session_logger,
                    "detail_button_click_exception",
                    page_url=listing.page_url,
                    button_index=idx,
                    error_message=str(exc),
                )
                break

            if not result.success:
                self._log(
                    session_logger,
                    "detail_button_click_failed",
                    page_url=listing.page_url,
                    button_index=idx,
                    error_message=result.error_message,
                )
                break

            final_url = (getattr(result, "url", None) or getattr(result, "final_url", None) or "").strip()
            if final_url and final_url.rstrip("/") != listing.page_url.rstrip("/"):
                parsed = urlparse(final_url)
                if parsed.netloc in blueprint.allowed_hosts:
                    captured = final_url.rstrip("/")
                    urls.append(captured)
                    self._log(
                        session_logger,
                        "detail_button_captured",
                        page_url=listing.page_url,
                        button_index=idx,
                        captured_url=captured,
                    )

            try:
                back_result = await crawler.arun(
                    url=listing.page_url,
                    config=interaction_run_config(
                        settings,
                        listing.session_id,
                        self._back_js(listing),
                        self._back_wait_for(listing),
                    ),
                )
                if not back_result.success:
                    self._log(
                        session_logger,
                        "back_to_listing_failed",
                        page_url=listing.page_url,
                        button_index=idx,
                        error_message=back_result.error_message,
                    )
                    break
            except Exception as exc:
                self._log(
                    session_logger,
                    "back_to_listing_exception",
                    page_url=listing.page_url,
                    button_index=idx,
                    error_message=str(exc),
                )
                break

        return unique_keep_order(urls)

    async def _collect_current_page_urls(self, crawler, blueprint, system_config, session_logger=None):
        return await self._collect_by_clicking_detail_buttons(
            crawler,
            blueprint,
            system_config,
            session_logger=session_logger,
        )

    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
        settings = system_config.browser
        listing = blueprint.listing

        initial_result = await self._open_listing(crawler, settings, listing, session_logger=session_logger)
        await self._accept_cookies(crawler, settings, listing)
        latest_result = await self._refresh_listing(crawler, settings, listing) or initial_result

        from ..crawl.link_collector import page_has_multiple_pages, page_has_pagination

        urls = await self._collect_current_page_urls(crawler, blueprint, system_config, session_logger=session_logger)
        initial_count = len(urls)
        has_pagination = page_has_pagination(latest_result)
        has_multiple_pages = page_has_multiple_pages(latest_result)

        self._log(
            session_logger,
            "listing_initial_complete",
            page_url=listing.page_url,
            discovered_urls=initial_count,
            has_pagination=has_pagination,
            has_multiple_pages=has_multiple_pages,
            detail_capture_mode="click_buttons",
        )

        stable_rounds = 0
        turns = 0
        any_growth = False

        while listing.pagination.enabled and turns < listing.pagination.max_turns:
            turns += 1
            self._log(
                session_logger,
                "pagination_click_start",
                page_url=listing.page_url,
                page_turn=turns,
                current_discovered_urls=len(urls),
                pagination_type=listing.pagination.type,
            )

            try:
                result = await crawler.arun(
                    url=listing.page_url,
                    config=interaction_run_config(
                        settings,
                        listing.session_id,
                        self._pagination_click_js(listing),
                        self._pagination_wait_js(listing),
                    ),
                )
            except Exception as exc:
                stable_rounds += 1
                self._log(
                    session_logger,
                    "pagination_click_exception",
                    page_url=listing.page_url,
                    page_turn=turns,
                    stable_rounds=stable_rounds,
                    error_message=str(exc),
                )
                if stable_rounds >= listing.pagination.stop_after_stable_rounds:
                    break
                continue

            if not result.success:
                stable_rounds += 1
                self._log(
                    session_logger,
                    "pagination_click_failed",
                    page_url=listing.page_url,
                    page_turn=turns,
                    stable_rounds=stable_rounds,
                    error_message=result.error_message,
                )
                if stable_rounds >= listing.pagination.stop_after_stable_rounds:
                    break
                continue

            latest_result = result
            new_urls = await self._collect_current_page_urls(crawler, blueprint, system_config, session_logger=session_logger)
            merged = unique_keep_order(urls + new_urls)

            if len(merged) > len(urls):
                previous_count = len(urls)
                discovered_new_urls = merged[previous_count:]
                urls = merged
                stable_rounds = 0
                any_growth = True
                self._log(
                    session_logger,
                    "pagination_click_growth",
                    page_url=listing.page_url,
                    page_turn=turns,
                    previous_discovered_urls=previous_count,
                    new_urls_found=len(discovered_new_urls),
                    total_discovered_urls=len(urls),
                    discovered_urls=discovered_new_urls,
                )
            else:
                stable_rounds += 1
                self._log(
                    session_logger,
                    "pagination_click_no_growth",
                    page_url=listing.page_url,
                    page_turn=turns,
                    stable_rounds=stable_rounds,
                    discovered_urls=len(urls),
                )
                if stable_rounds >= listing.pagination.stop_after_stable_rounds:
                    break

        await self._close_listing(crawler, listing)

        if listing.pagination.enabled and has_multiple_pages and initial_count > 0 and not any_growth:
            message = (
                "Pagination detected on listing page, but no new URLs were discovered "
                "after pagination attempts. Aborting before detail extraction."
            )
            self._log(
                session_logger,
                "pagination_stalled_abort",
                page_url=listing.page_url,
                discovered_urls=len(urls),
                pagination_turns=turns,
                message=message,
            )
            raise RuntimeError(message)

        self._log(
            session_logger,
            "listing_complete",
            page_url=listing.page_url,
            discovered_urls=len(urls),
            pagination_turns=turns,
        )
        return urls

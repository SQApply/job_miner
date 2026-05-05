from __future__ import annotations

from ..crawl.browser_lane import interaction_run_config
from ..crawl.interactions import build_anchor_pagination_click_js, build_anchor_pagination_wait_js
from ..crawl.link_collector import page_has_multiple_pages, page_has_pagination
from ..utils import unique_keep_order
from .base import BaseAdapter
from .listing_base import ListingAdapterMixin


class PaginatedAnchorAdapter(ListingAdapterMixin, BaseAdapter):
    """Generic adapter for listings with clickable page numbers or Next links."""

    def _pagination_click_js(self, listing) -> str:
        if listing.pagination.click_js_override:
            return listing.pagination.click_js_override
        if listing.pagination.click_js:
            return listing.pagination.click_js
        return build_anchor_pagination_click_js(listing.pagination.next_text_patterns)

    def _pagination_wait_js(self, listing) -> str:
        if listing.pagination.wait_for_js_override:
            return listing.pagination.wait_for_js_override
        if listing.pagination.wait_for_js:
            return listing.pagination.wait_for_js
        return build_anchor_pagination_wait_js()

    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
        settings = system_config.browser
        listing = blueprint.listing

        initial_result = await self._open_listing(crawler, settings, listing, session_logger=session_logger)
        await self._accept_cookies(crawler, settings, listing)
        latest_result = await self._refresh_listing(crawler, settings, listing) or initial_result

        urls = self._collect_links(latest_result, blueprint)
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
            pagination_type=listing.pagination.type,
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
            merged = unique_keep_order(urls + self._collect_links(latest_result, blueprint))

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
                for discovered_url in discovered_new_urls:
                    self._log(
                        session_logger,
                        "pagination_discovered_job_url",
                        page_url=listing.page_url,
                        page_turn=turns,
                        job_url=discovered_url,
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

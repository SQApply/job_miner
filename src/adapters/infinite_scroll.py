from __future__ import annotations

from ..crawl.browser_lane import interaction_run_config
from ..crawl.interactions import build_infinite_scroll_js, build_infinite_scroll_wait_js
from ..utils import unique_keep_order
from .base import BaseAdapter
from .listing_base import ListingAdapterMixin


class InfiniteScrollAdapter(ListingAdapterMixin, BaseAdapter):
    """Generic adapter for pages that reveal additional jobs while scrolling."""

    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
        settings = system_config.browser
        listing = blueprint.listing

        initial_result = await self._open_listing(crawler, settings, listing, session_logger=session_logger)
        await self._accept_cookies(crawler, settings, listing)
        latest_result = await self._refresh_listing(crawler, settings, listing) or initial_result

        urls = self._collect_links(latest_result, blueprint)
        self._log(
            session_logger,
            "listing_initial_complete",
            page_url=listing.page_url,
            discovered_urls=len(urls),
            adapter="infinite_scroll",
        )

        stable_rounds = 0
        scroll_index = 0
        while listing.infinite_scroll.enabled and scroll_index < listing.infinite_scroll.max_scrolls:
            scroll_index += 1
            self._log(
                session_logger,
                "infinite_scroll_start",
                page_url=listing.page_url,
                scroll_index=scroll_index,
                discovered_urls=len(urls),
            )
            try:
                result = await crawler.arun(
                    url=listing.page_url,
                    config=interaction_run_config(
                        settings,
                        listing.session_id,
                        build_infinite_scroll_js(),
                        listing.infinite_scroll.wait_for_js or build_infinite_scroll_wait_js(),
                    ),
                )
            except Exception as exc:
                self._log(
                    session_logger,
                    "infinite_scroll_exception",
                    page_url=listing.page_url,
                    scroll_index=scroll_index,
                    error_message=str(exc),
                )
                break

            if not result.success:
                stable_rounds += 1
                self._log(
                    session_logger,
                    "infinite_scroll_failed",
                    page_url=listing.page_url,
                    scroll_index=scroll_index,
                    stable_rounds=stable_rounds,
                    error_message=result.error_message,
                )
                if stable_rounds >= listing.infinite_scroll.stop_after_stable_rounds:
                    break
                continue

            latest_result = result
            merged = unique_keep_order(urls + self._collect_links(latest_result, blueprint))
            if len(merged) > len(urls):
                urls = merged
                stable_rounds = 0
                self._log(
                    session_logger,
                    "infinite_scroll_growth",
                    page_url=listing.page_url,
                    scroll_index=scroll_index,
                    discovered_urls=len(urls),
                )
            else:
                stable_rounds += 1
                self._log(
                    session_logger,
                    "infinite_scroll_no_growth",
                    page_url=listing.page_url,
                    scroll_index=scroll_index,
                    stable_rounds=stable_rounds,
                    discovered_urls=len(urls),
                )
                if stable_rounds >= listing.infinite_scroll.stop_after_stable_rounds:
                    break

        await self._close_listing(crawler, listing)
        self._log(
            session_logger,
            "listing_complete",
            page_url=listing.page_url,
            discovered_urls=len(urls),
            scroll_rounds=scroll_index,
        )
        return urls

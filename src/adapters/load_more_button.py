from __future__ import annotations

from ..crawl.browser_lane import load_more_run_config
from ..crawl.interactions import build_load_more_click_js, build_load_more_wait_js
from ..crawl.link_collector import page_has_load_more
from ..utils import unique_keep_order
from .base import BaseAdapter
from .listing_base import ListingAdapterMixin


class LoadMoreButtonAdapter(ListingAdapterMixin, BaseAdapter):
    """Generic adapter for listing pages expanded by a Load More / View More button."""

    def _click_js(self, listing) -> str:
        if listing.load_more.click_js_override:
            return listing.load_more.click_js_override
        if listing.load_more.click_js:
            return listing.load_more.click_js
        return build_load_more_click_js(
            href_contains=listing.item_href_contains,
            button_text_patterns=listing.load_more.button_text_patterns,
        )

    def _wait_js(self, listing) -> str:
        if listing.load_more.wait_for_js_override:
            return listing.load_more.wait_for_js_override
        if listing.load_more.wait_for_js:
            return listing.load_more.wait_for_js
        return build_load_more_wait_js(
            href_contains=listing.item_href_contains,
            button_text_patterns=listing.load_more.button_text_patterns,
        )

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
            has_load_more=page_has_load_more(latest_result),
        )

        stable_rounds = 0
        click_index = 0
        max_stable = max(1, int(listing.load_more.stop_after_stable_rounds or 1))

        while listing.load_more.enabled and click_index < listing.load_more.max_clicks:
            if click_index > 0 and not page_has_load_more(latest_result):
                self._log(
                    session_logger,
                    "load_more_button_gone",
                    page_url=listing.page_url,
                    click_index=click_index,
                    discovered_urls=len(urls),
                )
                break

            click_index += 1
            self._log(
                session_logger,
                "load_more_click_start",
                page_url=listing.page_url,
                click_index=click_index,
                discovered_urls=len(urls),
            )

            try:
                result = await crawler.arun(
                    url=listing.page_url,
                    config=load_more_run_config(
                        settings,
                        listing.session_id,
                        self._click_js(listing),
                        self._wait_js(listing),
                    ),
                )
            except Exception as exc:
                self._log(
                    session_logger,
                    "load_more_click_exception",
                    page_url=listing.page_url,
                    click_index=click_index,
                    error_message=str(exc),
                )
                break

            if not result.success:
                stable_rounds += 1
                self._log(
                    session_logger,
                    "load_more_click_failed",
                    page_url=listing.page_url,
                    click_index=click_index,
                    stable_rounds=stable_rounds,
                    error_message=result.error_message,
                )
                if stable_rounds >= max_stable:
                    break
                continue

            latest_result = result
            new_urls = unique_keep_order(urls + self._collect_links(latest_result, blueprint))

            if len(new_urls) > len(urls):
                urls = new_urls
                stable_rounds = 0
                self._log(
                    session_logger,
                    "load_more_click_growth",
                    page_url=listing.page_url,
                    click_index=click_index,
                    discovered_urls=len(urls),
                    has_load_more=page_has_load_more(latest_result),
                )
            else:
                stable_rounds += 1
                self._log(
                    session_logger,
                    "load_more_click_no_growth",
                    page_url=listing.page_url,
                    click_index=click_index,
                    stable_rounds=stable_rounds,
                    discovered_urls=len(urls),
                    has_load_more=page_has_load_more(latest_result),
                )
                if stable_rounds >= max_stable:
                    self._log(
                        session_logger,
                        "load_more_stable_limit_reached",
                        page_url=listing.page_url,
                        click_index=click_index,
                        stable_rounds=stable_rounds,
                        discovered_urls=len(urls),
                        has_load_more=page_has_load_more(latest_result),
                    )
                    break

        final_urls = unique_keep_order(urls + self._collect_links(latest_result, blueprint))
        await self._close_listing(crawler, listing)

        self._log(
            session_logger,
            "listing_complete",
            page_url=listing.page_url,
            discovered_urls=len(final_urls),
            load_more_rounds=click_index,
        )
        return final_urls

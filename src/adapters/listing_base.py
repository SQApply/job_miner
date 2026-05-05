from __future__ import annotations

from ..crawl.browser_lane import close_session, interaction_run_config, listing_run_config
from ..crawl.consent import build_accept_all_cookies_js
from ..crawl.link_collector import collect_job_links


class ListingAdapterMixin:
    def _log(self, session_logger, event: str, **payload) -> None:
        if session_logger:
            session_logger.log(event, **payload)

    async def _open_listing(self, crawler, settings, listing, session_logger=None):
        self._log(session_logger, "listing_start", page_url=listing.page_url, browser_session_id=listing.session_id)
        result = await crawler.arun(
            url=listing.page_url,
            config=listing_run_config(settings, listing.session_id, listing.initial_wait_for),
        )
        if not result.success:
            self._log(
                session_logger,
                "listing_initial_failed",
                page_url=listing.page_url,
                error_message=result.error_message,
            )
            raise RuntimeError(f"Initial listing crawl failed: {result.error_message}")
        return result

    async def _accept_cookies(self, crawler, settings, listing) -> None:
        try:
            await crawler.arun(
                url=listing.page_url,
                config=interaction_run_config(
                    settings,
                    listing.session_id,
                    build_accept_all_cookies_js(),
                    "js:() => true",
                ),
            )
        except Exception:
            pass

    async def _refresh_listing(self, crawler, settings, listing):
        return await crawler.arun(
            url=listing.page_url,
            config=listing_run_config(settings, listing.session_id, listing.initial_wait_for),
        )

    def _collect_links(self, result, blueprint) -> list[str]:
        listing = blueprint.listing
        return collect_job_links(
            result,
            page_url=listing.page_url,
            allowed_hosts=blueprint.allowed_hosts,
            href_contains=listing.item_href_contains,
            detail_text_patterns=listing.detail_text_patterns,
            exclude_exact_urls=listing.exclude_exact_urls,
        )

    async def _close_listing(self, crawler, listing) -> None:
        await close_session(crawler, listing.session_id)

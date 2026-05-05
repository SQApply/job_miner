from __future__ import annotations

from .base import BaseAdapter
from .listing_base import ListingAdapterMixin


class GenericListingAdapter(ListingAdapterMixin, BaseAdapter):
    """Direct-link listing adapter: open one listing page and harvest matching job links.

    Use this when the listing page already exposes all job detail URLs and no pagination,
    load-more button, search step, or modal interaction is required.
    """

    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
        settings = system_config.browser
        listing = blueprint.listing

        initial_result = await self._open_listing(crawler, settings, listing, session_logger=session_logger)
        await self._accept_cookies(crawler, settings, listing)
        latest_result = await self._refresh_listing(crawler, settings, listing) or initial_result
        urls = self._collect_links(latest_result, blueprint)
        await self._close_listing(crawler, listing)

        self._log(
            session_logger,
            "listing_complete",
            page_url=listing.page_url,
            discovered_urls=len(urls),
            adapter="generic_listing",
        )
        return urls

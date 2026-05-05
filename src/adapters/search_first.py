from __future__ import annotations

from ..crawl.browser_lane import interaction_run_config
from ..crawl.interactions import build_wait_for_body_or_url_change_js
from .paginated_anchor import PaginatedAnchorAdapter


class SearchFirstAdapter(PaginatedAnchorAdapter):
    """Adapter for sites that require submitting a search before listings appear.

    YAML controls the search step via listing.search.submit_js and listing.search.wait_for_js.
    After search, normal paginated-anchor harvesting is used.
    """

    async def _run_search_if_configured(self, crawler, settings, listing, session_logger=None) -> None:
        if not listing.search.enabled:
            return
        submit_js = listing.search.submit_js
        if not submit_js:
            if not listing.search.input_selector:
                raise ValueError("listing.search.enabled=true requires submit_js or input_selector")
            query = listing.search.query.replace("`", "\\`")
            submit_selector = listing.search.submit_selector
            submit_js = f"""
(() => {{
  const input = document.querySelector(`{listing.search.input_selector}`);
  if (input) {{
    input.focus();
    input.value = `{query}`;
    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
  }}
  const submit = {f'document.querySelector(`{submit_selector}`)' if submit_selector else 'input'};
  if (submit) submit.click();
}})();
"""
        await crawler.arun(
            url=listing.page_url,
            config=interaction_run_config(
                settings,
                listing.session_id,
                submit_js,
                listing.search.wait_for_js or build_wait_for_body_or_url_change_js(),
            ),
        )

    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
        # Open once, run search, then delegate to normal pagination flow.
        settings = system_config.browser
        listing = blueprint.listing
        await self._open_listing(crawler, settings, listing, session_logger=session_logger)
        await self._accept_cookies(crawler, settings, listing)
        await self._run_search_if_configured(crawler, settings, listing, session_logger=session_logger)
        return await super().discover_job_urls(crawler, blueprint, system_config, session_logger=session_logger)

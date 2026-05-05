from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from ..crawl.browser_lane import listing_run_config
from ..crawl.link_collector import page_has_multiple_pages, page_has_pagination
from ..utils import unique_keep_order
from .base import BaseAdapter
from .listing_base import ListingAdapterMixin


def _replace_query_param(url: str, param_name: str, value: int) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[param_name] = str(value)

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query),
            parsed.fragment,
        )
    )


def _build_url_from_template(url_template: str, page_value: int) -> str:
    if not url_template:
        return ""

    return (
        url_template
        .replace("{page}", str(page_value))
        .replace("{page_number}", str(page_value))
        .replace("{{page}}", str(page_value))
        .replace("{{page_number}}", str(page_value))
    )


class PaginatedUrlParamAdapter(ListingAdapterMixin, BaseAdapter):
    """
    Generic adapter for listings paginated by URL query parameter.

    Examples:
        ?page=1  -> ?page=2
        ?paged=1 -> ?paged=2

    This adapter intentionally does NOT inherit from PaginatedAnchorAdapter.
    It directly opens the next URL instead of clicking pagination controls.
    """

    async def discover_job_urls(
        self,
        crawler,
        blueprint,
        system_config,
        session_logger=None,
    ) -> list[str]:
        settings = system_config.browser
        listing = blueprint.listing

        initial_result = await self._open_listing(
            crawler,
            settings,
            listing,
            session_logger=session_logger,
        )

        await self._accept_cookies(crawler, settings, listing)

        latest_result = await self._refresh_listing(
            crawler,
            settings,
            listing,
        ) or initial_result

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

        page_param = listing.pagination.page_param
        start_page = listing.pagination.start_page
        step = listing.pagination.step
        url_template = getattr(listing.pagination, "url_template", "") or ""

        while listing.pagination.enabled and turns < listing.pagination.max_turns:
            turns += 1

            next_page = start_page + (turns * step)

            if url_template:
                next_url = _build_url_from_template(url_template, next_page)
            else:
                next_url = _replace_query_param(
                    listing.page_url,
                    page_param,
                    next_page,
                )

            self._log(
                session_logger,
                "pagination_click_start",
                page_url=listing.page_url,
                page_turn=turns,
                current_discovered_urls=len(urls),
                pagination_type=listing.pagination.type,
            )

            self._log(
                session_logger,
                "pagination_url_param_next_url",
                page_url=listing.page_url,
                page_turn=turns,
                page_param=page_param,
                next_page=next_page,
                next_url=next_url,
            )

            try:
                result = await crawler.arun(
                    url=next_url,
                    config=listing_run_config(
                        settings=settings,
                        session_id=listing.session_id,
                        wait_for=listing.initial_wait_for,
                    ),
                )
            except Exception as exc:
                stable_rounds += 1
                self._log(
                    session_logger,
                    "pagination_click_exception",
                    page_url=listing.page_url,
                    page_turn=turns,
                    attempted_url=next_url,
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
                    attempted_url=next_url,
                    stable_rounds=stable_rounds,
                    error_message=result.error_message,
                )

                if stable_rounds >= listing.pagination.stop_after_stable_rounds:
                    break

                continue

            latest_result = result
            page_urls = self._collect_links(latest_result, blueprint)
            merged = unique_keep_order(urls + page_urls)

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
                    attempted_url=next_url,
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
                        attempted_url=next_url,
                        job_url=discovered_url,
                    )

            else:
                stable_rounds += 1
                self._log(
                    session_logger,
                    "pagination_click_no_growth",
                    page_url=listing.page_url,
                    page_turn=turns,
                    attempted_url=next_url,
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
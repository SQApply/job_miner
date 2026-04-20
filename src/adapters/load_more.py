# from __future__ import annotations

# from ..crawl.browser_lane import close_session, listing_run_config, load_more_run_config
# from ..crawl.continuation import should_continue
# from ..crawl.link_collector import collect_job_links, page_has_load_more
# from .base import BaseAdapter


# class LoadMoreAdapter(BaseAdapter):
#     async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
#         settings = system_config.browser
#         listing = blueprint.listing

#         if session_logger:
#             session_logger.log(
#                 "listing_start",
#                 page_url=listing.page_url,
#                 browser_session_id=listing.session_id,
#             )

#         initial_result = await crawler.arun(
#             url=listing.page_url,
#             config=listing_run_config(settings, listing.session_id, listing.initial_wait_for),
#         )

#         if not initial_result.success:
#             if session_logger:
#                 session_logger.log(
#                     "listing_initial_failed",
#                     page_url=listing.page_url,
#                     error_message=initial_result.error_message,
#                 )
#             raise RuntimeError(f"Initial listing crawl failed: {initial_result.error_message}")

#         latest_result = initial_result
#         urls = collect_job_links(
#             latest_result,
#             page_url=listing.page_url,
#             allowed_hosts=blueprint.allowed_hosts,
#             href_contains=listing.item_href_contains,
#             exclude_exact_urls=listing.exclude_exact_urls,
#         )

#         if session_logger:
#             session_logger.log(
#                 "listing_initial_complete",
#                 page_url=listing.page_url,
#                 discovered_urls=len(urls),
#                 has_load_more=page_has_load_more(latest_result),
#             )

#         stable_rounds = 0
#         click_index = 0

#         while should_continue(listing.load_more, click_index):
#             # Stop only when the button is gone and the page is stable
#             if not page_has_load_more(latest_result) and click_index > 0:
#                 if session_logger:
#                     session_logger.log(
#                         "load_more_button_gone",
#                         page_url=listing.page_url,
#                         click_index=click_index,
#                         discovered_urls=len(urls),
#                     )
#                 break

#             click_index += 1

#             if session_logger:
#                 session_logger.log(
#                     "load_more_click_start",
#                     page_url=listing.page_url,
#                     click_index=click_index,
#                     discovered_urls=len(urls),
#                 )

#             try:
#                 result = await crawler.arun(
#                     url=listing.page_url,
#                     config=load_more_run_config(
#                         settings,
#                         listing.session_id,
#                         listing.load_more.click_js,
#                         listing.load_more.wait_for_js,
#                     ),
#                 )
#             except Exception as exc:
#                 if session_logger:
#                     session_logger.log(
#                         "load_more_click_exception",
#                         page_url=listing.page_url,
#                         click_index=click_index,
#                         error_message=str(exc),
#                     )
#                 break

#             if not result.success:
#                 stable_rounds += 1
#                 if session_logger:
#                     session_logger.log(
#                         "load_more_click_failed",
#                         page_url=listing.page_url,
#                         click_index=click_index,
#                         stable_rounds=stable_rounds,
#                         error_message=result.error_message,
#                     )
#                 if stable_rounds >= 3:
#                     break
#                 continue

#             latest_result = result

#             new_urls = collect_job_links(
#                 latest_result,
#                 page_url=listing.page_url,
#                 allowed_hosts=blueprint.allowed_hosts,
#                 href_contains=listing.item_href_contains,
#                 exclude_exact_urls=listing.exclude_exact_urls,
#             )

#             if len(new_urls) > len(urls):
#                 urls = new_urls
#                 stable_rounds = 0
#                 if session_logger:
#                     session_logger.log(
#                         "load_more_click_growth",
#                         page_url=listing.page_url,
#                         click_index=click_index,
#                         discovered_urls=len(urls),
#                         has_load_more=page_has_load_more(latest_result),
#                     )
#             else:
#                 stable_rounds += 1
#                 if session_logger:
#                     session_logger.log(
#                         "load_more_click_no_growth",
#                         page_url=listing.page_url,
#                         click_index=click_index,
#                         stable_rounds=stable_rounds,
#                         discovered_urls=len(urls),
#                         has_load_more=page_has_load_more(latest_result),
#                     )

#                 # Allow a few stable rounds before concluding the list is exhausted
#                 if stable_rounds >= 3 and not page_has_load_more(latest_result):
#                     break

#         # One final harvest from the fully expanded DOM state
#         final_urls = collect_job_links(
#             latest_result,
#             page_url=listing.page_url,
#             allowed_hosts=blueprint.allowed_hosts,
#             href_contains=listing.item_href_contains,
#             exclude_exact_urls=listing.exclude_exact_urls,
#         )

#         if len(final_urls) > len(urls):
#             urls = final_urls

#         await close_session(crawler, listing.session_id)

#         if session_logger:
#             session_logger.log(
#                 "listing_complete",
#                 page_url=listing.page_url,
#                 discovered_urls=len(urls),
#                 load_more_rounds=click_index,
#             )

#         return urls
from __future__ import annotations

from ..crawl.browser_lane import close_session, listing_run_config, load_more_run_config
from ..crawl.continuation import should_continue
from ..crawl.link_collector import collect_job_links, page_has_load_more
from .base import BaseAdapter


class LoadMoreAdapter(BaseAdapter):
    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
        settings = system_config.browser
        listing = blueprint.listing

        if session_logger:
            session_logger.log(
                "listing_start",
                page_url=listing.page_url,
                browser_session_id=listing.session_id,
            )

        initial_result = await crawler.arun(
            url=listing.page_url,
            config=listing_run_config(settings, listing.session_id, listing.initial_wait_for),
        )

        if not initial_result.success:
            if session_logger:
                session_logger.log(
                    "listing_initial_failed",
                    page_url=listing.page_url,
                    error_message=initial_result.error_message,
                )
            raise RuntimeError(f"Initial listing crawl failed: {initial_result.error_message}")

        latest_result = initial_result
        urls = collect_job_links(
            latest_result,
            page_url=listing.page_url,
            allowed_hosts=blueprint.allowed_hosts,
            href_contains=listing.item_href_contains,
            detail_text_patterns=listing.detail_text_patterns,
            exclude_exact_urls=listing.exclude_exact_urls,
        )

        if session_logger:
            session_logger.log(
                "listing_initial_complete",
                page_url=listing.page_url,
                discovered_urls=len(urls),
                has_load_more=page_has_load_more(latest_result),
            )

        stable_rounds = 0
        click_index = 0

        while should_continue(listing.load_more, click_index):
            if not page_has_load_more(latest_result) and click_index > 0:
                if session_logger:
                    session_logger.log(
                        "load_more_button_gone",
                        page_url=listing.page_url,
                        click_index=click_index,
                        discovered_urls=len(urls),
                    )
                break

            click_index += 1

            if session_logger:
                session_logger.log(
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
                        listing.load_more.click_js,
                        listing.load_more.wait_for_js,
                    ),
                )
            except Exception as exc:
                if session_logger:
                    session_logger.log(
                        "load_more_click_exception",
                        page_url=listing.page_url,
                        click_index=click_index,
                        error_message=str(exc),
                    )
                break

            if not result.success:
                stable_rounds += 1
                if session_logger:
                    session_logger.log(
                        "load_more_click_failed",
                        page_url=listing.page_url,
                        click_index=click_index,
                        stable_rounds=stable_rounds,
                        error_message=result.error_message,
                    )
                if stable_rounds >= 3:
                    break
                continue

            latest_result = result

            new_urls = collect_job_links(
                latest_result,
                page_url=listing.page_url,
                allowed_hosts=blueprint.allowed_hosts,
                href_contains=listing.item_href_contains,
                detail_text_patterns=listing.detail_text_patterns,
                exclude_exact_urls=listing.exclude_exact_urls,
            )

            if len(new_urls) > len(urls):
                urls = new_urls
                stable_rounds = 0
                if session_logger:
                    session_logger.log(
                        "load_more_click_growth",
                        page_url=listing.page_url,
                        click_index=click_index,
                        discovered_urls=len(urls),
                        has_load_more=page_has_load_more(latest_result),
                    )
            else:
                stable_rounds += 1
                if session_logger:
                    session_logger.log(
                        "load_more_click_no_growth",
                        page_url=listing.page_url,
                        click_index=click_index,
                        stable_rounds=stable_rounds,
                        discovered_urls=len(urls),
                        has_load_more=page_has_load_more(latest_result),
                    )

                if stable_rounds >= 3 and not page_has_load_more(latest_result):
                    break

        final_urls = collect_job_links(
            latest_result,
            page_url=listing.page_url,
            allowed_hosts=blueprint.allowed_hosts,
            href_contains=listing.item_href_contains,
            detail_text_patterns=listing.detail_text_patterns,
            exclude_exact_urls=listing.exclude_exact_urls,
        )

        if len(final_urls) > len(urls):
            urls = final_urls

        await close_session(crawler, listing.session_id)

        if session_logger:
            session_logger.log(
                "listing_complete",
                page_url=listing.page_url,
                discovered_urls=len(urls),
                load_more_rounds=click_index,
            )

        return urls
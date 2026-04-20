from __future__ import annotations

from ..schemas import BrowserSettings


def build_browser_config(settings: BrowserSettings):
    from crawl4ai import BrowserConfig

    return BrowserConfig(
        headless=settings.headless,
        verbose=settings.verbose,
        use_persistent_context=settings.use_persistent_context,
        user_data_dir=settings.user_data_dir,
    )


def listing_run_config(settings: BrowserSettings, session_id: str, wait_for: str | None):
    from crawl4ai import CacheMode, CrawlerRunConfig

    return CrawlerRunConfig(
        session_id=session_id,
        cache_mode=CacheMode.BYPASS,
        wait_for=wait_for,
        wait_for_timeout=settings.listing_wait_for_timeout,
        delay_before_return_html=settings.delay_before_return_html,
        scan_full_page=settings.scan_full_page,
        max_scroll_steps=settings.max_scroll_steps,
        scroll_delay=settings.scroll_delay,
        remove_overlay_elements=settings.remove_overlay_elements,
        remove_consent_popups=settings.remove_consent_popups,
        screenshot=True,
    )


def load_more_run_config(settings: BrowserSettings, session_id: str, click_js: str, wait_for_js: str):
    from crawl4ai import CacheMode, CrawlerRunConfig

    return CrawlerRunConfig(
        session_id=session_id,
        cache_mode=CacheMode.BYPASS,
        js_only=True,
        js_code_before_wait=click_js,
        wait_for=wait_for_js,
        wait_for_timeout=settings.listing_wait_for_timeout,
        delay_before_return_html=settings.delay_before_return_html,
        remove_overlay_elements=settings.remove_overlay_elements,
        remove_consent_popups=settings.remove_consent_popups,
        screenshot=True,
    )


# def interaction_run_config(settings: BrowserSettings, session_id: str, click_js: str, wait_for_js: str):
#     from crawl4ai import CacheMode, CrawlerRunConfig

#     return CrawlerRunConfig(
#         session_id=session_id,
#         cache_mode=CacheMode.BYPASS,
#         js_only=True,
#         js_code_before_wait=click_js,
#         wait_for=wait_for_js,
#         wait_for_timeout=settings.listing_wait_for_timeout,
#         delay_before_return_html=settings.delay_before_return_html,
#         remove_overlay_elements=settings.remove_overlay_elements,
#         remove_consent_popups=settings.remove_consent_popups,
#         screenshot=True,
#     )
def interaction_run_config(settings: BrowserSettings, session_id: str, click_js: str, wait_for_js: str):
    from crawl4ai import CacheMode, CrawlerRunConfig

    return CrawlerRunConfig(
        session_id=session_id,
        cache_mode=CacheMode.BYPASS,
        js_only=True,
        js_code_before_wait=click_js,
        wait_for=wait_for_js,
        wait_for_timeout=settings.listing_wait_for_timeout,
        delay_before_return_html=settings.delay_before_return_html,
        remove_overlay_elements=settings.remove_overlay_elements,
        remove_consent_popups=settings.remove_consent_popups,
        screenshot=True,
    )

def detail_run_config(
    settings: BrowserSettings,
    wait_for: str,
    extraction_strategy,
    session_id: str | None = None,
):
    from crawl4ai import CacheMode, CrawlerRunConfig

    return CrawlerRunConfig(
        session_id=session_id,
        cache_mode=CacheMode.BYPASS,
        wait_for=wait_for,
        wait_for_timeout=settings.detail_wait_for_timeout,
        delay_before_return_html=0.5,
        remove_overlay_elements=settings.remove_overlay_elements,
        remove_consent_popups=settings.remove_consent_popups,
        extraction_strategy=extraction_strategy,
    )


async def close_session(crawler, session_id: str) -> None:
    try:
        await crawler.crawler_strategy.kill_session(session_id)
    except Exception:
        pass
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add repo root to Python import path before importing src.*
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crawl4ai import AsyncWebCrawler

from src.blueprint_hub import BlueprintHub
from src.crawl.browser_lane import build_browser_config, listing_run_config


async def main() -> None:
    root = ROOT

    hub = BlueprintHub(root)
    system_config = hub.system
    blueprint = hub.get_target("sescorporation_jobs")

    browser_config = build_browser_config(system_config.browser)

    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(
            url=blueprint.listing.page_url,
            config=listing_run_config(
                settings=system_config.browser,
                session_id=blueprint.listing.session_id,
                wait_for="js:() => true",
            ),
        )

        print("success:", result.success)
        print("url:", getattr(result, "url", None))
        print("error:", getattr(result, "error_message", None))

        html = getattr(result, "html", "") or ""
        cleaned_html = getattr(result, "cleaned_html", "") or ""
        markdown = getattr(result, "markdown", "") or ""

        (root / "debug_ses_html.html").write_text(
            html,
            encoding="utf-8",
            errors="ignore",
        )

        (root / "debug_ses_cleaned.html").write_text(
            cleaned_html,
            encoding="utf-8",
            errors="ignore",
        )

        (root / "debug_ses_markdown.md").write_text(
            str(markdown),
            encoding="utf-8",
            errors="ignore",
        )

        print("html length:", len(html))
        print("cleaned html length:", len(cleaned_html))
        print("markdown length:", len(str(markdown)))
        print("contains body:", "<body" in html.lower())
        print(
            "contains workable:",
            "app.workable.com/j/" in html.lower()
            or "app.workable.com/j/" in cleaned_html.lower(),
        )


if __name__ == "__main__":
    asyncio.run(main())
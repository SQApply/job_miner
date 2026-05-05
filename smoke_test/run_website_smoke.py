# smoke_test/run_website_smoke.py

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any


def _add_repo_to_path(root: Path) -> None:
    root_str = str(root.resolve())
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def _safe_set(obj: Any, attr: str, value: Any) -> None:
    if obj is not None and hasattr(obj, attr):
        setattr(obj, attr, value)


def apply_smoke_limits(blueprint: Any) -> Any:
    """
    Temporary smoke-test limiter.

    This mutates a copied blueprint only.
    It does not change your YAML files.
    """

    bp = deepcopy(blueprint)
    listing = bp.listing

    # Keep listing waits short for smoke testing.
    _safe_set(listing, "max_pages", 3)

    # Load-more websites:
    # Strategic Staff / Randstad style.
    if hasattr(listing, "load_more") and listing.load_more is not None:
        _safe_set(listing.load_more, "max_clicks", 2)
        _safe_set(listing.load_more, "stop_after_stable_rounds", 1)

    # Paginated websites:
    # Judge / Experis / OpenSystems / JobDiva pagination.
    if hasattr(listing, "pagination") and listing.pagination is not None:
        _safe_set(listing.pagination, "max_turns", 2)
        _safe_set(listing.pagination, "max_pages", 3)
        _safe_set(listing.pagination, "stop_after_stable_rounds", 1)

    # Detail button capture websites:
    # JobDiva style. Limit how many Details buttons are clicked.
    if hasattr(listing, "detail_button") and listing.detail_button is not None:
        _safe_set(listing.detail_button, "max_buttons", 3)

    # Infinite scroll websites:
    if hasattr(listing, "infinite_scroll") and listing.infinite_scroll is not None:
        _safe_set(listing.infinite_scroll, "max_scrolls", 2)
        _safe_set(listing.infinite_scroll, "stop_after_stable_rounds", 1)

    return bp


class SmokeLogger:
    """
    Minimal logger compatible with your adapters.
    It prints JSONL-style events to terminal.
    """

    def __init__(self, target_id: str):
        self.target_id = target_id
        self.events: list[dict[str, Any]] = []

    def log(self, event: str, **kwargs: Any) -> None:
        payload = {
            "target_id": self.target_id,
            "event": event,
            **kwargs,
        }
        self.events.append(payload)
        print(json.dumps(payload, ensure_ascii=False))


async def smoke_target(root: Path, target_id: str) -> dict[str, Any]:
    from crawl4ai import AsyncWebCrawler

    from src.blueprint_hub import BlueprintHub
    from src.crawl.browser_lane import build_browser_config
    from src.router import get_adapter

    started = time.perf_counter()

    hub = BlueprintHub(root)
    system_config = hub.system

    original_blueprint = hub.get_target(target_id)
    blueprint = apply_smoke_limits(original_blueprint)

    adapter = get_adapter(blueprint)
    browser_config = build_browser_config(system_config.browser)
    session_logger = SmokeLogger(target_id)

    session_logger.log(
        "smoke_start",
        adapter=blueprint.adapter,
        page_url=blueprint.listing.page_url,
    )

    status = "passed"
    error_message = None
    urls: list[str] = []

    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            urls = await adapter.discover_job_urls(
                crawler,
                blueprint,
                system_config,
                session_logger=session_logger,
            )

        if not urls:
            status = "failed"
            error_message = "No job URLs discovered."

    except Exception as exc:
        status = "failed"
        error_message = str(exc)

    elapsed = round(time.perf_counter() - started, 3)

    result = {
        "target_id": target_id,
        "adapter": getattr(blueprint, "adapter", None),
        "status": status,
        "discovered_urls": len(urls),
        "sample_urls": urls[:5],
        "elapsed_seconds": elapsed,
        "error_message": error_message,
    }

    session_logger.log("smoke_complete", **result)
    return result


async def smoke_all(root: Path, target_ids: list[str] | None = None) -> list[dict[str, Any]]:
    from src.blueprint_hub import BlueprintHub

    hub = BlueprintHub(root)

    if target_ids:
        targets = target_ids
    else:
        # For smoke testing, run every site from site_registry.yaml,
        # not only the limited fleet.yaml targets.
        targets = hub.list_target_ids()

    results = []

    for target_id in targets:
        print("\n" + "=" * 100)
        print(f"SMOKE TEST TARGET: {target_id}")
        print("=" * 100)

        result = await smoke_target(root, target_id)
        results.append(result)

    return results

def main() -> None:
    parser = argparse.ArgumentParser(description="Fast website smoke test without LLM detail extraction.")
    parser.add_argument(
        "--root",
        default=".",
        help="Repo root. Default: current directory.",
    )
    parser.add_argument(
        "--target",
        action="append",
        help="Run smoke test for one target. Can be repeated. If omitted, all fleet targets are tested.",
    )
    parser.add_argument(
        "--output",
        default="smoke_test/smoke_results.json",
        help="Path to write smoke test summary JSON.",
    )

    args = parser.parse_args()

    root = Path(args.root).resolve()
    _add_repo_to_path(root)

    results = asyncio.run(smoke_all(root, args.target))

    output_path = root / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 100)
    print("SMOKE TEST SUMMARY")
    print("=" * 100)
    print(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nSaved smoke summary to: {output_path}")


if __name__ == "__main__":
    main()
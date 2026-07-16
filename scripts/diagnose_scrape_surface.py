from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.blueprint_hub import BlueprintHub
from src.crawl.browser_lane import (
    build_browser_config,
    close_session,
    detail_run_config,
    listing_run_config,
    resilient_detail_wait,
)
from src.portals.surface_diagnostics import build_surface_report, dump_report, report_summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture a read-only Crawl4AI surface bundle without invoking an LLM."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--url", required=True)
    parser.add_argument("--mode", choices=("listing", "detail"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--wait-for", default=None)
    return parser


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raw_markdown = getattr(value, "raw_markdown", None)
    return str(raw_markdown if raw_markdown is not None else value)


def _write_screenshot(value: Any, path: Path) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        payload = base64.b64decode(raw, validate=True)
    except Exception:
        return False
    path.write_bytes(payload)
    return True


async def _run(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    hub = BlueprintHub(root)
    session_id = f"surface_diag_{uuid.uuid4().hex[:12]}"

    from crawl4ai import AsyncWebCrawler

    async with AsyncWebCrawler(config=build_browser_config(hub.system.browser)) as crawler:
        try:
            if args.mode == "listing":
                config = listing_run_config(hub.system.browser, session_id, args.wait_for)
            else:
                config = detail_run_config(
                    hub.system.browser,
                    resilient_detail_wait(args.wait_for),
                    session_id=session_id,
                )
            result = await crawler.arun(url=args.url, config=config)
            report = build_surface_report(result, requested_url=args.url, mode=args.mode)

            (output_dir / "report.json").write_text(dump_report(report), encoding="utf-8")
            (output_dir / "raw.html").write_text(
                _text(getattr(result, "html", None)), encoding="utf-8"
            )
            (output_dir / "cleaned.html").write_text(
                _text(getattr(result, "cleaned_html", None)), encoding="utf-8"
            )
            (output_dir / "markdown.md").write_text(
                _text(getattr(result, "markdown", None)), encoding="utf-8"
            )
            screenshot_written = _write_screenshot(
                getattr(result, "screenshot", None), output_dir / "screenshot.png"
            )
            print(
                "SCRAPE_SURFACE_DIAGNOSTIC_OK",
                report_summary(report),
                f"screenshot={str(screenshot_written).lower()}",
                f"output={output_dir}",
            )
            return 0
        except Exception as exc:
            payload = {
                "requested_url": args.url,
                "mode": args.mode,
                "exception_type": type(exc).__name__,
                "exception": str(exc),
                "traceback": traceback.format_exc(),
            }
            (output_dir / "exception.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(
                "SCRAPE_SURFACE_DIAGNOSTIC_EXCEPTION",
                f"type={type(exc).__name__}",
                f"output={output_dir}",
            )
            return 2
        finally:
            await close_session(crawler, session_id)


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()

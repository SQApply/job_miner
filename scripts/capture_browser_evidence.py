from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.blueprint_hub import BlueprintHub
from src.crawl.browser_evidence import BrowserEvidenceCollector, BrowserEvidenceOptions
from src.portals.safety import default_allowed_hosts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture bounded accessible-DOM and public JSON evidence for one portal."
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--url", required=True)
    parser.add_argument("--allowed-host", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-nodes", type=int, default=20_000)
    parser.add_argument("--max-frames", type=int, default=20)
    parser.add_argument("--max-network-json", type=int, default=120)
    parser.add_argument("--navigation-timeout-ms", type=int, default=60_000)
    return parser.parse_args()


def atomic_write_json(path: Path, payload: dict) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, destination)


async def run(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    host = str(urlsplit(args.url).hostname or "").lower()
    allowed_hosts = tuple(args.allowed_host or default_allowed_hosts(host))
    browser_settings = BlueprintHub(root).system.browser
    collector = BrowserEvidenceCollector(
        browser_settings,
        options=BrowserEvidenceOptions(
            navigation_timeout_ms=args.navigation_timeout_ms,
            max_frames=args.max_frames,
            max_nodes_total=args.max_nodes,
            max_nodes_per_frame=min(12_000, args.max_nodes),
            max_network_json_responses=args.max_network_json,
        ),
    )
    report = await collector.capture(args.url, allowed_hosts=allowed_hosts)
    atomic_write_json(args.output, report.model_dump(mode="json"))
    print(
        "PHASE_7B_BROWSER_EVIDENCE_OK",
        f"success={str(report.success).lower()}",
        f"frames={len(report.frames)}",
        f"nodes={report.node_count}",
        f"linkless_clickables={report.linkless_clickable_count}",
        f"network_json={len(report.network_json)}",
        f"output={args.output.resolve()}",
    )
    return 0 if report.success else 2


def main() -> None:
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()

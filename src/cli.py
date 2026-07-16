from __future__ import annotations

import argparse
import asyncio
import json

from .mission_control import run_fleet, run_target
from .settings import resolve_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Job miner CLI")
    parser.add_argument("--root", default=".", help="Project root directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    target_cmd = subparsers.add_parser("launch-target", help="Run one target")
    target_cmd.add_argument("--target", required=True, help="Target id from blueprints/site_registry.yaml")
    target_cmd.add_argument("--force-detail-refresh", action="store_true", help="Ignore incremental planning and deep-scrape every discovered URL")
    target_cmd.add_argument("--disable-incremental-rescrape", action="store_true", help="Use the old behavior and scrape every discovered detail URL")
    target_cmd.add_argument("--deep-refresh-days", type=int, default=14, help="Deep-refresh active known jobs after this many days")
    target_cmd.add_argument("--max-jobs", type=int, default=None, help="Bound detail extraction for a safe test scrape")

    fleet_cmd = subparsers.add_parser("launch-fleet", help="Run all targets in blueprints/fleet.yaml")
    fleet_cmd.add_argument("--force-detail-refresh", action="store_true", help="Ignore incremental planning and deep-scrape every discovered URL")
    fleet_cmd.add_argument("--disable-incremental-rescrape", action="store_true", help="Use the old behavior and scrape every discovered detail URL")
    fleet_cmd.add_argument("--deep-refresh-days", type=int, default=14, help="Deep-refresh active known jobs after this many days")
    fleet_cmd.add_argument("--max-jobs", type=int, default=None, help="Bound detail extraction per target for a safe fleet test")
    fleet_cmd.add_argument("--targets", default="", help="Optional comma-separated target ids; defaults to blueprints/fleet.yaml")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    root = resolve_root(args.root)

    if args.command == "launch-target":
        result = asyncio.run(
            run_target(
                root,
                args.target,
                force_detail_refresh=bool(args.force_detail_refresh),
                incremental_rescrape=not bool(args.disable_incremental_rescrape),
                deep_refresh_days=int(args.deep_refresh_days),
                max_jobs=args.max_jobs,
            )
        )
        print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
        return

    if args.command == "launch-fleet":
        results = asyncio.run(
            run_fleet(
                root,
                force_detail_refresh=bool(args.force_detail_refresh),
                incremental_rescrape=not bool(args.disable_incremental_rescrape),
                deep_refresh_days=int(args.deep_refresh_days),
                max_jobs=args.max_jobs,
                target_ids=[value.strip() for value in args.targets.split(",") if value.strip()] or None,
            )
        )
        print(json.dumps([item.model_dump() for item in results], indent=2, ensure_ascii=False))
        return

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

    subparsers.add_parser("launch-fleet", help="Run all targets in blueprints/fleet.yaml")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    root = resolve_root(args.root)

    if args.command == "launch-target":
        result = asyncio.run(run_target(root, args.target))
        print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
        return

    if args.command == "launch-fleet":
        results = asyncio.run(run_fleet(root))
        print(json.dumps([item.model_dump() for item in results], indent=2, ensure_ascii=False))
        return
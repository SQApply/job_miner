from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .health.checks import check_glmocr_config, check_ollama
from .logger import build_session_logger
from .pipeline import _new_run_session_id, run_dir, run_file
from .settings import load_system_config, resolve_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resume OCR miner CLI")
    parser.add_argument("--root", default=".", help="Project root directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    file_cmd = subparsers.add_parser("run-file", help="OCR and parse one resume")
    file_cmd.add_argument("--input", required=True, help="Resume PDF/image path")
    file_cmd.add_argument("--skip-existing", action="store_true", help="Skip if the same sha256 already has a profile")

    dir_cmd = subparsers.add_parser("run-dir", help="OCR and parse all supported resumes in a directory")
    dir_cmd.add_argument("--input-dir", required=True, help="Directory containing resumes")
    dir_cmd.add_argument("--no-skip-existing", action="store_true", help="Process files even if sha256 already exists")

    subparsers.add_parser("health", help="Check local dependencies")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    root = resolve_root(args.root)
    config = load_system_config(root)

    if args.command == "health":
        payload = {
            "ollama": check_ollama(config),
            "glmocr_config": check_glmocr_config(root, config),
            "config": config.model_dump(),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return

    run_session_id = _new_run_session_id()
    logger = build_session_logger(root, config.output.log_dir, run_session_id)
    logger.log("session_start", command=args.command, ocr_backend=config.ocr.backend, ocr_model=config.ocr.ollama_model, extract_model=config.llm.provider)

    if args.command == "run-file":
        result = asyncio.run(run_file(root, config, logger, Path(args.input).resolve(), skip_existing=args.skip_existing))
        logger.log("session_complete", **result.model_dump())
        print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False, default=str))
        return

    if args.command == "run-dir":
        result = asyncio.run(run_dir(root, config, logger, Path(args.input_dir).resolve(), skip_existing=not args.no_skip_existing))
        logger.log("session_complete", **result.model_dump())
        print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False, default=str))
        return

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.certification import (
    CertificationOptions,
    PortalCertificationRecord,
    certify_portal_inventory,
    read_portal_inventory,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run resumable, side-effect-free certification against an XLSX/CSV/TXT portal inventory."
    )
    parser.add_argument("--input", required=True, help="Path to the portal inventory workbook, CSV, or text file")
    parser.add_argument("--root", default=".", help="Job Miner repository root")
    parser.add_argument(
        "--output-dir",
        default="data/certification",
        help="Directory for JSONL, JSON, CSV, failure evidence, and per-source jobs",
    )
    parser.add_argument("--max-jobs", type=int, default=10, choices=range(1, 11))
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--detail-concurrency", type=int, default=1, choices=(1, 2))
    parser.add_argument("--detail-retries", type=int, default=1, choices=(0, 1, 2))
    parser.add_argument("--requests-per-minute", type=int, default=30)
    parser.add_argument("--source-timeout-seconds", type=int, default=600)
    parser.add_argument("--acquisition-timeout-seconds", type=float, default=20.0)
    parser.add_argument("--resume", action="store_true", help="Skip sources whose latest result is successful")
    parser.add_argument("--only-failed", action="store_true", help="Rerun only previously failed/partial/blocked sources")
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N selected sources")
    parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="Run a specific stable source id; repeat for multiple sources",
    )
    parser.add_argument(
        "--allow-unknown-cross-domain-redirects",
        action="store_true",
        help="Allow a public redirect to an unclassified external domain during controlled testing",
    )
    parser.add_argument(
        "--allow-llm-fallback",
        action="store_true",
        help=(
            "Allow GPU/LLM extraction after deterministic extraction fails. "
            "Disabled by default; enabled results must pass source grounding."
        ),
    )
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="Parse and display the inventory without making network or browser requests",
    )
    return parser


def _progress(record: PortalCertificationRecord, position: int, total: int) -> None:
    print(
        f"[{position}/{total}]",
        record.status.upper(),
        record.source_id,
        f"platform={record.detected_platform or 'unknown'}",
        f"surface={record.surface_kind or 'unknown'}",
        f"discovered={record.discovered_urls}",
        f"attempted={record.attempted_urls}",
        f"extracted={record.extracted_jobs}",
        f"elapsed={record.elapsed_seconds}s",
        f"error={record.error_type or '-'}",
        flush=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    input_path = Path(args.input).resolve()
    root = Path(args.root).resolve()
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = root / output_dir

    inventory = read_portal_inventory(input_path)
    print(f"PORTAL_INVENTORY_OK sources={len(inventory)} input={input_path}")
    if args.inventory_only:
        print(json.dumps([entry.__dict__ for entry in inventory], indent=2, ensure_ascii=False))
        return

    options = CertificationOptions(
        max_jobs=args.max_jobs,
        max_pages=args.max_pages,
        detail_concurrency=args.detail_concurrency,
        detail_retry_attempts=args.detail_retries,
        requests_per_minute=args.requests_per_minute,
        source_timeout_seconds=args.source_timeout_seconds,
        acquisition_timeout_seconds=args.acquisition_timeout_seconds,
        allow_unknown_cross_domain_redirects=bool(args.allow_unknown_cross_domain_redirects),
        allow_llm_fallback=bool(args.allow_llm_fallback),
    )
    records = asyncio.run(
        certify_portal_inventory(
            input_path=input_path,
            root=root,
            output_dir=output_dir,
            options=options,
            resume=bool(args.resume),
            only_failed=bool(args.only_failed),
            limit=args.limit,
            source_ids=set(args.source_id) or None,
            on_progress=_progress,
        )
    )
    counts: dict[str, int] = {}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    print(
        "PORTAL_CERTIFICATION_COMPLETE",
        f"executed={len(records)}",
        f"counts={json.dumps(counts, sort_keys=True)}",
        f"summary={output_dir / 'portal_certification_summary.json'}",
    )


if __name__ == "__main__":
    main()

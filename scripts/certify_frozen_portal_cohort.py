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
from src.portals.production_cohort import read_source_id_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run bounded certification only for the frozen Phase 5.5C cohort, "
            "without enabling production ingestion or reconciliation."
        )
    )
    parser.add_argument("--input", required=True, help="Portal XLSX/CSV/TXT inventory")
    parser.add_argument("--source-id-file", required=True, help="Frozen cohort source-id file")
    parser.add_argument("--root", default=".", help="Job Miner repository root")
    parser.add_argument(
        "--output-dir",
        default="data/certification_phase55c_full",
        help="Existing full-fleet evidence directory to update",
    )
    parser.add_argument("--expected-sources", type=int, default=102)
    parser.add_argument("--expected-cohort-size", type=int, default=19)
    parser.add_argument("--max-jobs", type=int, default=10, choices=range(1, 11))
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--detail-concurrency", type=int, default=1, choices=(1, 2))
    parser.add_argument("--detail-retries", type=int, default=1, choices=(0, 1, 2))
    parser.add_argument("--requests-per-minute", type=int, default=30)
    parser.add_argument("--source-timeout-seconds", type=int, default=600)
    parser.add_argument("--acquisition-timeout-seconds", type=float, default=20.0)
    return parser


def _progress(record: PortalCertificationRecord, position: int, total: int) -> None:
    print(
        f"[{position}/{total}]",
        record.status.upper(),
        record.source_id,
        f"platform={record.detected_platform or 'unknown'}",
        f"surface={record.surface_kind or 'unknown'}",
        f"route={record.resolved_route_url or '-'}",
        f"discovered={record.discovered_urls}",
        f"attempted={record.attempted_urls}",
        f"extracted={record.extracted_jobs}",
        f"elapsed={record.elapsed_seconds}s",
        f"error={record.error_type or '-'}",
        flush=True,
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    input_path = Path(args.input).resolve()
    root = Path(args.root).resolve()
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = root / output_dir

    inventory = read_portal_inventory(input_path)
    if len(inventory) != args.expected_sources:
        parser.error(
            f"expected {args.expected_sources} inventory sources but parsed {len(inventory)}"
        )
    source_ids = read_source_id_file(Path(args.source_id_file))
    if len(source_ids) != args.expected_cohort_size:
        parser.error(
            f"expected {args.expected_cohort_size} cohort sources but file contains {len(source_ids)}"
        )
    inventory_ids = {entry.source_id for entry in inventory}
    unknown = sorted(set(source_ids) - inventory_ids)
    if unknown:
        parser.error("cohort contains unknown source ids: " + ", ".join(unknown))

    options = CertificationOptions(
        max_jobs=args.max_jobs,
        max_pages=args.max_pages,
        detail_concurrency=args.detail_concurrency,
        detail_retry_attempts=args.detail_retries,
        requests_per_minute=args.requests_per_minute,
        source_timeout_seconds=args.source_timeout_seconds,
        acquisition_timeout_seconds=args.acquisition_timeout_seconds,
        allow_unknown_cross_domain_redirects=False,
        allow_llm_fallback=False,
    )
    print(
        "PHASE_5_5C_COHORT_REGRESSION_START",
        f"inventory={len(inventory)}",
        f"selected={len(source_ids)}",
        f"output={output_dir}",
        flush=True,
    )
    records = asyncio.run(
        certify_portal_inventory(
            input_path=input_path,
            root=root,
            output_dir=output_dir,
            options=options,
            source_ids=set(source_ids),
            on_progress=_progress,
        )
    )
    counts: dict[str, int] = {}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    print(
        "PHASE_5_5C_COHORT_REGRESSION_COMPLETE",
        f"executed={len(records)}",
        f"counts={json.dumps(counts, sort_keys=True)}",
        f"summary={output_dir / 'portal_certification_summary.json'}",
        flush=True,
    )
    if len(records) != len(source_ids) or any(record.status != "success" for record in records):
        raise SystemExit(4)


if __name__ == "__main__":
    main()

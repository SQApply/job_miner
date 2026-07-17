from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.certification import (
    CertificationOptions,
    CertificationReportStore,
    PortalCertificationRecord,
    certify_portal_inventory,
    read_portal_inventory,
)
from src.portals.fleet_campaign import (
    build_fleet_campaign_report,
    select_campaign_source_ids,
    write_fleet_campaign_artifacts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run or resume a bounded all-portal certification campaign and emit "
            "truthful coverage, failure clusters, and safe retry manifests."
        )
    )
    parser.add_argument("--input", required=True, help="XLSX, CSV, or TXT portal inventory")
    parser.add_argument("--root", default=".", help="Job Miner repository root")
    parser.add_argument(
        "--output-dir",
        default="data/certification_fleet_campaign",
        help="Dedicated campaign output directory",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--resume",
        action="store_true",
        help="Run only inventory sources without any terminal certification record",
    )
    mode.add_argument(
        "--retry-actionable",
        action="store_true",
        help="Retry repairable sources while excluding successes and protected portals",
    )
    mode.add_argument(
        "--retry-gpu-eligible",
        action="store_true",
        help="Retry only detail-extraction failures eligible for grounded GPU fallback",
    )
    mode.add_argument(
        "--analyze-only",
        action="store_true",
        help="Regenerate campaign artifacts without making network/browser requests",
    )
    parser.add_argument(
        "--expected-sources",
        type=int,
        default=None,
        help="Fail before crawling when the parsed inventory count differs",
    )
    parser.add_argument("--target-successes", type=int, default=80)
    parser.add_argument("--max-jobs", type=int, default=10, choices=range(1, 11))
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--detail-concurrency", type=int, default=1, choices=(1, 2))
    parser.add_argument("--detail-retries", type=int, default=1, choices=(0, 1, 2))
    parser.add_argument("--requests-per-minute", type=int, default=30)
    parser.add_argument("--source-timeout-seconds", type=int, default=600)
    parser.add_argument("--acquisition-timeout-seconds", type=float, default=20.0)
    parser.add_argument(
        "--allow-llm-fallback",
        action="store_true",
        help="Enable grounded GPU extraction only after deterministic detail extraction misses",
    )
    parser.add_argument(
        "--allow-unknown-cross-domain-redirects",
        action="store_true",
        help="Allow unclassified public redirect hosts during a controlled campaign",
    )
    parser.add_argument(
        "--require-target",
        action="store_true",
        help="Exit with status 3 when the complete campaign has fewer certified sources than target",
    )
    return parser


def _mode(args: argparse.Namespace) -> str:
    if args.resume:
        return "resume"
    if args.retry_actionable:
        return "retry-actionable"
    if args.retry_gpu_eligible:
        return "retry-gpu-eligible"
    if args.analyze_only:
        return "analyze-only"
    return "full"


def _campaign_id() -> str:
    return (
        "fleet_"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid.uuid4().hex[:8]
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    mode = _mode(args)
    root = Path(args.root).resolve()
    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = root / output_dir

    inventory = read_portal_inventory(input_path)
    if args.expected_sources is not None and len(inventory) != args.expected_sources:
        parser.error(
            f"expected {args.expected_sources} inventory sources but parsed {len(inventory)}"
        )
    if args.target_successes < 1:
        parser.error("--target-successes must be at least 1")
    if args.allow_llm_fallback and args.detail_concurrency != 1:
        parser.error("GPU/LLM campaign fallback requires --detail-concurrency 1")
    if mode == "retry-gpu-eligible" and not args.allow_llm_fallback:
        parser.error("--retry-gpu-eligible requires --allow-llm-fallback")

    options = CertificationOptions(
        max_jobs=args.max_jobs,
        max_pages=args.max_pages,
        detail_concurrency=args.detail_concurrency,
        detail_retry_attempts=args.detail_retries,
        requests_per_minute=args.requests_per_minute,
        source_timeout_seconds=args.source_timeout_seconds,
        acquisition_timeout_seconds=args.acquisition_timeout_seconds,
        allow_unknown_cross_domain_redirects=bool(
            args.allow_unknown_cross_domain_redirects
        ),
        allow_llm_fallback=bool(args.allow_llm_fallback),
    )
    campaign_id = _campaign_id()
    store = CertificationReportStore(output_dir)
    inventory_ids = {entry.source_id for entry in inventory}

    def latest() -> dict[str, dict[str, Any]]:
        return {
            source_id: payload
            for source_id, payload in store.load_latest().items()
            if source_id in inventory_ids
        }

    def refresh_artifacts() -> tuple[dict[str, Any], dict[str, str]]:
        report = build_fleet_campaign_report(
            inventory=inventory,
            latest_records=latest(),
            campaign_id=campaign_id,
            target_successes=args.target_successes,
        )
        artifacts = write_fleet_campaign_artifacts(
            output_dir=output_dir,
            report=report,
        )
        return report, artifacts

    initial_report, _ = refresh_artifacts()
    if mode in {"retry-actionable", "retry-gpu-eligible"} and initial_report[
        "accounted_count"
    ] == 0:
        parser.error(f"--{mode} requires an existing campaign report")
    selected_ids = select_campaign_source_ids(initial_report, mode=mode)

    print(
        "FLEET_CAMPAIGN_START",
        f"campaign={campaign_id}",
        f"mode={mode}",
        f"inventory={len(inventory)}",
        f"selected={len(selected_ids)}",
        f"already_accounted={initial_report['accounted_count']}",
        f"gpu_llm_fallback={'on' if options.allow_llm_fallback else 'off'}",
        f"detail_concurrency={options.detail_concurrency}",
        flush=True,
    )

    def progress(record: PortalCertificationRecord, position: int, total: int) -> None:
        refresh_artifacts()
        print(
            f"[{position}/{total}]",
            record.status.upper(),
            record.source_id,
            f"platform={record.detected_platform or 'unknown'}",
            f"route={record.resolved_route_url or '-'}",
            f"discovered={record.discovered_urls}",
            f"extracted={record.extracted_jobs}",
            f"error={record.error_type or '-'}",
            flush=True,
        )

    completed: list[PortalCertificationRecord] = []
    if selected_ids:
        completed = asyncio.run(
            certify_portal_inventory(
                input_path=input_path,
                root=root,
                output_dir=output_dir,
                options=options,
                source_ids=set(selected_ids),
                on_progress=progress,
            )
        )

    report, artifacts = refresh_artifacts()
    print(
        "FLEET_CAMPAIGN_COMPLETE",
        f"campaign={campaign_id}",
        f"mode={mode}",
        f"executed={len(completed)}",
        f"accounted={report['accounted_count']}/{report['inventory_count']}",
        f"certified={report['certified_source_count']}",
        f"blocked={report['blocked_source_count']}",
        f"actionable={report['actionable_source_count']}",
        f"gpu_eligible={report['gpu_eligible_source_count']}",
        f"target={report['target_successes']}",
        f"target_met={str(report['target_met']).lower()}",
        f"report={artifacts['report']}",
        f"manifest={artifacts['manifest']}",
        flush=True,
    )
    if args.require_target and not report["target_met"]:
        raise SystemExit(3)


if __name__ == "__main__":
    main()

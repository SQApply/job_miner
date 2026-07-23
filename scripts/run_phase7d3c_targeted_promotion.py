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
)
from src.portals.production_target25 import (
    evaluate_target25_promotion,
    read_target25_plan,
    write_target25_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run complete-catalog, read-only certification for only the nine "
            "Phase 7D3C promotion candidates and evaluate progress toward 25."
        )
    )
    parser.add_argument("--input", required=True, help="The original 102-source XLSX/CSV/TXT inventory")
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--plan",
        default="data/phase7d3c/phase7d3c_target25_plan.json",
    )
    parser.add_argument(
        "--output-dir",
        default="data/phase7d3c/promotion",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Diagnostic only: run the first N still-selected target candidates",
    )
    parser.add_argument(
        "--disable-llm-fallback",
        action="store_true",
        help="Disable the serialized local GPU/LLM fallback; deterministic extraction still runs.",
    )
    return parser


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _progress(record: PortalCertificationRecord, position: int, total: int) -> None:
    print(
        f"[{position}/{total}]",
        record.status.upper(),
        record.source_id,
        f"platform={record.detected_platform or 'unknown'}",
        f"discovered={record.discovered_urls}",
        f"attempted={record.attempted_urls}",
        f"extracted={record.extracted_jobs}",
        f"catalog_complete={str(record.catalog_complete).lower()}",
        f"elapsed={record.elapsed_seconds}s",
        f"error={record.error_type or '-'}",
        flush=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    plan = read_target25_plan(_resolve(root, args.plan))
    output_dir = _resolve(root, args.output_dir)
    source_ids = set(plan["promotion_candidate_source_ids"])
    options = CertificationOptions(
        catalog_mode="complete_catalog",
        max_jobs=None,
        max_pages=int(plan["execution_limits"]["page_safety_cap"]),
        detail_concurrency=1,
        detail_retry_attempts=1,
        requests_per_minute=int(plan["execution_limits"]["requests_per_minute"]),
        source_timeout_seconds=int(
            plan["execution_limits"]["source_timeout_seconds"]
        ),
        acquisition_timeout_seconds=float(
            plan["execution_limits"]["acquisition_timeout_seconds"]
        ),
        allow_unknown_cross_domain_redirects=False,
        allow_llm_fallback=not bool(args.disable_llm_fallback),
    )
    print(
        "PHASE_7D3C_TARGETED_PROMOTION_START",
        f"sources={len(source_ids)}",
        f"source_ids={json.dumps(plan['promotion_candidate_source_ids'], separators=(',', ':'))}",
        "catalog_mode=complete",
        "source_concurrency=1",
        "detail_concurrency=1",
        "gpu_llm_concurrency=1",
        "target_only=true",
        "mongodb_reads=false",
        "mongodb_writes=false",
        "reconciliation=false",
        "deactivation=false",
        flush=True,
    )
    asyncio.run(
        certify_portal_inventory(
            input_path=Path(args.input).resolve(),
            root=root,
            output_dir=output_dir,
            options=options,
            resume=bool(args.resume),
            only_failed=False,
            limit=args.limit,
            source_ids=source_ids,
            on_progress=_progress,
        )
    )
    summary_path = output_dir / "portal_certification_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    report = evaluate_target25_promotion(
        plan=plan,
        certification_summary=summary,
    )
    report_path = write_target25_json(
        output_dir / "phase7d3c_target25_promotion_report.json",
        report,
        checksum_field="report_sha256",
    )
    counts = report["counts"]
    print(
        "PHASE_7D3C_TARGET25_"
        + ("REACHED" if report["target_achieved"] else "NOT_REACHED"),
        f"previously_usable={counts['previously_usable']}",
        f"promoted={counts['promoted']}",
        f"complete_promotions={counts['candidate_complete_catalog']}",
        f"partial_safe_promotions={counts['candidate_partial_safe']}",
        f"failed_or_not_run={counts['candidate_not_productive_or_not_run']}",
        f"final_recurring_usable={counts['final_recurring_usable']}",
        f"shortfall={counts['shortfall']}",
        "mongodb_writes=false",
        "reconciliation=false",
        "deactivation=false",
        f"report={report_path}",
        flush=True,
    )
    if not report["target_achieved"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

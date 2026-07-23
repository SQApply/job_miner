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
    read_target25_plan,
    write_target25_json,
)
from src.portals.production_target25_repair import (
    FINAL_REPAIR_DETAIL_BUDGET,
    build_final_repair_report,
    final_repair_source_ids,
    read_prior_promotion_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the final read-only generic repair for only the four failed "
            "Phase 7D3C candidates and re-evaluate the target-25 cohort."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        help="The original 102-source XLSX/CSV/TXT portal inventory",
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--plan",
        default="data/phase7d3c/phase7d3c_target25_plan.json",
    )
    parser.add_argument(
        "--baseline-summary",
        default="data/phase7d3c/promotion/portal_certification_summary.json",
    )
    parser.add_argument(
        "--baseline-report",
        default="data/phase7d3c/promotion/phase7d3c_target25_promotion_report.json",
    )
    parser.add_argument(
        "--output-dir",
        default="data/phase7d3c/final_repair",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--disable-llm-fallback",
        action="store_true",
        help=(
            "Disable serialized local GPU/LLM extraction. Deterministic and "
            "rendered semantic extraction remain enabled."
        ),
    )
    return parser


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _progress(record: PortalCertificationRecord, position: int, total: int) -> None:
    print(
        f"[{position}/{total}]",
        record.status.upper(),
        record.source_id,
        f"platform={record.detected_platform or 'unknown'}",
        f"route={record.resolved_route_url or '-'}",
        f"discovered={record.discovered_urls}",
        f"attempted={record.attempted_urls}",
        f"extracted={record.extracted_jobs}",
        f"catalog_complete={str(record.catalog_complete).lower()}",
        f"detail_budget={record.discovery_quality.get('detail_budget') or '-'}",
        f"elapsed={record.elapsed_seconds}s",
        f"error={record.error_type or '-'}",
        flush=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    plan = read_target25_plan(_resolve(root, args.plan))
    baseline_report = read_prior_promotion_report(
        _resolve(root, args.baseline_report)
    )
    source_ids = final_repair_source_ids(
        plan=plan,
        prior_report=baseline_report,
    )
    output_dir = _resolve(root, args.output_dir)
    options = CertificationOptions(
        catalog_mode="complete_catalog",
        max_jobs=None,
        detail_budget=FINAL_REPAIR_DETAIL_BUDGET,
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
        "PHASE_7D3C1_FINAL_REPAIR_START",
        f"sources={len(source_ids)}",
        f"source_ids={json.dumps(source_ids, separators=(',', ':'))}",
        f"detail_budget_per_source={FINAL_REPAIR_DETAIL_BUDGET}",
        "source_concurrency=1",
        "detail_concurrency=1",
        "gpu_llm_concurrency=1",
        "target_only=true",
        "mongodb_reads=false",
        "mongodb_writes=false",
        "reconciliation=false",
        "deactivation=false",
        "anti_bot_bypass=false",
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
            limit=None,
            source_ids=set(source_ids),
            on_progress=_progress,
        )
    )
    baseline_summary = _read_json(_resolve(root, args.baseline_summary))
    repair_summary = _read_json(
        output_dir / "portal_certification_summary.json"
    )
    report = build_final_repair_report(
        plan=plan,
        prior_report=baseline_report,
        baseline_summary=baseline_summary,
        repair_summary=repair_summary,
    )
    report_path = write_target25_json(
        output_dir / "phase7d3c1_target25_final_report.json",
        report,
        checksum_field="report_sha256",
    )
    counts = report["counts"]
    repair = report["repair"]
    print(
        "PHASE_7D3C1_TARGET25_"
        + ("REACHED" if report["target_achieved"] else "NOT_REACHED"),
        f"repair_productive={len(repair['productive_source_ids'])}/4",
        f"final_recurring_usable={counts['final_recurring_usable']}",
        f"shortfall={counts['shortfall']}",
        f"still_failed={json.dumps(repair['still_failed_source_ids'], separators=(',', ':'))}",
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

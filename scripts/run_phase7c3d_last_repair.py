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
from src.portals.repair_campaign import audit_repair_records, load_repair_cohort


DEFAULT_COHORT = Path("configs/portal_cohorts/phase7c3d_last_repair.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the single-attempt Phase 7C3D repair cohort only. This command "
            "never expands to the complete portal inventory."
        )
    )
    parser.add_argument("--input", required=True, help="Portal inventory XLSX/CSV/TXT")
    parser.add_argument("--root", default=".", help="Job Miner repository root")
    parser.add_argument(
        "--output-dir",
        default="data/certification_phase7c3d_last_repair",
        help="Dedicated output directory for this bounded campaign",
    )
    parser.add_argument("--cohort", default=str(DEFAULT_COHORT))
    parser.add_argument(
        "--resume-missing",
        action="store_true",
        help="After interruption, run only cohort ids not already present in JSONL",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _progress(record: PortalCertificationRecord, position: int, total: int) -> None:
    print(
        f"[{position}/{total}]",
        record.status.upper(),
        record.source_id,
        f"platform={record.detected_platform or 'unknown'}",
        f"discovered={record.discovered_urls}",
        f"attempted={record.attempted_urls}",
        f"extracted={record.extracted_jobs}",
        f"elapsed={record.elapsed_seconds}s",
        f"error={record.error_type or '-'}",
        flush=True,
    )


def _existing_source_ids(output_dir: Path) -> set[str]:
    path = output_dir / "portal_certification.jsonl"
    if not path.exists():
        return set()
    values: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            source_id = str(json.loads(line).get("source_id") or "")
        except json.JSONDecodeError:
            continue
        if source_id:
            values.add(source_id)
    return values


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    cohort_path = Path(args.cohort)
    if not cohort_path.is_absolute():
        cohort_path = root / cohort_path

    cohort = load_repair_cohort(cohort_path)
    inventory = read_portal_inventory(input_path)
    inventory_ids = {entry.source_id for entry in inventory}
    unknown = [source_id for source_id in cohort.source_ids if source_id not in inventory_ids]
    if unknown:
        raise SystemExit(f"Cohort contains source ids absent from inventory: {unknown}")

    existing = _existing_source_ids(output_dir)
    if existing and not args.resume_missing:
        raise SystemExit(
            f"Output already contains {len(existing)} source results. Use --resume-missing "
            "only after an interrupted run, or choose a new output directory."
        )
    selected = [
        source_id
        for source_id in cohort.source_ids
        if not (args.resume_missing and source_id in existing)
    ]
    print(
        "PHASE_7C3D_PLAN_OK",
        f"cohort={cohort.cohort_id}",
        f"inventory={len(inventory)}",
        f"selected={len(selected)}",
        "max_jobs=10",
        "gpu_llm_fallback=true",
        "anti_bot_bypass=false",
    )
    for group, source_ids in cohort.groups.items():
        print(f"  {group}: {len(source_ids)}")
    if args.dry_run:
        print(json.dumps(selected, indent=2))
        return

    if selected:
        asyncio.run(
            certify_portal_inventory(
                input_path=input_path,
                root=root,
                output_dir=output_dir,
                options=CertificationOptions(
                    max_jobs=10,
                    max_pages=3,
                    detail_concurrency=1,
                    detail_retry_attempts=0,
                    requests_per_minute=30,
                    source_timeout_seconds=600,
                    acquisition_timeout_seconds=25.0,
                    allow_llm_fallback=True,
                ),
                source_ids=set(selected),
                on_progress=_progress,
            )
        )

    summary_path = output_dir / "portal_certification_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    audit = audit_repair_records(summary.get("records") or [], cohort)
    audit_path = output_dir / "phase7c3d_last_repair_audit.json"
    audit_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(
        "PHASE_7C3D_LAST_REPAIR_COMPLETE",
        f"evaluated={audit['evaluated']}/{audit['cohort_size']}",
        f"clean_successes={len(audit['clean_success_source_ids'])}",
        f"usable_partials={len(audit['usable_partial_source_ids'])}",
        f"deferred={len(audit['deferred_source_ids'])}",
        f"production_ready={audit['production_ready_after']}",
        f"stop_rule={audit['stop_rule']}",
        f"audit={audit_path}",
    )


if __name__ == "__main__":
    main()

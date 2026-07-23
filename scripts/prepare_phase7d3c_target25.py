from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.certification import read_portal_inventory
from src.portals.production_persistence import read_phase6a_ingestion_plan
from src.portals.production_target25 import (
    build_target25_plan,
    load_phase7d3b_tiers,
    load_target25_policy,
    write_target25_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build the truthful Phase 7D3C 25-source promotion plan from the "
            "signed seven-batch Phase 7D3B evidence. No network or database access occurs."
        )
    )
    parser.add_argument("--input", required=True, help="The original 102-source XLSX/CSV/TXT inventory")
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--production-plan",
        default="data/phase7d1/phase7d1_production_plan.json",
    )
    parser.add_argument("--checkpoint-dir", default="data/phase7d3b")
    parser.add_argument(
        "--policy",
        default="configs/portal_cohorts/phase7d3c_target25_policy.json",
    )
    parser.add_argument(
        "--output",
        default="data/phase7d3c/phase7d3c_target25_plan.json",
    )
    return parser


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    inventory = read_portal_inventory(Path(args.input).resolve())
    production_plan = read_phase6a_ingestion_plan(
        _resolve(root, args.production_plan)
    )
    policy = load_target25_policy(_resolve(root, args.policy))
    tiers, checkpoint_evidence = load_phase7d3b_tiers(
        _resolve(root, args.checkpoint_dir),
        expected_source_ids=list(production_plan.selected_source_ids),
    )
    plan = build_target25_plan(
        inventory=inventory,
        production_plan=production_plan,
        tiers=tiers,
        checkpoint_evidence=checkpoint_evidence,
        policy=policy,
    )
    output = write_target25_json(
        _resolve(root, args.output),
        plan,
        checksum_field="plan_sha256",
    )
    counts = plan["current_counts"]
    print(
        "PHASE_7D3C_TARGET25_PLAN_READY",
        f"complete={counts['complete_catalog']}",
        f"partial_safe={counts['partial_safe']}",
        f"recurring_usable={counts['recurring_usable']}",
        f"nonproductive={counts['nonproductive']}",
        f"promotion_candidates={counts['promotion_candidates']}",
        f"target={plan['target_production_source_count']}",
        "reconciliation=false",
        "deactivation=false",
        "network=false",
        "mongodb_reads=false",
        "mongodb_writes=false",
        f"plan={output}",
        flush=True,
    )


if __name__ == "__main__":
    main()

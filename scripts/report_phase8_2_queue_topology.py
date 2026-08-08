from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.scrape_queue_topology import ScrapeQueueTopology


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and report the Phase 8.2 Celery queue topology."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--output-dir",
        default="data/phase8_2/queue_isolation",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    root = Path(args.root).resolve()
    output_dir = (root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    topology = ScrapeQueueTopology.from_environment()
    report = topology.to_dict()
    report_path = output_dir / "phase8_2_queue_topology.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(
        "PHASE_8_2_QUEUE_TOPOLOGY_VALID "
        f"queues={len(topology.lanes)} "
        f"worker_groups={len(topology.worker_groups())} "
        "browser_isolated=true scheduler=false network=false "
        "mongodb_reads=false mongodb_writes=false "
        f"report={report_path}"
    )
    print("PHASE_8_2_WINDOWS_WORKERS")
    for ordinal, command in enumerate(topology.windows_worker_commands(), start=1):
        print(f"[{ordinal}] {command}")


if __name__ == "__main__":
    main()

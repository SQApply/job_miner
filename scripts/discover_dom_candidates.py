from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.crawl.browser_evidence import BrowserEvidenceReport
from src.portals.dom_discovery import DomCandidateDiscoverer, DomDiscoveryOptions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Infer job candidates from a Phase 7B browser-evidence report."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-candidates", type=int, default=1_000)
    parser.add_argument("--minimum-cluster-size", type=int, default=3)
    parser.add_argument("--minimum-confidence", type=float, default=0.62)
    return parser.parse_args()


def atomic_write_json(path: Path, payload: dict) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def main() -> None:
    args = parse_args()
    report = BrowserEvidenceReport.model_validate_json(
        args.input.resolve().read_text(encoding="utf-8")
    )
    discoverer = DomCandidateDiscoverer(
        DomDiscoveryOptions(
            minimum_cluster_size=args.minimum_cluster_size,
            minimum_confidence=args.minimum_confidence,
            max_candidates=args.max_candidates,
        )
    )
    batch = discoverer.discover(report)
    atomic_write_json(args.output, batch.model_dump(mode="json"))
    print(
        "PHASE_7C1_DOM_DISCOVERY_OK",
        f"candidates={len(batch.candidates)}",
        f"urls={len(batch.discovered_urls)}",
        f"linkless={len(batch.linkless_candidates)}",
        f"clusters={int(batch.metrics.get('clusters_qualified') or 0)}",
        f"output={args.output.resolve()}",
    )
    raise SystemExit(0 if batch.candidates else 2)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.production_semantic_closeout import (
    PHASE_7D2C1_ACKNOWLEDGEMENT,
    build_phase7d2c1_semantic_closeout,
    require_phase7d2c1_acknowledgement,
    write_phase7d2c1_semantic_closeout,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline Phase 7D2C.1 semantic-idempotency closeout for the observed "
            "two bounded content updates. This command performs no scraping and "
            "does not connect to or write MongoDB."
        )
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--first-write-report",
        default="data/phase7d2b/phase7d2b_write_report.json",
    )
    parser.add_argument(
        "--first-write-manifest",
        default="data/phase7d2b/phase7d2b_first_write_manifest.json",
    )
    parser.add_argument(
        "--rerun-manifest",
        default="data/phase7d2c/phase7d2c_rerun_manifest.json",
    )
    parser.add_argument(
        "--strict-report",
        default="data/phase7d2c/phase7d2c_idempotency_report.json",
    )
    parser.add_argument(
        "--phase6f-closeout",
        default="data/phase7d2c/phase7d2c_phase6f_closeout.json",
    )
    parser.add_argument(
        "--output",
        default="data/phase7d2c/phase7d2c_semantic_closeout.json",
    )
    parser.add_argument("--accept-bounded-content-variance", default="")
    return parser


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    output_path = _resolve(root, args.output)
    try:
        require_phase7d2c1_acknowledgement(
            args.accept_bounded_content_variance
        )
        report = build_phase7d2c1_semantic_closeout(
            first_write_report_path=_resolve(root, args.first_write_report),
            first_write_manifest_path=_resolve(root, args.first_write_manifest),
            rerun_manifest_path=_resolve(root, args.rerun_manifest),
            strict_report_path=_resolve(root, args.strict_report),
            phase6f_closeout_path=_resolve(root, args.phase6f_closeout),
        )
        report_output = write_phase7d2c1_semantic_closeout(output_path, report)
        semantic = report["semantic_idempotency"]
        print(
            "PHASE_7D2C1_SEMANTIC_CLOSEOUT_"
            + ("PASSED" if report["ready_for_phase7d3"] else "FAILED"),
            f"accepted={semantic['accepted']}",
            f"inserted={semantic['inserted']}",
            f"updated={semantic['updated']}",
            f"unchanged={semantic['unchanged']}",
            f"variance_rate={semantic['content_variance_rate']}",
            f"ready_for_phase7d3={str(report['ready_for_phase7d3']).lower()}",
            "network=false",
            "mongodb_reads=false",
            "mongodb_writes=false",
            f"report={report_output}",
            flush=True,
        )
        if not report["ready_for_phase7d3"]:
            print(
                "PHASE_7D2C1_BLOCKERS",
                json.dumps(report["blockers"], separators=(",", ":")),
                flush=True,
            )
            raise SystemExit(2)
    except SystemExit:
        raise
    except Exception as exc:
        print(
            "PHASE_7D2C1_SEMANTIC_CLOSEOUT_FAILED",
            f"error_type={type(exc).__name__}",
            f"error={str(exc)}",
            "network=false",
            "mongodb_reads=false",
            "mongodb_writes=false",
            f"report={output_path}",
            flush=True,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

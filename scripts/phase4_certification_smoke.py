from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.certification import CertificationOptions, read_portal_inventory
from src.portals.detector import detect_portal


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        inventory = Path(directory) / "portals.csv"
        inventory.write_text(
            "name,url\n"
            "Greenhouse,https://boards.greenhouse.io/example\n"
            "Workable,https://apply.workable.com/example/\n"
            "Duplicate,https://boards.greenhouse.io/example\n",
            encoding="utf-8",
        )
        entries = read_portal_inventory(inventory)

    detection = detect_portal(
        listing_url="https://company.example/careers",
        html='<a href="https://apply.workable.com/example/">Jobs</a>',
    )
    options = CertificationOptions()
    assert len(entries) == 2
    assert detection.source_platform == "workable"
    assert options.max_jobs == 10
    assert options.detail_concurrency == 1
    print(
        "PHASE_4_CERTIFICATION_SMOKE_OK",
        f"inventory_sources={len(entries)}",
        f"detected_platform={detection.source_platform}",
        f"max_jobs={options.max_jobs}",
        f"gpu_safe_detail_concurrency={options.detail_concurrency}",
        f"source_ids={json.dumps([entry.source_id for entry in entries])}",
    )


if __name__ == "__main__":
    main()

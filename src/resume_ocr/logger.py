from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionLogger:
    def __init__(self, log_file: Path, target_id: str, session_id: str):
        self.log_file = log_file
        self.target_id = target_id
        self.session_id = session_id
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **payload: Any) -> None:
        row = {
            "ts": utc_now_iso(),
            "target_id": self.target_id,
            "session_id": self.session_id,
            "event": event,
            **payload,
        }
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

        short = payload.get("source_path") or payload.get("file_name") or payload.get("message", "")
        print(f"[LOG] {event} {short}".strip())


def build_session_logger(root: Path, log_dir: str, session_id: str) -> SessionLogger:
    log_file = root / log_dir / "resume_ocr" / f"{session_id}.jsonl"
    return SessionLogger(log_file=log_file, target_id="resume_ocr", session_id=session_id)

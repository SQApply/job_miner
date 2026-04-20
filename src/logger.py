from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionLogger:
    def __init__(self, log_file: Path, target_id: str, session_id: str):
        self.log_file = log_file
        self.target_id = target_id
        self.session_id = session_id
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **payload: Any) -> None:
        row = {
            "ts": _utc_now(),
            "target_id": self.target_id,
            "session_id": self.session_id,
            "event": event,
            **payload,
        }
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

        short = payload.get("job_url") or payload.get("page_url") or payload.get("message", "")
        print(f"[LOG] {event} {short}".strip())


def build_session_logger(root: Path, target_id: str, session_id: str) -> SessionLogger:
    logs_dir = root / "data" / "logs" / target_id
    log_file = logs_dir / f"{session_id}.jsonl"
    return SessionLogger(log_file=log_file, target_id=target_id, session_id=session_id)
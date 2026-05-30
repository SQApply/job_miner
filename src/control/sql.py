from __future__ import annotations

from pathlib import Path

SCHEMA_SQL_PATH = Path(__file__).resolve().parents[2] / "infra" / "postgres" / "init" / "001_job_miner_control.sql"


def load_schema_sql() -> str:
    return SCHEMA_SQL_PATH.read_text(encoding="utf-8")

from __future__ import annotations

import argparse
import json

from .postgres import get_postgres_engine, postgres_healthcheck
from .sql import load_schema_sql


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def health(_: argparse.Namespace) -> None:
    _print(postgres_healthcheck())


def init_schema(_: argparse.Namespace) -> None:
    sql = load_schema_sql()
    with get_postgres_engine().begin() as conn:
        conn.exec_driver_sql(sql)
    _print({"ok": True, "message": "PostgreSQL MVP schema initialized"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Job Miner PostgreSQL control-plane CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health").set_defaults(func=health)
    sub.add_parser("init-schema").set_defaults(func=init_schema)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

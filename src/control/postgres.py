from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..common.env import load_runtime_env
from ..common.constants import EnvironmentVariables

load_runtime_env()

DEFAULT_POSTGRES_URL = "postgresql+psycopg://job_miner_app:job_miner_password@localhost:5432/job_miner_control"


@lru_cache(maxsize=1)
def get_postgres_engine() -> Engine:
    database_url = os.getenv(EnvironmentVariables.POSTGRES_URL, DEFAULT_POSTGRES_URL)
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=int(os.getenv("JOB_MINER_POSTGRES_POOL_SIZE", "5")),
        max_overflow=int(os.getenv("JOB_MINER_POSTGRES_MAX_OVERFLOW", "10")),
        pool_recycle=int(os.getenv("JOB_MINER_POSTGRES_POOL_RECYCLE_SECONDS", "1800")),
        future=True,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_postgres_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )


@contextmanager
def postgres_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def postgres_healthcheck() -> dict[str, object]:
    with get_postgres_engine().connect() as conn:
        value = conn.execute(text("SELECT 1")).scalar_one()
    return {"ok": value == 1}

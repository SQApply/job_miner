from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

_request_id_var: ContextVar[str | None] = ContextVar("job_miner_request_id", default=None)


def new_uuid() -> str:
    """Return a backend-generated UUID string.

    Do not use client-provided request IDs as trusted correlation IDs.
    """
    return str(uuid.uuid4())


def set_request_id(request_id: str) -> Token[str | None]:
    return _request_id_var.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id_var.reset(token)


def get_request_id() -> str | None:
    return _request_id_var.get()

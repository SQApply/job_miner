from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

import redis

logger = logging.getLogger(__name__)

_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


def _redis_url() -> str:
    return os.getenv("JOB_MINER_REDIS_URL") or os.getenv("JOB_MINER_CELERY_BROKER_URL") or "redis://localhost:6379/0"


@dataclass
class RedisLock:
    key: str
    token: str
    acquired: bool
    client: Any | None = None

    def release(self) -> None:
        if not self.acquired or not self.client:
            return
        try:
            self.client.eval(_RELEASE_LOCK_SCRIPT, 1, self.key, self.token)
        except Exception:
            logger.exception("Failed to release Redis lock key=%s", self.key)


def acquire_candidate_recommendation_lock(candidate_id: str, task_id: str | None) -> RedisLock:
    """Acquire a candidate-scoped recommendation lock.

    This prevents multiple recommendation workers from regenerating recommendations
    for the same candidate at the same time. It is intentionally scoped only to
    recommendation generation; agent/browser/domain concurrency will be handled by
    a separate lock layer later.
    """
    ttl_seconds = int(os.getenv("JOB_MINER_RECOMMENDATION_CANDIDATE_LOCK_TTL_SECONDS", "3600"))
    fail_closed = os.getenv("JOB_MINER_RECOMMENDATION_LOCK_FAIL_CLOSED", "true").strip().lower() in {"1", "true", "yes", "on"}
    key = f"job_miner:locks:recommendations:candidate:{candidate_id}"
    token = f"{task_id or 'unknown'}:{uuid.uuid4()}"

    try:
        client = redis.Redis.from_url(_redis_url(), decode_responses=True)
        acquired = bool(client.set(key, token, nx=True, ex=max(ttl_seconds, 60)))
        return RedisLock(key=key, token=token, acquired=acquired, client=client)
    except Exception:
        logger.exception("Failed to acquire candidate recommendation lock candidate_id=%s", candidate_id)
        return RedisLock(key=key, token=token, acquired=not fail_closed, client=None)

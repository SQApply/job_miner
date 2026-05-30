from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


@lru_cache(maxsize=1)
def load_runtime_env(root: str | Path | None = None) -> Path:
    """Load the repository-level .env file once for local runtime commands.

    Docker Compose reads .env for compose interpolation. This helper covers
    non-Docker Python entrypoints such as uvicorn, Celery workers, CLI commands,
    and smoke tests so developers do not need to export variables manually.

    Existing OS environment variables intentionally win over .env values. This
    is important for production where secrets are injected by the platform.
    """
    if root is not None:
        repo_root = Path(root).resolve()
    else:
        # src/common/env.py -> src/common -> src -> repo root
        repo_root = Path(__file__).resolve().parents[2]

    load_dotenv(repo_root / ".env", override=False)
    return repo_root

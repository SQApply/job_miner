from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

_MAX_TEXT_BYTES = 2_000_000


def _safe_slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip())
    value = value.strip("-._")
    return value[:120] or "artifact"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_bytes(path: Path, data: bytes) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "size_bytes": len(data),
        "sha256": _sha256_bytes(data),
    }


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def save_portal_artifact(
    *,
    root: Path,
    portal_id: str,
    run_session_id: str,
    category: str,
    artifact_type: str,
    name: str,
    content: str | bytes | dict[str, Any] | list[Any] | None,
    mime_type: str,
    extension: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if content is None:
        return None

    if isinstance(content, (dict, list)):
        data = json.dumps(content, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    elif isinstance(content, str):
        data = content.encode("utf-8", errors="ignore")[:_MAX_TEXT_BYTES]
    else:
        data = bytes(content)[:_MAX_TEXT_BYTES]

    if not data:
        return None

    clean_portal = _safe_slug(portal_id)
    clean_run = _safe_slug(run_session_id)
    clean_name = _safe_slug(name)
    extension = extension if extension.startswith(".") else f".{extension}"
    base_dir = root / "data" / "portal_artifacts" / clean_portal / clean_run
    path = base_dir / f"{clean_name}{extension}"
    stats = _write_bytes(path, data)
    artifact_id = f"portal:{clean_portal}:{clean_run}:{category}:{artifact_type}:{clean_name}"
    return {
        "artifact_id": artifact_id[:512],
        "artifact_category": category,
        "artifact_type": artifact_type,
        "file_name": path.name,
        "relative_path": _relative(path, root),
        "file_extension": extension,
        "mime_type": mime_type,
        "storage_mode": "temporary_local",
        "external_uri": None,
        "metadata": metadata or {},
        **stats,
    }


def result_artifacts(
    *,
    root: Path,
    portal_id: str,
    run_session_id: str,
    prefix: str,
    result: Any,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    html = getattr(result, "cleaned_html", None) or getattr(result, "html", None)
    markdown = getattr(result, "markdown", None) or getattr(result, "fit_markdown", None) or getattr(result, "text", None)
    error_message = getattr(result, "error_message", None)

    for artifact in (
        save_portal_artifact(
            root=root,
            portal_id=portal_id,
            run_session_id=run_session_id,
            category="portal_crawl",
            artifact_type="html_snapshot",
            name=f"{prefix}_page",
            content=html,
            mime_type="text/html",
            extension=".html",
            metadata=metadata,
        ),
        save_portal_artifact(
            root=root,
            portal_id=portal_id,
            run_session_id=run_session_id,
            category="portal_crawl",
            artifact_type="markdown_snapshot",
            name=f"{prefix}_text",
            content=markdown,
            mime_type="text/markdown",
            extension=".md",
            metadata=metadata,
        ),
        save_portal_artifact(
            root=root,
            portal_id=portal_id,
            run_session_id=run_session_id,
            category="portal_crawl",
            artifact_type="error_snapshot",
            name=f"{prefix}_error",
            content={"error_message": error_message} if error_message else None,
            mime_type="application/json",
            extension=".json",
            metadata=metadata,
        ),
    ):
        if artifact:
            artifacts.append(artifact)

    screenshot = getattr(result, "screenshot", None)
    if screenshot:
        try:
            raw = base64.b64decode(str(screenshot), validate=False)
            artifact = save_portal_artifact(
                root=root,
                portal_id=portal_id,
                run_session_id=run_session_id,
                category="portal_crawl",
                artifact_type="screenshot",
                name=f"{prefix}_screenshot",
                content=raw,
                mime_type="image/png",
                extension=".png",
                metadata=metadata,
            )
            if artifact:
                artifacts.append(artifact)
        except Exception:
            pass
    return artifacts

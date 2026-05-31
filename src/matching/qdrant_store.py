from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models

logger = logging.getLogger(__name__)

_QDRANT_NAMESPACE = uuid.UUID("7e19dbd6-5e8f-4c9d-9e89-589f0d75c001")


def stable_qdrant_point_id(raw_id: str) -> str:
    if not raw_id:
        raise ValueError("Qdrant point ID source cannot be empty.")

    return str(uuid.uuid5(_QDRANT_NAMESPACE, raw_id))


@dataclass(slots=True)
class QdrantMatchResult:
    point_id: str
    score: float
    payload: dict[str, Any]


@dataclass(slots=True)
class QdrantVectorStore:
    url: str = "http://localhost:6333"
    api_key: str | None = None
    prefer_grpc: bool = False
    client: QdrantClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        logger.info("Connecting to Qdrant at %s", self.url)

        self.client = QdrantClient(
            url=self.url,
            api_key=self.api_key,
            prefer_grpc=self.prefer_grpc,
        )

    def collection_exists(self, collection_name: str) -> bool:
        try:
            self.client.get_collection(collection_name=collection_name)
            return True
        except Exception:
            return False

    def healthcheck(self) -> dict[str, Any]:
        """Return a small health payload for CLI/API diagnostics."""
        try:
            collections = self.client.get_collections()
            names = [
                item.name
                for item in getattr(collections, "collections", [])
            ]
            return {
                "ok": True,
                "url": self.url,
                "collections": names,
            }
        except Exception as exc:
            logger.exception("Qdrant healthcheck failed url=%s", self.url)
            return {
                "ok": False,
                "url": self.url,
                "error": str(exc),
            }

    def ensure_collection(
        self,
        collection_name: str,
        *,
        vector_size: int,
        recreate: bool = False,
    ) -> None:
        if vector_size <= 0:
            raise ValueError(f"Invalid vector size: {vector_size}")

        logger.info(
            "Ensuring Qdrant collection=%s vector_size=%s recreate=%s",
            collection_name,
            vector_size,
            recreate,
        )

        if self.collection_exists(collection_name):
            if recreate:
                logger.warning("Deleting existing Qdrant collection=%s", collection_name)
                self.client.delete_collection(collection_name=collection_name)
            else:
                info = self.client.get_collection(collection_name=collection_name)
                existing_size = self._extract_existing_vector_size(info)

                if existing_size is not None and existing_size != vector_size:
                    raise ValueError(
                        f"Collection {collection_name} already exists with vector size "
                        f"{existing_size}, but current embedding model returns {vector_size}. "
                        "Use --recreate if you changed embedding models."
                    )

                logger.info("Qdrant collection already exists: %s", collection_name)
                return

        logger.info("Creating Qdrant collection=%s", collection_name)

        self.client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(
                size=vector_size,
                distance=models.Distance.COSINE,
            ),
        )

    def _extract_existing_vector_size(self, collection_info: Any) -> int | None:
        try:
            vectors_config = collection_info.config.params.vectors

            if hasattr(vectors_config, "size"):
                return int(vectors_config.size)

            if isinstance(vectors_config, dict):
                default_vector = vectors_config.get("") or next(iter(vectors_config.values()))
                if hasattr(default_vector, "size"):
                    return int(default_vector.size)

        except Exception:
            return None

        return None

    def upsert(
        self,
        collection_name: str,
        *,
        ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
        batch_size: int = 64,
    ) -> list[str]:
        if not ids:
            logger.warning("Qdrant upsert skipped because ids list is empty.")
            return []

        if not (len(ids) == len(vectors) == len(payloads)):
            raise ValueError(
                "Qdrant upsert input length mismatch: "
                f"ids={len(ids)}, vectors={len(vectors)}, payloads={len(payloads)}"
            )

        logger.info(
            "Upserting %s points into Qdrant collection=%s batch_size=%s",
            len(ids),
            collection_name,
            batch_size,
        )

        point_ids: list[str] = []

        for start in range(0, len(ids), batch_size):
            end = min(start + batch_size, len(ids))
            batch_point_ids: list[str] = []
            points: list[models.PointStruct] = []

            for i in range(start, end):
                point_id = stable_qdrant_point_id(ids[i])
                batch_point_ids.append(point_id)

                points.append(
                    models.PointStruct(
                        id=point_id,
                        vector=vectors[i],
                        payload=payloads[i],
                    )
                )

            logger.info(
                "Qdrant upsert batch collection=%s start=%s end=%s count=%s",
                collection_name,
                start,
                end,
                len(points),
            )

            self.client.upsert(
                collection_name=collection_name,
                points=points,
                wait=True,
            )

            point_ids.extend(batch_point_ids)

        logger.info(
            "Finished Qdrant upsert collection=%s total_points=%s",
            collection_name,
            len(point_ids),
        )

        return point_ids

    def search(
        self,
        collection_name: str,
        *,
        query_vector: list[float],
        top_n: int,
        query_filter: models.Filter | None = None,
    ) -> list[QdrantMatchResult]:
        logger.info(
            "Searching Qdrant collection=%s top_n=%s vector_size=%s",
            collection_name,
            top_n,
            len(query_vector),
        )

        try:
            results = self.client.search(
                collection_name=collection_name,
                query_vector=query_vector,
                query_filter=query_filter,
                limit=top_n,
                with_payload=True,
            )
        except AttributeError:
            response = self.client.query_points(
                collection_name=collection_name,
                query=query_vector,
                query_filter=query_filter,
                limit=top_n,
                with_payload=True,
            )
            results = response.points

        matches: list[QdrantMatchResult] = []

        for item in results:
            payload = item.payload or {}

            matches.append(
                QdrantMatchResult(
                    point_id=str(item.id),
                    score=float(item.score),
                    payload=dict(payload),
                )
            )

        logger.info(
            "Qdrant search completed collection=%s returned=%s",
            collection_name,
            len(matches),
        )

        return matches
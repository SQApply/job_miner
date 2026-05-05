from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import requests


def clean_text_for_embedding(text: str, *, max_chars: int) -> str:
    text = " ".join((text or "").replace("\x00", " ").split())
    if len(text) <= max_chars:
        return text
    marker = "\n\n[...TEXT_TRUNCATED_FOR_EMBEDDING...]\n\n"
    head = int(max_chars * 0.75); tail = max_chars - head - len(marker)
    return text[:head] + marker + text[-tail:] if tail > 0 else text[:max_chars]


def batched(items: list[str], batch_size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), batch_size):
        yield items[i : i + batch_size]


@dataclass(slots=True)
class OllamaEmbedder:
    model: str = "embeddinggemma"
    base_url: str = "http://localhost:11434"
    batch_size: int = 16
    timeout_seconds: int = 600
    max_chars_per_text: int = 12000
    truncate: bool = True

    def embed_one(self, text: str) -> list[float]:
        vectors = self.embed_many([text])
        if not vectors:
            raise RuntimeError("Ollama returned no embeddings")
        return vectors[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        cleaned = [clean_text_for_embedding(t, max_chars=self.max_chars_per_text) for t in texts]
        vectors: list[list[float]] = []
        for batch in batched(cleaned, self.batch_size):
            vectors.extend(self._embed_batch(batch))
        if len(vectors) != len(texts):
            raise RuntimeError(f"Embedding count mismatch. Expected {len(texts)}, got {len(vectors)}")
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        res = requests.post(self.base_url.rstrip("/") + "/api/embed", json={"model": self.model, "input": texts, "truncate": self.truncate}, timeout=self.timeout_seconds)
        res.raise_for_status()
        data = res.json(); embeddings = data.get("embeddings")
        if not isinstance(embeddings, list):
            raise RuntimeError(f"Invalid Ollama embedding response: {data}")
        return [[float(v) for v in emb] for emb in embeddings]

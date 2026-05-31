from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..infrastructure.mongo import get_mongo_database
from ..infrastructure.settings import load_app_settings
from ..warehouse.repositories import WarehouseRepository
from .mongo_matcher import match_candidates_from_mongo
from .mongo_qdrant_sync import build_embedder, build_vector_store, index_candidate_towers, index_job_towers


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _objects() -> tuple[WarehouseRepository, Any, Any, Any]:
    settings = load_app_settings()
    return WarehouseRepository(get_mongo_database()), build_vector_store(settings.vector), build_embedder(settings.vector), settings


def health(_: argparse.Namespace) -> None:
    _, store, embedder, settings = _objects()
    _print({
        "qdrant": store.healthcheck(),
        "embedding_model": embedder.model,
        "ollama_url": settings.vector.ollama_url,
    })


def index_jobs(args: argparse.Namespace) -> None:
    repo, store, embedder, settings = _objects(); _print(index_job_towers(repo=repo, store=store, embedder=embedder, collection_name=args.jobs_collection or settings.vector.jobs_collection, recreate=args.recreate, only_pending=args.only_pending, limit=args.limit))


def index_candidates(args: argparse.Namespace) -> None:
    repo, store, embedder, settings = _objects()
    _print(index_candidate_towers(
        repo=repo,
        store=store,
        embedder=embedder,
        collection_name=args.candidates_collection or settings.vector.candidates_collection,
        recreate=args.recreate,
        only_pending=args.only_pending,
        limit=args.limit,
        candidate_id=args.candidate_id,
    ))


def match(args: argparse.Namespace) -> None:
    repo, store, embedder, settings = _objects()
    _print(match_candidates_from_mongo(
        repo=repo,
        store=store,
        embedder=embedder,
        jobs_collection=args.jobs_collection or settings.vector.jobs_collection,
        top_n=args.top_n,
        output_dir=Path(args.output_dir),
        limit=args.limit,
        candidate_id=args.candidate_id,
    ))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Qdrant matching commands backed by MongoDB warehouse"); p.add_argument("--root", default="."); sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("health").set_defaults(func=health)
    ij = sub.add_parser("index-jobs-from-mongo"); ij.add_argument("--jobs-collection", default=None); ij.add_argument("--recreate", action="store_true"); ij.add_argument("--only-pending", action="store_true"); ij.add_argument("--limit", type=int, default=None); ij.set_defaults(func=index_jobs)
    ic = sub.add_parser("index-candidates-from-mongo"); ic.add_argument("--candidates-collection", default=None); ic.add_argument("--recreate", action="store_true"); ic.add_argument("--only-pending", action="store_true"); ic.add_argument("--limit", type=int, default=None); ic.add_argument("--candidate-id", default=None); ic.set_defaults(func=index_candidates)
    m = sub.add_parser("match-candidates-from-mongo"); m.add_argument("--jobs-collection", default=None); m.add_argument("--top-n", type=int, default=10); m.add_argument("--limit", type=int, default=None); m.add_argument("--candidate-id", default=None); m.add_argument("--output-dir", default="data/processed/matching"); m.set_defaults(func=match)
    return p


def main() -> None:
    args = build_parser().parse_args(); args.func(args)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..infrastructure.mongo import get_mongo_database
from ..warehouse.repositories import WarehouseRepository
from .embeddings import OllamaEmbedder
from .feedback import add_feedback, export_training_pairs, init_optimization_indexes
from .llm_reranker import LLMJobReranker
from .qdrant_store import QdrantVectorStore
from .reranked_matcher import match_candidates_with_llm_rerank


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _repo() -> WarehouseRepository:
    db = get_mongo_database()
    return WarehouseRepository(db)


def _vector_store(args: argparse.Namespace) -> QdrantVectorStore:
    return QdrantVectorStore(
        url=args.qdrant_url,
        api_key=args.qdrant_api_key,
    )


def _embedder(args: argparse.Namespace) -> OllamaEmbedder:
    return OllamaEmbedder(
        model=args.embedding_model,
        base_url=args.ollama_url,
        batch_size=args.embedding_batch_size,
        max_chars_per_text=args.max_chars_per_text,
    )


def _reranker(args: argparse.Namespace) -> LLMJobReranker:
    return LLMJobReranker(
        model=args.llm_model,
        base_url=args.ollama_url,
        timeout_seconds=args.llm_timeout_seconds,
        chunk_size=args.llm_chunk_size,
        num_ctx=args.llm_num_ctx,
        num_predict=args.llm_num_predict,
    )


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".")
    parser.add_argument("--qdrant-url", default=os.getenv("JOB_MINER_QDRANT_URL", "http://localhost:6333"))
    parser.add_argument("--qdrant-api-key", default=os.getenv("JOB_MINER_QDRANT_API_KEY") or None)
    parser.add_argument("--ollama-url", default=os.getenv("JOB_MINER_OLLAMA_URL", "http://localhost:11434"))
    parser.add_argument("--embedding-model", default=os.getenv("JOB_MINER_EMBEDDING_MODEL", "embeddinggemma"))
    parser.add_argument("--embedding-batch-size", type=int, default=16)
    parser.add_argument("--max-chars-per-text", type=int, default=12000)


def cmd_init_indexes(args: argparse.Namespace) -> None:
    os.chdir(args.root)
    repo = _repo()
    _print(init_optimization_indexes(repo.db))


def cmd_match(args: argparse.Namespace) -> None:
    os.chdir(args.root)

    repo = _repo()
    store = _vector_store(args)
    embedder = _embedder(args)
    reranker = _reranker(args)

    summary = match_candidates_with_llm_rerank(
        repo=repo,
        store=store,
        embedder=embedder,
        reranker=reranker,
        jobs_collection=args.jobs_collection,
        top_k=args.top_k,
        final_top_n=args.final_top_n,
        output_dir=Path(args.output_dir),
        candidates_limit=args.candidates_limit,
    )

    _print(summary)


def cmd_add_feedback(args: argparse.Namespace) -> None:
    os.chdir(args.root)

    repo = _repo()

    result = add_feedback(
        db=repo.db,
        candidate_id=args.candidate_id,
        job_id=args.job_id,
        label=args.label,
        match_run_id=args.match_run_id,
        reason=args.reason,
        source=args.source,
        created_by=args.created_by,
    )

    _print(result)


def cmd_export_training_pairs(args: argparse.Namespace) -> None:
    os.chdir(args.root)

    repo = _repo()

    result = export_training_pairs(
        db=repo.db,
        output_path=Path(args.output_path),
    )

    _print(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="LLM reranking and feedback CLI for candidate-job recommendations."
    )

    _add_common_args(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-optimization-indexes")
    init_parser.set_defaults(func=cmd_init_indexes)

    match_parser = subparsers.add_parser("match")
    match_parser.add_argument("--jobs-collection", default="job_miner_jobs")
    match_parser.add_argument("--top-k", type=int, default=100)
    match_parser.add_argument("--final-top-n", type=int, default=10)
    match_parser.add_argument("--output-dir", default="data/processed/matching_llm")
    match_parser.add_argument("--candidates-limit", type=int, default=None)
    match_parser.add_argument("--llm-model", default=os.getenv("JOB_MINER_LLM_RERANKER_MODEL", "qwen2.5:3b"))
    match_parser.add_argument("--llm-timeout-seconds", type=int, default=600)
    match_parser.add_argument("--llm-chunk-size", type=int, default=3)
    match_parser.add_argument("--llm-num-ctx", type=int, default=8192)
    match_parser.add_argument("--llm-num-predict", type=int, default=4096)
    match_parser.set_defaults(func=cmd_match)

    feedback_parser = subparsers.add_parser("add-feedback")
    feedback_parser.add_argument("--candidate-id", required=True)
    feedback_parser.add_argument("--job-id", required=True)
    feedback_parser.add_argument(
        "--label",
        required=True,
        choices=[
            "accepted",
            "shortlisted",
            "applied",
            "good_match",
            "rejected",
            "irrelevant",
            "bad_match",
            "neutral",
        ],
    )
    feedback_parser.add_argument("--match-run-id", default=None)
    feedback_parser.add_argument("--reason", default=None)
    feedback_parser.add_argument("--source", default="manual")
    feedback_parser.add_argument("--created-by", default=None)
    feedback_parser.set_defaults(func=cmd_add_feedback)

    export_parser = subparsers.add_parser("export-training-pairs")
    export_parser.add_argument(
        "--output-path",
        default="data/processed/training/candidate_job_training_pairs_latest.jsonl",
    )
    export_parser.set_defaults(func=cmd_export_training_pairs)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
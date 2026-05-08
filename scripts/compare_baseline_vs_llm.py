from __future__ import annotations

import csv
import json
from pathlib import Path
from statistics import mean
from typing import Any


BASELINE_PATH = Path("data/processed/matching/candidate_job_matches_latest.json")
LLM_PATH = Path("data/processed/matching_llm/candidate_job_matches_llm_latest.json")
OUT_DIR = Path("data/processed/comparison")


def read_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def as_candidate_groups(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise ValueError("Expected top-level JSON list of candidate groups.")


def get_baseline_rank(match: dict[str, Any]) -> int | None:
    value = match.get("rank") or match.get("baseline_rank")
    try:
        return int(value)
    except Exception:
        return None


def get_llm_rank(match: dict[str, Any]) -> int | None:
    value = match.get("final_rank") or match.get("rank")
    try:
        return int(value)
    except Exception:
        return None


def get_job_id(match: dict[str, Any]) -> str:
    return str(match.get("job_id") or "").strip()


def get_candidate_id(group: dict[str, Any]) -> str:
    return str(group.get("candidate_id") or group.get("resume_id") or "").strip()


def get_candidate_name(group: dict[str, Any]) -> str:
    return str(group.get("candidate_name") or group.get("full_name") or "").strip()


def get_matches(group: dict[str, Any]) -> list[dict[str, Any]]:
    matches = group.get("matches")
    return matches if isinstance(matches, list) else []


def avg(values: list[float]) -> float:
    return round(mean(values), 6) if values else 0.0


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        path.write_text("", encoding="utf-8")
        return

    fieldnames = list(rows[0].keys())

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    baseline_groups = as_candidate_groups(read_json(BASELINE_PATH))
    llm_groups = as_candidate_groups(read_json(LLM_PATH))

    baseline_by_candidate = {
        get_candidate_id(group): group
        for group in baseline_groups
    }

    llm_by_candidate = {
        get_candidate_id(group): group
        for group in llm_groups
    }

    all_candidate_ids = sorted(set(baseline_by_candidate) | set(llm_by_candidate))

    by_candidate_rows: list[dict[str, Any]] = []
    rank_change_rows: list[dict[str, Any]] = []

    baseline_scores: list[float] = []
    llm_scores: list[float] = []

    for candidate_id in all_candidate_ids:
        baseline_group = baseline_by_candidate.get(candidate_id, {})
        llm_group = llm_by_candidate.get(candidate_id, {})

        candidate_name = (
            get_candidate_name(llm_group)
            or get_candidate_name(baseline_group)
        )

        baseline_matches = get_matches(baseline_group)
        llm_matches = get_matches(llm_group)

        baseline_top = [
            get_job_id(match)
            for match in baseline_matches
            if get_job_id(match)
        ]

        llm_top = [
            get_job_id(match)
            for match in llm_matches
            if get_job_id(match)
        ]

        baseline_rank_by_job = {
            get_job_id(match): get_baseline_rank(match)
            for match in baseline_matches
            if get_job_id(match)
        }

        llm_rank_by_job = {
            get_job_id(match): get_llm_rank(match)
            for match in llm_matches
            if get_job_id(match)
        }

        overlap = sorted(set(baseline_top).intersection(set(llm_top)))
        new_in_llm_top = sorted(set(llm_top) - set(baseline_top))
        dropped_from_baseline_top = sorted(set(baseline_top) - set(llm_top))

        for match in baseline_matches:
            try:
                baseline_scores.append(float(match.get("score")))
            except Exception:
                pass

        for match in llm_matches:
            try:
                llm_scores.append(float(match.get("final_score_0_100")))
            except Exception:
                pass

        moved_up = 0
        moved_down = 0
        same_rank = 0

        for match in llm_matches:
            job_id = get_job_id(match)
            if not job_id:
                continue

            baseline_rank = match.get("baseline_rank") or baseline_rank_by_job.get(job_id)
            llm_rank = get_llm_rank(match)

            try:
                baseline_rank_int = int(baseline_rank)
                llm_rank_int = int(llm_rank)
                delta = baseline_rank_int - llm_rank_int
            except Exception:
                baseline_rank_int = None
                llm_rank_int = None
                delta = None

            if delta is not None:
                if delta > 0:
                    moved_up += 1
                elif delta < 0:
                    moved_down += 1
                else:
                    same_rank += 1

            rank_change_rows.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_name": candidate_name,
                    "job_id": job_id,
                    "title": match.get("title"),
                    "company": match.get("company"),
                    "baseline_rank": baseline_rank_int,
                    "llm_final_rank": llm_rank_int,
                    "rank_delta_positive_means_moved_up": delta,
                    "baseline_score_0_1": match.get("baseline_score_0_1"),
                    "llm_score_0_100": match.get("final_score_0_100"),
                    "llm_decision": match.get("llm_decision"),
                    "llm_reason": match.get("llm_reason"),
                }
            )

        by_candidate_rows.append(
            {
                "candidate_id": candidate_id,
                "candidate_name": candidate_name,
                "baseline_match_count": len(baseline_matches),
                "llm_match_count": len(llm_matches),
                "top10_overlap_count": len(overlap),
                "top10_overlap_percent": round((len(overlap) / 10) * 100, 2),
                "new_jobs_in_llm_top10_count": len(new_in_llm_top),
                "dropped_baseline_jobs_count": len(dropped_from_baseline_top),
                "moved_up_count": moved_up,
                "moved_down_count": moved_down,
                "same_rank_count": same_rank,
                "baseline_top_job_ids": " | ".join(baseline_top),
                "llm_top_job_ids": " | ".join(llm_top),
                "new_jobs_in_llm_top10": " | ".join(new_in_llm_top),
                "dropped_from_baseline_top10": " | ".join(dropped_from_baseline_top),
            }
        )

    summary = {
        "baseline_file": str(BASELINE_PATH),
        "llm_file": str(LLM_PATH),
        "candidate_count_baseline": len(baseline_groups),
        "candidate_count_llm": len(llm_groups),
        "candidate_count_compared": len(all_candidate_ids),
        "baseline_total_matches": sum(len(get_matches(group)) for group in baseline_groups),
        "llm_total_matches": sum(len(get_matches(group)) for group in llm_groups),
        "baseline_average_score_0_1": avg(baseline_scores),
        "baseline_max_score_0_1": round(max(baseline_scores), 6) if baseline_scores else 0.0,
        "llm_average_score_0_100": avg(llm_scores),
        "llm_max_score_0_100": round(max(llm_scores), 6) if llm_scores else 0.0,
        "average_top10_overlap_percent": avg(
            [float(row["top10_overlap_percent"]) for row in by_candidate_rows]
        ),
        "total_new_jobs_in_llm_top10": sum(
            int(row["new_jobs_in_llm_top10_count"]) for row in by_candidate_rows
        ),
        "total_dropped_baseline_jobs": sum(
            int(row["dropped_baseline_jobs_count"]) for row in by_candidate_rows
        ),
        "total_moved_up": sum(int(row["moved_up_count"]) for row in by_candidate_rows),
        "total_moved_down": sum(int(row["moved_down_count"]) for row in by_candidate_rows),
    }

    summary_path = OUT_DIR / "baseline_vs_llm_summary.json"
    by_candidate_path = OUT_DIR / "baseline_vs_llm_by_candidate.csv"
    rank_changes_path = OUT_DIR / "baseline_vs_llm_rank_changes.csv"

    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    write_csv(by_candidate_path, by_candidate_rows)
    write_csv(rank_changes_path, rank_change_rows)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print()
    print(f"Wrote: {summary_path}")
    print(f"Wrote: {by_candidate_path}")
    print(f"Wrote: {rank_changes_path}")


if __name__ == "__main__":
    main()
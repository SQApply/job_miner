from __future__ import annotations

import subprocess
from typing import Any


def _run(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:
        return None


def _parse_csv_lines(raw: str | None, columns: list[str]) -> list[dict[str, Any]]:
    if not raw:
        return []
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == len(columns):
            rows.append(dict(zip(columns, parts)))
    return rows


def query_gpu_snapshot() -> dict[str, Any]:
    gpu_raw = _run([
        "nvidia-smi",
        "--query-gpu=index,name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw",
        "--format=csv,noheader,nounits",
    ])
    proc_raw = _run([
        "nvidia-smi",
        "--query-compute-apps=pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
    ])
    return {
        "gpus": _parse_csv_lines(gpu_raw, [
            "index", "name", "utilization_gpu_pct", "utilization_mem_pct",
            "memory_used_mb", "memory_total_mb", "temperature_c", "power_w",
        ]),
        "compute_processes": _parse_csv_lines(proc_raw, ["pid", "process_name", "used_memory_mb"]),
    }

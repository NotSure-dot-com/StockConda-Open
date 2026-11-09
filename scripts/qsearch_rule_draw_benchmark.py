"""Reproducible A/B benchmark for the settled rule-draw QSearch path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
from typing import Sequence, TypedDict, cast


BENCHMARK_CODE = r"""
import json
import time
import gc

import chess

from chess_engine.stockconda import MATE_SCORE, UCIEngine

engine = UCIEngine.__new__(UCIEngine)
engine.stop_flag = False
engine._active_cancel = None
engine._stop_check_counter = 0
engine._stop_check_threshold = 10**9
engine._search_time_limit = None
engine._search_start_time = 0.0
board = chess.Board("7k/8/8/8/8/8/8/KR6 w - - 100 51")
for _ in range(WARMUP_CALLS):
    result = engine.quiescence_search(board, -MATE_SCORE, MATE_SCORE, 1)
gc.disable()
wall_seconds = []
process_seconds = []
for _ in range(BATCHES):
    wall_started = time.perf_counter()
    process_started = time.process_time()
    for _ in range(CALLS):
        result = engine.quiescence_search(board, -MATE_SCORE, MATE_SCORE, 1)
    process_seconds.append(time.process_time() - process_started)
    wall_seconds.append(time.perf_counter() - wall_started)
assert result == 0
assert board.fen() == "7k/8/8/8/8/8/8/KR6 w - - 100 51"
print(json.dumps({
    "wall_seconds": wall_seconds,
    "process_seconds": process_seconds,
}))
"""


IDENTITY_CODE = r"""
import json
import os
import platform
import sys

import chess

from chess_engine.cython_ext import cython_chess

print(json.dumps({
    "python": sys.version,
    "platform": platform.platform(),
    "python_chess": chess.__version__,
    "cython_chess_file": cython_chess.__file__,
    "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
}))
"""


class Measurement(TypedDict):
    wall_seconds: list[float]
    process_seconds: list[float]
    wall_median: float
    process_median: float


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarize_pairs(
    reference: Sequence[float],
    candidate: Sequence[float],
    max_ratio: float,
    max_relative_mad: float,
) -> dict[str, object]:
    if not reference or len(reference) != len(candidate):
        raise ValueError("reference and candidate need equally many samples")
    if any(value <= 0 for value in reference) or any(
        value <= 0 for value in candidate
    ):
        raise ValueError("sample durations must be positive")

    ratios = [
        candidate_value / reference_value
        for reference_value, candidate_value in zip(reference, candidate)
    ]
    if max_ratio <= 0 or max_relative_mad <= 0:
        raise ValueError("ratio and stability limits must be positive")
    median_ratio = statistics.median(ratios)
    relative_mad = statistics.median(
        abs(ratio - median_ratio) for ratio in ratios
    ) / median_ratio
    return {
        "reference_seconds": list(reference),
        "candidate_seconds": list(candidate),
        "paired_ratios": ratios,
        "median_paired_ratio": median_ratio,
        "relative_mad": relative_mad,
        "max_ratio": max_ratio,
        "max_relative_mad": max_relative_mad,
        "status": (
            "PASS"
            if median_ratio <= max_ratio and relative_mad <= max_relative_mad
            else "HOLD"
        ),
    }


def run_python(root: Path, code: str) -> dict[str, object]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    parsed: dict[str, object] = json.loads(completed.stdout)
    return parsed


def measure(root: Path, calls: int, warmup_calls: int, batches: int) -> Measurement:
    code = (
        f"CALLS = {calls}\n"
        f"WARMUP_CALLS = {warmup_calls}\n"
        f"BATCHES = {batches}\n"
        + BENCHMARK_CODE
    )
    parsed = run_python(root, code)
    wall_seconds = list(cast(list[float], parsed["wall_seconds"]))
    process_seconds = list(cast(list[float], parsed["process_seconds"]))
    if len(wall_seconds) != batches or len(process_seconds) != batches:
        raise ValueError("benchmark subprocess returned an incomplete batch")
    return {
        "wall_seconds": wall_seconds,
        "process_seconds": process_seconds,
        "wall_median": statistics.median(wall_seconds),
        "process_median": statistics.median(process_seconds),
    }


def git_text(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def git_identity(root: Path, declared_revision: str) -> dict[str, object]:
    try:
        revision = git_text(root, "rev-parse", "HEAD")
        status = git_text(root, "status", "--porcelain", "--untracked-files=no")
    except subprocess.CalledProcessError:
        return {
            "declared_revision": declared_revision,
            "git_available": False,
            "git_revision": None,
            "git_status": None,
        }
    if not revision.startswith(declared_revision):
        raise ValueError(
            f"declared revision {declared_revision} does not match {revision}"
        )
    return {
        "declared_revision": declared_revision,
        "git_available": True,
        "git_revision": revision,
        "git_status": status,
    }


def identity(root: Path, declared_revision: str) -> dict[str, object]:
    runtime = run_python(root, IDENTITY_CODE)
    extension = Path(str(runtime["cython_chess_file"])).resolve(strict=True)
    return {
        "root": str(root),
        **git_identity(root, declared_revision),
        "python": runtime["python"],
        "platform": runtime["platform"],
        "python_chess": runtime["python_chess"],
        "cpu_affinity": runtime["cpu_affinity"],
        "stockconda_sha256": sha256(root / "src/chess_engine/stockconda.py"),
        "cython_chess_source_sha256": sha256(
            root / "src/chess_engine/cython_ext/cython_chess.pyx"
        ),
        "cython_chess_binary": str(extension),
        "cython_chess_binary_sha256": sha256(extension),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--reference-revision", required=True)
    parser.add_argument("--candidate-revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--calls", type=int, default=250_000)
    parser.add_argument("--warmup-calls", type=int, default=100_000)
    parser.add_argument("--batches", type=int, default=7)
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--max-ratio", type=float, default=1.03)
    parser.add_argument("--max-relative-mad", type=float, default=0.03)
    parser.add_argument("--allow-candidate-dirty", action="store_true")
    args = parser.parse_args()

    reference = args.reference.resolve(strict=True)
    candidate = args.candidate.resolve(strict=True)
    if reference == candidate:
        raise ValueError("reference and candidate roots must differ")
    if (
        args.calls <= 0
        or args.warmup_calls < 0
        or args.batches <= 0
        or args.pairs <= 0
        or args.max_ratio <= 0
        or args.max_relative_mad <= 0
    ):
        raise ValueError("benchmark counts and limits are invalid")

    identities = {
        "reference": identity(reference, args.reference_revision),
        "candidate": identity(candidate, args.candidate_revision),
    }
    if identities["reference"]["git_available"] and identities["reference"]["git_status"]:
        raise ValueError("reference root must be clean")
    if (
        identities["candidate"]["git_available"]
        and identities["candidate"]["git_status"]
        and not args.allow_candidate_dirty
    ):
        raise ValueError("candidate root is dirty; pass --allow-candidate-dirty")

    measurements: dict[str, list[Measurement]] = {
        "reference": [],
        "candidate": [],
    }
    for pair in range(args.pairs):
        order = (
            ("reference", "candidate")
            if pair % 2 == 0
            else ("candidate", "reference")
        )
        for label in order:
            root = reference if label == "reference" else candidate
            measurements[label].append(
                measure(root, args.calls, args.warmup_calls, args.batches)
            )

    wall_medians = {
        label: [measurement["wall_median"] for measurement in values]
        for label, values in measurements.items()
    }
    process_medians = {
        label: [measurement["process_median"] for measurement in values]
        for label, values in measurements.items()
    }
    wall_summary = summarize_pairs(
        wall_medians["reference"],
        wall_medians["candidate"],
        args.max_ratio,
        args.max_relative_mad,
    )
    process_summary = summarize_pairs(
        process_medians["reference"],
        process_medians["candidate"],
        args.max_ratio,
        args.max_relative_mad,
    )

    report = {
        "schema": 2,
        "purpose": "settled non-check rule-draw QSearch A/B benchmark",
        "runner_sha256": sha256(Path(__file__)),
        "reference": identities["reference"],
        "candidate": identities["candidate"],
        "calls_per_sample": args.calls,
        "warmup_calls": args.warmup_calls,
        "batches_per_process": args.batches,
        "pairs": args.pairs,
        "alternating_first": True,
        "fixture_fen": "7k/8/8/8/8/8/8/KR6 w - - 100 51",
        "measurements": measurements,
        "decision_metric": "wall_median_seconds",
        "wall_summary": wall_summary,
        "process_summary": process_summary,
        "status": wall_summary["status"],
    }
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())

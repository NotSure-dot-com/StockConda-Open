"""AP-04B Gate 3 frozen, unprofiled A/B performance runner.

Engine archives are immutable inputs.  This module contains no match or Elo
path and never edits an engine build.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
from importlib import util
import json
import math
import os
from pathlib import Path
import platform
import shlex
import statistics
import subprocess
import sys
import tarfile
import time
from types import ModuleType
from typing import Any


ENGINES = ("baseline", "candidate")
BLOCKS = 10
FIXTURES = 12
REPETITIONS = 5
CATEGORIES = ("tt", "capture", "killer", "other")
BASELINE_ARCHIVE_SHA256 = (
    "70b991d3d3c0bb6cd3f0907862f6b87222d63bdd6b6829e32cdf3f8ecac49473"
)
BASELINE_COMMIT = "a2d608e419c53fc0d2cdfd3baa667f4c6e124518"
PS_PATH = Path("/usr/bin/ps")


def load_runtime_policy() -> ModuleType:
    path = Path(__file__).with_name("ap04b_runtime_policy.py")
    spec = util.spec_from_file_location("ap04b_runtime_policy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load AP04B runtime policy")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME_POLICY = load_runtime_policy()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def new_output(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)


def relative_mad(values: list[float]) -> float:
    median = statistics.median(values)
    return statistics.median(abs(value - median) for value in values) / median


def decision(
    aggregate: list[float],
    fixtures: list[list[float]],
    *,
    valid: bool = True,
) -> dict[str, Any]:
    complete = (
        valid
        and len(aggregate) == BLOCKS
        and len(fixtures) == FIXTURES
        and all(len(series) == BLOCKS for series in fixtures)
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
            for series in [aggregate, *fixtures]
            for value in series
        )
    )
    if not complete:
        return {
            "status": "FAIL",
            "reason": "technical, identity, parity or incomplete sample",
        }
    aggregate_median = statistics.median(aggregate)
    fixture_medians = [statistics.median(series) for series in fixtures]
    dispersion = [relative_mad(series) for series in [aggregate, *fixtures]]
    if max(dispersion) > 0.03:
        status, reason = "HOLD", "relative MAD exceeds 3%"
    elif aggregate_median > 1.00 or max(fixture_medians) > 1.03:
        status, reason = "FAIL", "stable regression exceeds frozen threshold"
    elif aggregate_median <= 0.97:
        status, reason = (
            "PASS",
            "stable aggregate gain at least 3%, fixture bounds satisfied",
        )
    else:
        status, reason = "HOLD", "stable aggregate gain below 3%"
    return {
        "status": status,
        "reason": reason,
        "aggregate_median": aggregate_median,
        "fixture_medians": fixture_medians,
        "relative_mad": dispersion,
        "aggregate_ratios": aggregate,
        "fixture_ratios": fixtures,
    }


def ratios(
    rows: list[dict[str, Any]],
) -> tuple[list[float], list[list[float]]]:
    expected = {
        (block, fixture, engine, repetition)
        for block in range(BLOCKS)
        for fixture in range(FIXTURES)
        for engine in ENGINES
        for repetition in range(REPETITIONS)
    }
    samples: dict[tuple[int, int, str, int], int] = {}
    for row in rows:
        try:
            key = (
                row["block"],
                row["fixture"],
                row["engine"],
                row["repetition"],
            )
            duration = row["elapsed_ns"]
        except KeyError as exc:
            raise ValueError("invalid measurement grid row") from exc
        if (
            key not in expected
            or key in samples
            or not isinstance(duration, int)
            or isinstance(duration, bool)
            or duration <= 0
        ):
            raise ValueError("invalid measurement grid row")
        samples[key] = duration
    if set(samples) != expected:
        raise ValueError("incomplete measurement grid")
    totals = {
        (block, fixture, engine): sum(
            samples[block, fixture, engine, repetition]
            for repetition in range(REPETITIONS)
        )
        for block in range(BLOCKS)
        for fixture in range(FIXTURES)
        for engine in ENGINES
    }
    aggregate = [
        sum(totals[block, fixture, "candidate"] for fixture in range(FIXTURES))
        / sum(totals[block, fixture, "baseline"] for fixture in range(FIXTURES))
        for block in range(BLOCKS)
    ]
    fixture_ratios = [
        [
            totals[block, fixture, "candidate"]
            / totals[block, fixture, "baseline"]
            for block in range(BLOCKS)
        ]
        for fixture in range(FIXTURES)
    ]
    return aggregate, fixture_ratios


def host_gate(
    load1: Any,
    conflicts: list[dict[str, Any]],
) -> dict[str, str]:
    if (
        not isinstance(load1, (int, float))
        or isinstance(load1, bool)
        or not math.isfinite(load1)
        or load1 > 5.5
    ):
        return {"status": "HOLD", "reason": "load1 exceeds 5.5"}
    if conflicts:
        return {"status": "HOLD", "reason": "conflicting process observed"}
    return {"status": "PASS", "reason": "host gate satisfied"}


def engine_order(block: int) -> tuple[str, str]:
    if not 0 <= block < BLOCKS:
        raise ValueError("block outside frozen grid")
    return ENGINES if block % 2 == 0 else ("candidate", "baseline")


def validate_coverage(coverage: dict[str, dict[str, int]]) -> None:
    if set(coverage) != set(CATEGORIES):
        raise ValueError("category coverage is incomplete")
    for category in CATEGORIES:
        values = coverage[category]
        if (
            set(values) != {"selected", "cutoffs"}
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                for value in values.values()
            )
        ):
            raise ValueError(f"category coverage missing for {category}")


def is_authored_source(relative: str) -> bool:
    suffix = Path(relative).suffix
    return suffix in {".py", ".pyi", ".pyx", ".pxd", ".h"} or (
        relative.endswith("cython_chess_backend.cpp")
    )


def verify_archive(
    root: Path,
    archive: Path,
    expected_sha256: str,
) -> dict[str, str]:
    if sha(archive) != expected_sha256:
        raise RuntimeError("archive SHA-256 mismatch")
    sources: dict[str, str] = {}
    with tarfile.open(archive) as stream:
        for member in stream.getmembers():
            relative = member.name.replace("\\", "/").removeprefix("./")
            if not member.isfile() or not is_authored_source(relative):
                continue
            extracted = stream.extractfile(member)
            if extracted is None:
                raise RuntimeError(f"cannot read archive member: {relative}")
            digest = hashlib.sha256(extracted.read()).hexdigest()
            path = root / relative
            if not path.is_file() or sha(path) != digest:
                raise RuntimeError(f"archive/source mismatch: {relative}")
            sources[relative] = digest
    if not sources:
        raise RuntimeError("archive/source mismatch: no source files")
    root_sources: set[str] = set()
    for parent in (root / "src", root / "scripts"):
        if not parent.is_dir():
            continue
        for path in sorted(parent.rglob("*")):
            if not path.is_file():
                continue
            relative = str(path.relative_to(root)).replace("\\", "/")
            if is_authored_source(relative):
                root_sources.add(relative)
    unexpected = sorted(root_sources - set(sources))
    if unexpected:
        raise RuntimeError(
            f"unexpected build-root source: {unexpected[0]}"
        )
    return sources


def validate_distinct_roots(baseline: Path, candidate: Path) -> None:
    if baseline.resolve() == candidate.resolve():
        raise RuntimeError("build roots must be independent")


def validate_evidence(path: Path, expected_sha256: str) -> None:
    if sha(path) != expected_sha256:
        raise RuntimeError(f"evidence changed: {path}")


def compare_semantics(baseline: Any, candidate: Any) -> None:
    if canonical(baseline) != canonical(candidate):
        raise RuntimeError("preflight semantic mismatch")


def classify_move(
    board: Any,
    move: Any,
    tt_move: Any,
    killers: tuple[Any, Any],
) -> str:
    if tt_move is not None and move == tt_move:
        return "tt"
    if board.is_capture(move):
        return "capture"
    if move in killers:
        return "killer"
    return "other"


def loop_cutoff_category(
    board: Any,
    result: tuple[Any, Any],
    beta: int,
    tt_move: Any,
    killers: tuple[Any, Any],
    searched_moves: set[str],
) -> str | None:
    """Classify only beta cutoffs produced after searching a node move."""
    value, cutoff_move = result
    if (
        value is None
        or value < beta
        or cutoff_move is None
        or cutoff_move.uci() not in searched_moves
    ):
        return None
    return classify_move(board, cutoff_move, tt_move, killers)


def validate_host_capabilities(cpu_count: int | None, affinity: set[int]) -> None:
    if cpu_count != 22:
        raise RuntimeError(f"Gate 3 requires exactly 22 logical CPUs, got {cpu_count}")
    if not {0, 1}.issubset(affinity):
        raise RuntimeError("Gate 3 CPU affinity requires CPUs 0 and 1")


def validate_host_availability(value: Any) -> None:
    if not isinstance(value, dict):
        raise RuntimeError("Gate 3 host availability is missing")
    cpu_count = value.get("logical_cpu_count")
    affinity = value.get("available_affinity")
    if not isinstance(affinity, list) or any(
        not isinstance(cpu, int) or isinstance(cpu, bool) for cpu in affinity
    ):
        raise RuntimeError("Gate 3 host availability is invalid")
    validate_host_capabilities(cpu_count, set(affinity))


def validate_and_pin_harness() -> dict[str, Any]:
    """Validate live availability before narrowing this process to CPU 1."""
    cpu_count = os.cpu_count()
    affinity = set(os.sched_getaffinity(0))
    validate_host_capabilities(cpu_count, affinity)
    availability = {
        "logical_cpu_count": cpu_count,
        "available_affinity": sorted(affinity),
    }
    os.sched_setaffinity(0, {1})
    if set(os.sched_getaffinity(0)) != {1}:
        raise RuntimeError("Gate 3 harness affinity pinning failed")
    return availability


def validate_same_host_availability(
    preflight: dict[str, Any], current: dict[str, Any]
) -> None:
    validate_host_availability(current)
    if preflight.get("host_availability") != current:
        raise RuntimeError("Gate 3 host availability changed since preflight")


def measurement_plan() -> list[dict[str, Any]]:
    return [
        {
            "block": block,
            "fixture": fixture,
            "engine": engine,
            "repetition": repetition,
        }
        for block in range(BLOCKS)
        for fixture in range(FIXTURES)
        for engine in engine_order(block)
        for repetition in range(REPETITIONS)
    ]


def validate_worker_result(
    result: dict[str, Any],
    identity: dict[str, Any],
    semantic: dict[str, Any],
) -> None:
    valid = (
        result.get("identity") == identity
        and result.get("semantic") == semantic
        and result.get("options") == {"hash_bytes": 16777216, "book": False}
        and result.get("worker_affinity") == [0]
        and isinstance(result.get("elapsed_ns"), int)
        and not isinstance(result.get("elapsed_ns"), bool)
        and result["elapsed_ns"] > 0
        and result.get("worker_exit_code") == 0
    )
    if not valid:
        raise RuntimeError("invalid timed worker result")


def validate_preflight(
    preflight: dict[str, Any],
    manifest_sha256: str,
    runner_sha256: str,
) -> None:
    if (
        preflight.get("status") != "PASS"
        or preflight.get("manifest_sha256") != manifest_sha256
        or preflight.get("runner_sha256") != runner_sha256
    ):
        raise RuntimeError("preflight is not PASS for this manifest")
    validate_host_availability(preflight.get("host_availability"))
    try:
        validate_coverage(preflight["coverage"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("preflight coverage is not admitted") from exc


def imported_identity(root: Path) -> dict[str, Any]:
    modules = (
        "chess_engine.stockconda",
        "chess_engine.uci_search_worker",
        "chess_engine.cython_ext.cython_chess",
        "chess_engine.cython_ext.evaluation",
        "chess_engine.cython_ext.transposition",
    )
    imported: dict[str, Any] = {}
    for name in modules:
        module = importlib.import_module(name)
        if module.__file__ is None:
            raise RuntimeError(f"module has no path: {name}")
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(root / "src"):
            raise RuntimeError(f"unbound import: {name}: {path}")
        if ".cython_ext." in name and path.suffix != ".so":
            raise RuntimeError(f"native extension missing: {path}")
        imported[name] = {"path": str(path), "sha256": sha(path)}
    runtime = RUNTIME_POLICY.runtime_identity()
    RUNTIME_POLICY.validate_runtime_identity(runtime)
    return {
        "imported": imported,
        "packages": {
            "chess": RUNTIME_POLICY.imported_package_identity("chess"),
            "numpy": RUNTIME_POLICY.imported_package_location_identity("numpy"),
        },
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "affinity": sorted(os.sched_getaffinity(0)),
        "chess": importlib.import_module("chess").__version__,
        "runtime": runtime,
    }


def worker(payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(payload["root"]).resolve()
    os.sched_setaffinity(0, {0})
    identity = imported_identity(root)
    if payload["mode"] == "identity":
        return identity
    if identity != payload["identity"]:
        raise RuntimeError("worker imported identity changed")

    engine_module: Any = importlib.import_module("chess_engine.stockconda")
    chess = importlib.import_module("chess")
    engine = engine_module.UCIEngine()
    output: list[str] = []
    measurement: dict[str, Any] = {}
    coverage = {
        category: {"selected": 0, "cutoffs": 0}
        for category in CATEGORIES
    }

    def capture_output(*args: Any, **kwargs: Any) -> None:
        del kwargs
        output.append(" ".join(map(str, args)))

    engine._uci_print = capture_output
    task_type = importlib.import_module(
        "chess_engine.uci_search_worker"
    ).SearchTask

    def execute(task: Any) -> None:
        engine._active_cancel = task.cancelled
        started = time.perf_counter_ns()
        move, score = engine.execute_search_task(
            None,
            task.depth_limit,
            False,
            task.fen,
            task.moves,
        )
        elapsed = time.perf_counter_ns() - started
        engine._active_cancel = None
        measurement.update(
            elapsed_ns=elapsed,
            bestmove=move.uci() if move else None,
            score=score,
            worker_affinity=sorted(os.sched_getaffinity(0)),
        )

    engine._search_worker._execute = execute
    original_pvs = engine.pvs_search
    contexts: list[dict[str, Any]] = []

    def covered_pvs(
        board: Any,
        depth: int,
        alpha: int,
        beta: int,
        lmr_depth: int = 3,
        ply: int = 0,
        *,
        tt_context: tuple[int, int] | None = None,
    ) -> Any:
        board_at_entry = board.copy(stack=True)
        killers: tuple[Any, Any] = (None, None)
        if 0 <= ply < len(engine.killer_moves):
            killers = (
                engine.killer_moves[ply][0],
                engine.killer_moves[ply][1],
            )
        board_hash = tt_context[0] if tt_context is not None else None
        tt_move, _ = engine_module.probe_tt_move(
            board, board_hash=board_hash
        )
        context = {
            "board": board_at_entry,
            "tt_move": tt_move,
            "killers": killers,
            "selected": set(),
        }
        if contexts and board.move_stack:
            parent = contexts[-1]
            selected_move = board.move_stack[-1]
            selected_key = selected_move.uci()
            if selected_key not in parent["selected"]:
                category = classify_move(
                    parent["board"],
                    selected_move,
                    parent["tt_move"],
                    parent["killers"],
                )
                coverage[category]["selected"] += 1
                parent["selected"].add(selected_key)
        contexts.append(context)
        try:
            result = original_pvs(
                board,
                depth,
                alpha,
                beta,
                lmr_depth,
                ply,
                tt_context=tt_context,
            )
        finally:
            contexts.pop()
        cutoff_category = loop_cutoff_category(
            board_at_entry,
            result,
            beta,
            tt_move,
            killers,
            context["selected"],
        )
        if cutoff_category is not None:
            coverage[cutoff_category]["cutoffs"] += 1
        return result

    try:
        engine.handle_setoption(["name", "Hash", "value", "16"])
        engine.handle_setoption(["name", "Book", "value", "false"])
        warm = engine._search_worker.submit(chess.STARTING_FEN, None, 2, False)
        if not warm.completed.wait(110) or engine._search_worker.failure:
            raise RuntimeError("warmup failed")
        engine.handle_ucinewgame()
        output.clear()
        if payload["mode"] == "coverage":
            engine.pvs_search = covered_pvs
        task = engine._search_worker.submit(
            payload["fen"],
            None,
            5,
            False,
            moves=tuple(payload.get("moves", [])),
        )
        if not isinstance(task, task_type):
            raise RuntimeError("unexpected search task type")
        if not task.completed.wait(110) or engine._search_worker.failure:
            raise RuntimeError(f"search failed: {engine._search_worker.failure!r}")
        completed_depth = max(
            [
                int(line.split()[2])
                for line in output
                if line.startswith("info depth ")
            ]
            or [0]
        )
        semantic = {
            "bestmove": measurement["bestmove"],
            "score": measurement["score"],
            "completed_depth": completed_depth,
            "info": [
                " ".join(line.split()[:6] + line.split()[8:])
                for line in output
                if line.startswith("info depth ")
            ],
        }
        return {
            **measurement,
            "semantic": semantic,
            "identity": identity,
            "options": {
                "hash_bytes": engine.tt_size_bytes,
                "book": engine.use_opening_book,
            },
            "coverage": coverage if payload["mode"] == "coverage" else None,
            "pid": os.getpid(),
        }
    finally:
        engine._shutdown()


def attach_exit_code(
    mode: str,
    result: dict[str, Any],
    exit_code: int,
) -> dict[str, Any]:
    if mode == "identity":
        return result
    return {**result, "worker_exit_code": exit_code}


def invoke(
    root: Path,
    payload: dict[str, Any],
    timeout: float = 120.0,
) -> dict[str, Any]:
    frozen_identity = payload.get("identity")
    packages = (
        frozen_identity.get("packages")
        if isinstance(frozen_identity, dict)
        else {"chess": RUNTIME_POLICY.imported_package_identity("chess")}
    )
    if not isinstance(packages, dict):
        raise RuntimeError("worker package identity is missing")
    environment = RUNTIME_POLICY.runtime_environment(
        root,
        import_paths=RUNTIME_POLICY.package_import_paths(packages),
    )
    command = RUNTIME_POLICY.controlled_python_command(
        Path(__file__),
        "worker",
        json.dumps({"root": str(root), **payload}),
    )
    result = subprocess.run(
        command,
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"worker exit {result.returncode}: {result.stdout}\n{result.stderr}"
        )
    if result.stderr:
        raise RuntimeError(f"worker stderr: {result.stderr}")
    loaded = json.loads(result.stdout)
    if not isinstance(loaded, dict):
        raise RuntimeError("worker output is not an object")
    return attach_exit_code(str(payload["mode"]), loaded, result.returncode)


def process_tool_identity() -> dict[str, str]:
    if not PS_PATH.is_absolute() or not PS_PATH.is_file():
        raise RuntimeError("process tool identity is unavailable")
    path = PS_PATH.resolve()
    return {"path": str(path), "sha256": sha(path)}


def verify_process_tool_identity(frozen: dict[str, str]) -> None:
    if process_tool_identity() != frozen:
        raise RuntimeError("process tool identity changed")


def process_snapshot() -> str:
    tool = process_tool_identity()
    return subprocess.check_output(
        [tool["path"], "-eo", "pid,ppid,psr,comm,args"],
        text=True,
        env=RUNTIME_POLICY.validated_environment(),
    )


def starts_stockconda_module(command: str, args: str) -> bool:
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    executable = Path(tokens[0]).name.lower() if tokens else command.lower()
    if not executable.startswith("python"):
        return False
    return any(
        token == "-m" and tokens[index + 1] == "chess_engine.stockconda"
        for index, token in enumerate(tokens[:-1])
    )


def conflicting_processes(snapshot: str) -> list[dict[str, Any]]:
    patterns = (
        "fastchess",
        "stockconda.py",
        "pytest",
        "cprofile",
        "py-spy",
        "perf record",
        "setup.py build_ext",
    )
    conflicts: list[dict[str, Any]] = []
    for line in snapshot.splitlines()[1:]:
        fields = line.strip().split(maxsplit=4)
        if len(fields) != 5:
            continue
        lowered = line.lower()
        if not any(pattern in lowered for pattern in patterns) and not (
            starts_stockconda_module(fields[3], fields[4])
        ):
            continue
        conflicts.append(
            {
                "pid": int(fields[0]),
                "ppid": int(fields[1]),
                "processor": int(fields[2]),
                "command": fields[3],
                "args": fields[4],
            }
        )
    return conflicts


def validate_gate2_handoff(
    manifest: dict[str, Any],
    parity_result: dict[str, Any],
    counter_result: dict[str, Any],
    manifest_sha256: str,
    positions: list[dict[str, Any]],
) -> None:
    runner_sha256 = manifest.get("runner_sha256")
    try:
        sources = manifest["engines"]["candidate"]["source"]
        frozen_runner = sources["scripts/ap04b_gate2.py"]
        frozen_policy = sources["scripts/ap04b_runtime_policy.py"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Gate-2 manifest runner identity mismatch") from exc
    if runner_sha256 != frozen_runner:
        raise RuntimeError("Gate-2 manifest runner identity mismatch")
    if manifest.get("runtime_policy_sha256") != frozen_policy:
        raise RuntimeError("Gate-2 manifest runtime policy identity mismatch")
    if (
        parity_result.get("status") != "PASS"
        or parity_result.get("comparisons") != 100
        or parity_result.get("manifest_sha256") != manifest_sha256
        or parity_result.get("runner_sha256") != runner_sha256
    ):
        raise RuntimeError("Gate-2 parity runner or evidence mismatch")
    if (
        counter_result.get("status") != "PASS"
        or counter_result.get("manifest_sha256") != manifest_sha256
        or counter_result.get("runner_sha256") != runner_sha256
    ):
        raise RuntimeError("Gate-2 counter is not PASS and identity-bound")
    expected_counter = {
        "tt": {"captures": 0, "killers": 0, "other": 0},
        "capture": {"captures": 1, "killers": 0, "other": 0},
        "killer": {"captures": 1, "killers": 1, "other": 0},
    }
    if canonical(counter_result.get("results")) != canonical(expected_counter):
        raise RuntimeError("Gate-2 counter scenarios mismatch")
    if canonical(manifest.get("depths")) != canonical([1, 2, 3, 4, 5]):
        raise RuntimeError("Gate-2 depths mismatch")
    engines = manifest.get("engines")
    if not isinstance(engines, dict):
        raise RuntimeError("Gate-2 engine identities are missing")
    baseline = engines.get("baseline")
    if (
        not isinstance(baseline, dict)
        or baseline.get("archive_sha256") != BASELINE_ARCHIVE_SHA256
    ):
        raise RuntimeError("Gate-2 baseline archive identity mismatch")
    candidate = engines.get("candidate")
    candidate_sha = (
        candidate.get("archive_sha256")
        if isinstance(candidate, dict)
        else None
    )
    if (
        not isinstance(candidate_sha, str)
        or len(candidate_sha) != 64
        or any(character not in "0123456789abcdef" for character in candidate_sha)
    ):
        raise RuntimeError("Gate-2 candidate archive identity mismatch")
    gate2_positions = manifest.get("positions")
    if (
        not isinstance(gate2_positions, list)
        or len(gate2_positions) != 20
        or canonical(gate2_positions[:FIXTURES]) != canonical(positions)
    ):
        raise RuntimeError("Gate-2 fixture prefix mismatch")


def host_state() -> dict[str, Any]:
    process_tool = process_tool_identity()
    snapshot = process_snapshot()
    load = os.getloadavg()
    return {
        "time_ns": time.time_ns(),
        "loadavg": load,
        "logical_cpu_count": os.cpu_count(),
        "affinity": sorted(os.sched_getaffinity(0)),
        "proc_stat": Path("/proc/stat").read_text(encoding="utf-8"),
        "processes": snapshot,
        "process_tool": process_tool,
        "conflicts": conflicting_processes(snapshot),
    }


def host_snapshot_index(output: Path) -> list[dict[str, str]]:
    expected = {
        f"host-block-{block:02d}-{phase}.json"
        for block in range(BLOCKS)
        for phase in ("before", "after")
    }
    paths = sorted(output.glob("host-block-*.json"))
    if {path.name for path in paths} != expected:
        raise RuntimeError("host snapshot set is incomplete")
    return [{"name": path.name, "sha256": sha(path)} for path in paths]


def validate_tool_binding(manifest: dict[str, Any]) -> None:
    try:
        sources = manifest["engines"]["candidate"]["source"]
        frozen_runner = sources["scripts/ap04b_ordering_benchmark.py"]
        frozen_auditor = sources["scripts/ap04b_gate3_audit.py"]
        frozen_match_runner = sources["scripts/ap04b_match_pipeline.py"]
        frozen_policy = sources["scripts/ap04b_runtime_policy.py"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Gate-3 tool identity is missing") from exc
    if manifest.get("runner_sha256") != frozen_runner:
        raise RuntimeError("Gate-3 runner is not the frozen candidate source")
    if manifest.get("auditor_sha256") != frozen_auditor:
        raise RuntimeError("Gate-3 auditor is not the frozen candidate source")
    if manifest.get("match_runner_sha256") != frozen_match_runner:
        raise RuntimeError("Gate-3 match runner is not the frozen candidate source")
    if manifest.get("runtime_policy_sha256") != frozen_policy:
        raise RuntimeError("Gate-3 runtime policy is not the frozen candidate source")


def freeze(args: argparse.Namespace) -> None:
    auditor_runtime = RUNTIME_POLICY.runtime_identity()
    RUNTIME_POLICY.validate_runtime_identity(auditor_runtime)
    new_output(args.output)
    validate_host_capabilities(os.cpu_count(), os.sched_getaffinity(0))
    validate_distinct_roots(args.baseline, args.candidate)
    fixtures = json.loads(args.fixtures.read_text(encoding="utf-8"))
    positions = fixtures.get("positions")
    if not isinstance(positions, list) or len(positions) != FIXTURES:
        raise RuntimeError("frozen Gate-3 fixture file must contain 12 positions")
    gate2_manifest = json.loads(args.gate2_manifest.read_text(encoding="utf-8"))
    gate2_result = json.loads(args.gate2_result.read_text(encoding="utf-8"))
    gate2_counter = json.loads(args.gate2_counter.read_text(encoding="utf-8"))
    validate_gate2_handoff(
        gate2_manifest,
        gate2_result,
        gate2_counter,
        sha(args.gate2_manifest),
        positions,
    )
    fixture_copy = args.output / "fixtures.json"
    fixture_copy.write_bytes(args.fixtures.read_bytes())
    gate2_manifest_copy = args.output / "gate2-manifest.json"
    gate2_manifest_copy.write_bytes(args.gate2_manifest.read_bytes())
    gate2_result_copy = args.output / "gate2-result.json"
    gate2_result_copy.write_bytes(args.gate2_result.read_bytes())
    gate2_counter_copy = args.output / "gate2-counter.json"
    gate2_counter_copy.write_bytes(args.gate2_counter.read_bytes())
    manifest: dict[str, Any] = {
        "runner_sha256": sha(Path(__file__)),
        "auditor_sha256": sha(Path(__file__).with_name("ap04b_gate3_audit.py")),
        "runtime_policy_sha256": sha(
            Path(__file__).with_name("ap04b_runtime_policy.py")
        ),
        "auditor_runtime": auditor_runtime,
        "process_tool": process_tool_identity(),
        "fixture_copy": str(fixture_copy),
        "fixture_sha256": sha(fixture_copy),
        "positions": positions,
        "positions_sha256": hashlib.sha256(canonical(positions)).hexdigest(),
        "gate2": {
            "manifest_copy": str(gate2_manifest_copy),
            "manifest_sha256": sha(gate2_manifest_copy),
            "result_copy": str(gate2_result_copy),
            "result_sha256": sha(gate2_result_copy),
            "counter_copy": str(gate2_counter_copy),
            "counter_sha256": sha(gate2_counter_copy),
            "source_manifest_sha256": gate2_result["manifest_sha256"],
            "runner_sha256": gate2_result["runner_sha256"],
            "baseline_archive_sha256": gate2_manifest["engines"]["baseline"][
                "archive_sha256"
            ],
            "candidate_archive_sha256": gate2_manifest["engines"]["candidate"][
                "archive_sha256"
            ],
        },
        "protocol": {
            "blocks": BLOCKS,
            "fixtures": FIXTURES,
            "repetitions": REPETITIONS,
            "samples": 1200,
            "depth": 5,
            "hash_mb": 16,
            "book": False,
            "warmup": "startpos depth 2 then ucinewgame; excluded",
            "engine_cpu": 0,
            "harness_cpu": 1,
            "process_timeout_seconds": 120,
            "phase_timeout_seconds": 3600,
            "load1_limit": 5.5,
            "even_order": ["baseline", "candidate"],
            "odd_order": ["candidate", "baseline"],
        },
        "engines": {},
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "compiler": subprocess.check_output(
                ["g++", "--version"],
                text=True,
                env=RUNTIME_POLICY.validated_environment(),
            ),
            "packages": subprocess.check_output(
                [sys.executable, "-m", "pip", "freeze"],
                text=True,
                env=RUNTIME_POLICY.validated_environment(),
            ),
        },
        "host_at_freeze": host_state(),
    }
    engine_inputs = {
        "baseline": (
            args.baseline,
            args.baseline_archive,
            BASELINE_ARCHIVE_SHA256,
        ),
        "candidate": (
            args.candidate,
            args.candidate_archive,
            gate2_manifest["engines"]["candidate"]["archive_sha256"],
        ),
    }
    for label, (root, archive, expected_archive_sha) in engine_inputs.items():
        sources = verify_archive(root, archive, expected_archive_sha)
        manifest["engines"][label] = {
            "root": str(root.resolve()),
            "archive": str(archive.resolve()),
            "archive_sha256": expected_archive_sha,
            "source": sources,
            "executables": RUNTIME_POLICY.inventory_executables(root),
            "identity": invoke(root, {"mode": "identity"}),
            "commit": BASELINE_COMMIT if label == "baseline" else None,
        }
    for label, expected in (
        ("baseline", BASELINE_ARCHIVE_SHA256),
        (
            "candidate",
            gate2_manifest["engines"]["candidate"]["archive_sha256"],
        ),
    ):
        if manifest["gate2"][f"{label}_archive_sha256"] != expected:
            raise RuntimeError(f"Gate-2 {label} archive identity mismatch")
    manifest["match_runner_sha256"] = manifest["engines"]["candidate"][
        "source"
    ]["scripts/ap04b_match_pipeline.py"]
    validate_tool_binding(manifest)
    write_json(args.output / "manifest.json", manifest)


def validate_manifest(manifest: dict[str, Any]) -> None:
    validate_tool_binding(manifest)
    try:
        RUNTIME_POLICY.verify_runtime_identity(manifest["auditor_runtime"])
    except (KeyError, TypeError, RuntimeError) as exc:
        raise RuntimeError("frozen auditor interpreter changed") from exc
    try:
        frozen_process_tool = manifest["process_tool"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("process tool identity is missing") from exc
    if not isinstance(frozen_process_tool, dict):
        raise RuntimeError("process tool identity is missing")
    verify_process_tool_identity(frozen_process_tool)
    if sha(Path(__file__)) != manifest["runner_sha256"]:
        raise RuntimeError("runner changed after freeze")
    if sha(Path(__file__).with_name("ap04b_runtime_policy.py")) != manifest[
        "runtime_policy_sha256"
    ]:
        raise RuntimeError("runtime policy changed after freeze")
    validate_evidence(
        Path(manifest["fixture_copy"]), manifest["fixture_sha256"]
    )
    validate_evidence(
        Path(manifest["gate2"]["manifest_copy"]),
        manifest["gate2"]["manifest_sha256"],
    )
    validate_evidence(
        Path(manifest["gate2"]["result_copy"]),
        manifest["gate2"]["result_sha256"],
    )
    validate_evidence(
        Path(manifest["gate2"]["counter_copy"]),
        manifest["gate2"]["counter_sha256"],
    )
    gate2_manifest = json.loads(
        Path(manifest["gate2"]["manifest_copy"]).read_text(encoding="utf-8")
    )
    gate2_result = json.loads(
        Path(manifest["gate2"]["result_copy"]).read_text(encoding="utf-8")
    )
    gate2_counter = json.loads(
        Path(manifest["gate2"]["counter_copy"]).read_text(encoding="utf-8")
    )
    validate_gate2_handoff(
        gate2_manifest,
        gate2_result,
        gate2_counter,
        manifest["gate2"]["manifest_sha256"],
        manifest["positions"],
    )
    roots = [Path(manifest["engines"][label]["root"]) for label in ENGINES]
    validate_distinct_roots(roots[0], roots[1])
    for label in ENGINES:
        entry = manifest["engines"][label]
        root = Path(entry["root"])
        archive = Path(entry["archive"])
        current_sources = verify_archive(
            root, archive, entry["archive_sha256"]
        )
        if current_sources != entry["source"]:
            raise RuntimeError(f"{label} source manifest changed")
        RUNTIME_POLICY.verify_executable_root(root, entry["executables"])
        if invoke(root, {"mode": "identity"}) != entry["identity"]:
            raise RuntimeError(f"{label} imported identity changed")


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    host_availability = validate_and_pin_harness()
    new_output(args.output)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report: dict[str, Any] = {
        "status": "RUNNING",
        "manifest_sha256": sha(args.manifest),
        "runner_sha256": sha(Path(__file__)),
        "host_availability": host_availability,
        "expected": {},
        "coverage": {
            category: {"selected": 0, "cutoffs": 0}
            for category in CATEGORIES
        },
    }
    write_json(args.output / "start.json", {**report, "host": host_state()})
    try:
        validate_manifest(manifest)
        for fixture_index, fixture in enumerate(manifest["positions"]):
            pair: dict[str, Any] = {}
            for label in ENGINES:
                entry = manifest["engines"][label]
                result = invoke(
                    Path(entry["root"]),
                    {
                        "mode": "semantic",
                        **fixture,
                        "identity": entry["identity"],
                    },
                )
                write_json(
                    args.output / f"semantic-{fixture_index:02d}-{label}.json",
                    result,
                )
                pair[label] = result["semantic"]
            compare_semantics(pair["baseline"], pair["candidate"])
            report["expected"][str(fixture_index)] = pair["baseline"]
            baseline = manifest["engines"]["baseline"]
            coverage_result = invoke(
                Path(baseline["root"]),
                {
                    "mode": "coverage",
                    **fixture,
                    "identity": baseline["identity"],
                },
            )
            write_json(
                args.output / f"coverage-{fixture_index:02d}.json",
                coverage_result,
            )
            fixture_coverage = coverage_result["coverage"]
            for category in CATEGORIES:
                for field in ("selected", "cutoffs"):
                    report["coverage"][category][field] += fixture_coverage[
                        category
                    ][field]
            print(
                f"preflight {fixture_index + 1}/{FIXTURES} "
                f"{fixture['label']} PASS",
                flush=True,
            )
        validate_coverage(report["coverage"])
        validate_manifest(manifest)
        report["status"] = "PASS"
    except BaseException as exc:
        report.update(status="FAIL", error=repr(exc))
    report["host_after"] = host_state()
    write_json(args.output / "result.json", report)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "expected"},
            sort_keys=True,
        ),
        flush=True,
    )
    return report


def execute_run(args: argparse.Namespace) -> dict[str, Any]:
    host_availability = validate_and_pin_harness()
    new_output(args.output)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    preflight_result = json.loads(args.preflight.read_text(encoding="utf-8"))
    report: dict[str, Any] = {
        "status": "RUNNING",
        "manifest_sha256": sha(args.manifest),
        "preflight_sha256": sha(args.preflight),
        "runner_sha256": sha(Path(__file__)),
        "host_availability": host_availability,
        "samples": 0,
        "harness_affinity": sorted(os.sched_getaffinity(0)),
    }
    write_json(args.output / "start.json", {**report, "host": host_state()})
    raw_path = args.output / "raw.jsonl"
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        validate_manifest(manifest)
        validate_preflight(
            preflight_result,
            sha(args.manifest),
            manifest["runner_sha256"],
        )
        validate_same_host_availability(preflight_result, host_availability)
        plan = measurement_plan()
        with raw_path.open("x", encoding="utf-8") as raw:
            for block in range(BLOCKS):
                before = host_state()
                write_json(args.output / f"host-block-{block:02d}-before.json", before)
                gate = host_gate(before["loadavg"][0], before["conflicts"])
                if gate["status"] != "PASS":
                    report.update(
                        status="HOLD",
                        reason=gate["reason"],
                        stopped_before_block=block,
                    )
                    break
                block_items = [item for item in plan if item["block"] == block]
                for item in block_items:
                    remaining = 3600 - (time.monotonic() - started)
                    if remaining <= 0:
                        raise TimeoutError("measurement phase exceeds 3600 seconds")
                    fixture = manifest["positions"][item["fixture"]]
                    entry = manifest["engines"][item["engine"]]
                    worker_started_ns = time.time_ns()
                    result = invoke(
                        Path(entry["root"]),
                        {
                            "mode": "measure",
                            **fixture,
                            "identity": entry["identity"],
                        },
                        min(120.0, remaining),
                    )
                    validate_worker_result(
                        result,
                        entry["identity"],
                        preflight_result["expected"][str(item["fixture"])],
                    )
                    row = {
                        **item,
                        "label": fixture["label"],
                        "elapsed_ns": result["elapsed_ns"],
                        "semantic": result["semantic"],
                        "identity_sha256": hashlib.sha256(
                            canonical(result["identity"])
                        ).hexdigest(),
                        "options": result["options"],
                        "worker_affinity": result["worker_affinity"],
                        "worker_exit_code": result["worker_exit_code"],
                        "pid": result["pid"],
                        "started_ns": worker_started_ns,
                        "finished_ns": time.time_ns(),
                    }
                    raw.write(json.dumps(row, sort_keys=True) + "\n")
                    raw.flush()
                    os.fsync(raw.fileno())
                    rows.append(row)
                    report["samples"] = len(rows)
                after = host_state()
                write_json(args.output / f"host-block-{block:02d}-after.json", after)
                gate = host_gate(after["loadavg"][0], after["conflicts"])
                if gate["status"] != "PASS":
                    report.update(
                        status="HOLD",
                        reason=gate["reason"],
                        stopped_after_block=block,
                    )
                    break
                print(
                    f"block {block + 1}/{BLOCKS} complete; "
                    f"{len(rows)}/1200 independent processes",
                    flush=True,
                )
            if report["status"] == "RUNNING":
                validate_manifest(manifest)
                validate_preflight(
                    preflight_result,
                    sha(args.manifest),
                    manifest["runner_sha256"],
                )
                aggregate, fixture_ratios = ratios(rows)
                report.update(
                    decision(aggregate, fixture_ratios),
                    samples=len(rows),
                    phase_seconds=time.monotonic() - started,
                )
    except BaseException as exc:
        report.update(status="FAIL", error=repr(exc), samples=len(rows))
    if raw_path.exists():
        report["raw_sha256"] = sha(raw_path)
    if report.get("status") == "PASS":
        report["host_snapshot_index"] = host_snapshot_index(args.output)
    report["host_after"] = host_state()
    write_json(args.output / "result.json", report)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "host_after"},
            sort_keys=True,
        ),
        flush=True,
    )
    return report


def main() -> int:
    RUNTIME_POLICY.require_controlled_startup()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    worker_parser = commands.add_parser("worker")
    worker_parser.add_argument("payload")
    freeze_parser = commands.add_parser("freeze")
    freeze_parser.add_argument("--fixtures", type=Path, required=True)
    freeze_parser.add_argument("--baseline", type=Path, required=True)
    freeze_parser.add_argument("--baseline-archive", type=Path, required=True)
    freeze_parser.add_argument("--candidate", type=Path, required=True)
    freeze_parser.add_argument("--candidate-archive", type=Path, required=True)
    freeze_parser.add_argument("--gate2-manifest", type=Path, required=True)
    freeze_parser.add_argument("--gate2-result", type=Path, required=True)
    freeze_parser.add_argument("--gate2-counter", type=Path, required=True)
    freeze_parser.add_argument("--output", type=Path, required=True)
    for name in ("preflight", "run"):
        child = commands.add_parser(name)
        child.add_argument("--manifest", type=Path, required=True)
        child.add_argument("--output", type=Path, required=True)
        if name == "run":
            child.add_argument("--preflight", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "worker":
        print(json.dumps(worker(json.loads(args.payload))))
        return 0
    if args.command == "freeze":
        freeze(args)
        return 0
    if args.command == "preflight":
        return 0 if preflight(args)["status"] == "PASS" else 3
    return 0 if execute_run(args)["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())

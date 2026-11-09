"""AP04 Revision 2: isolated semantic parity and the frozen unprofiled gate.

Run inside the build container. Archives/build logs remain external; never
overlay engine source. ``freeze``, ``parity`` and ``run`` each require a new
output directory. Every timed search runs in a new subprocess on CPU 0.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tarfile
import time
from typing import Any

BASELINE = "106f50016943e46a4c565f6c545bbd14d7d48b7f"
CANDIDATE = "741b05cb030e7384e8de931fc0b5488a11ed4764"
ENGINES = ("baseline", "candidate")
FROZEN_FENS = "e73792a4c70e7a9fb35a1efc231012cd3aa75a96f97712975b9624591f48c69b"
FROZEN_IDENTITY = "117531b161fd4810334498cd8b75c76d82b241dff9a3e02291f1baaf2886e01c"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def history_state(table: Any) -> Any:
    """Normalize old and color-plane history layouts for semantic parity."""
    plane = getattr(table, "plane", None)
    if not callable(plane):
        return table
    planes = (plane(0), plane(1))
    return [[[planes[turn][source][target] for turn in range(2)]
             for target in range(64)] for source in range(64)]


def write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def new_output(path: Path) -> None:
    path.mkdir(exist_ok=False)


def relative_mad(values: list[float]) -> float:
    median = statistics.median(values)
    return statistics.median([abs(value - median) for value in values]) / median


def decision(aggregate: list[float], fixtures: list[list[float]], *,
             valid: bool = True) -> dict[str, Any]:
    if (not valid or len(aggregate) != 10 or len(fixtures) != 12
            or any(len(series) != 10 for series in fixtures)
            or any(not math.isfinite(x) or x <= 0 for series in [aggregate, *fixtures]
                   for x in series)):
        return {"status": "FAIL", "reason": "technical, identity, parity or incomplete sample"}
    median = statistics.median(aggregate)
    medians = [statistics.median(series) for series in fixtures]
    dispersion = [relative_mad(series) for series in [aggregate, *fixtures]]
    if max(dispersion) > .03:
        status, reason = "HOLD", "relative MAD exceeds 3%"
    elif median > 1.00 or max(medians) > 1.03:
        status, reason = "FAIL", "stable regression exceeds frozen threshold"
    elif median <= .97:
        status, reason = "PASS", "stable aggregate gain at least 3%, fixture bounds satisfied"
    else:
        status, reason = "HOLD", "stable aggregate gain below 3%"
    return {"status": status, "reason": reason, "aggregate_median": median,
            "fixture_medians": medians, "relative_mad": dispersion,
            "aggregate_ratios": aggregate, "fixture_ratios": fixtures}


def ratios(rows: list[dict[str, Any]]) -> tuple[list[float], list[list[float]]]:
    expected = {(b, j, e, r) for b in range(10) for j in range(12)
                for e in ENGINES for r in range(5)}
    samples: dict[tuple[int, int, str, int], int] = {}
    for row in rows:
        key = (row["block"], row["fixture"], row["engine"], row["repetition"])
        duration = row["elapsed_ns"]
        if key in samples or key not in expected or not isinstance(duration, int) or duration <= 0:
            raise ValueError("duplicate, unexpected or invalid measurement")
        samples[key] = duration
    if set(samples) != expected:
        raise ValueError("incomplete measurement grid")
    totals = {(b, j, e): sum(samples[b, j, e, r] for r in range(5))
              for b in range(10) for j in range(12) for e in ENGINES}
    aggregate = [sum(totals[b, j, "candidate"] for j in range(12)) /
                 sum(totals[b, j, "baseline"] for j in range(12)) for b in range(10)]
    fixtures = [[totals[b, j, "candidate"] / totals[b, j, "baseline"]
                 for b in range(10)] for j in range(12)]
    return aggregate, fixtures


def host_state() -> dict[str, Any]:
    return {"time_ns": time.time_ns(), "loadavg": os.getloadavg(),
            "proc_stat": Path("/proc/stat").read_text(),
            "processes": subprocess.check_output(["ps", "-eo", "pid,comm,args"], text=True)}


def imported_identity(root: Path) -> dict[str, Any]:
    modules = ["chess_engine.stockconda", "chess_engine.uci_search_worker",
               "chess_engine.cython_ext.cython_chess", "chess_engine.cython_ext.evaluation",
               "chess_engine.cython_ext.transposition"]
    imported: dict[str, Any] = {}
    for name in modules:
        module = importlib.import_module(name)
        assert module.__file__ is not None
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(root / "src"):
            raise RuntimeError(f"unbound import: {name}: {path}")
        if ".cython_ext." in name and path.suffix != ".so":
            raise RuntimeError(f"native extension missing: {path}")
        imported[name] = {"path": str(path), "sha256": sha(path)}
    return {"imported": imported, "python": sys.version, "executable": sys.executable,
            "platform": platform.platform(), "affinity": sorted(os.sched_getaffinity(0)),
            "chess": importlib.import_module("chess").__version__}


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

    def capture(*args: Any, **kwargs: Any) -> None:
        output.append(" ".join(map(str, args)))

    engine._uci_print = capture
    measurement: dict[str, Any] = {}
    task_type = importlib.import_module("chess_engine.uci_search_worker").SearchTask

    def execute(task: Any) -> None:
        engine._active_cancel = task.cancelled
        start = time.perf_counter_ns()
        move, score = engine.execute_search_task(None, task.depth_limit, False,
                                                 task.fen, task.moves)
        elapsed = time.perf_counter_ns() - start
        engine._active_cancel = None
        measurement.update(elapsed_ns=elapsed, bestmove=move.uci() if move else None, score=score,
                           worker_affinity=sorted(os.sched_getaffinity(0)))

    engine._search_worker._execute = execute
    try:
        engine.handle_setoption(["name", "Hash", "value", "16"])
        engine.handle_setoption(["name", "Book", "value", "false"])
        warm = engine._search_worker.submit(chess.STARTING_FEN, None, 2, False)
        if not warm.completed.wait(110) or engine._search_worker.failure:
            raise RuntimeError("warmup failed")
        engine.handle_ucinewgame()
        output.clear()
        trace: list[Any] = []
        counts = {"pvs": 0, "qsearch": 0, "stores": 0}
        original_pvs = engine.pvs_search
        original_qsearch = engine.quiescence_search
        original_store = engine_module.store_tt_entry

        def pvs(board: Any, *args: Any, **kwargs: Any) -> Any:
            counts["pvs"] += 1
            trace.append(["pvs", board.fen(en_passant="fen"), args, kwargs,
                          [move.uci() for move in board.move_stack]])
            if payload.get("cancel_at") == counts["pvs"]:
                engine._active_cancel.set()
            result = original_pvs(board, *args, **kwargs)
            trace.append(["pvs_return", result[0], result[1].uci() if result[1] else None])
            return result

        def qsearch(board: Any, *args: Any, **kwargs: Any) -> Any:
            counts["qsearch"] += 1
            trace.append(["qsearch", board.fen(en_passant="fen"), args, kwargs])
            result = original_qsearch(board, *args, **kwargs)
            trace.append(["qsearch_return", result])
            return result

        def store(board: Any, depth: int, value: int, kind: str, move: Any,
                  **kwargs: Any) -> Any:
            counts["stores"] += 1
            trace.append(["store", board.fen(en_passant="fen"), depth, value, kind,
                          move.uci() if move else None, kwargs])
            return original_store(board, depth, value, kind, move, **kwargs)

        if payload["mode"] == "parity":
            engine.pvs_search = pvs
            engine.quiescence_search = qsearch
            engine_module.store_tt_entry = store
        # Submit uses the production serialized worker. The callback measures
        # only execute_search_task, excluding queueing, startup and warmup.
        task = engine._search_worker.submit(payload["fen"], None, payload.get("depth", 5), False,
                                             moves=tuple(payload.get("moves", [])))
        assert isinstance(task, task_type)
        if not task.completed.wait(110) or engine._search_worker.failure:
            raise RuntimeError(f"search failed: {engine._search_worker.failure!r}")
        semantic = {"bestmove": measurement["bestmove"], "score": measurement["score"],
                    "completed_depth": max([int(line.split()[2]) for line in output
                                            if line.startswith("info depth ")] or [0]),
                    "info": [" ".join(line.split()[:6] + line.split()[8:]) for line in output
                             if line.startswith("info depth ")]}
        if payload["mode"] == "parity":
            semantic.update(counts=counts, trace_sha256=hashlib.sha256(canonical(trace)).hexdigest(),
                            history=history_state(engine.history_table),
                            killers=[[move.uci() if move else None for move in row]
                                     for row in engine.killer_moves],
                            board=engine.board.fen(), stack=[move.uci() for move in engine.board.move_stack])
            write_json(Path(payload["trace_output"]), trace)
        return {**measurement, "semantic": semantic, "identity": identity, "pid": os.getpid(),
                "options": {"hash_bytes": engine.tt_size_bytes, "book": engine.use_opening_book}}
    finally:
        engine._shutdown()


def invoke(root: Path, payload: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
    environment = dict(os.environ, PYTHONPATH=str(root / "src"),
                       STOCKCONDA_TEST_SOURCE=str(root / "src"), PYTHONHASHSEED="0")
    command = [sys.executable, str(Path(__file__).resolve()), "worker", json.dumps({"root": str(root), **payload})]
    result = subprocess.run(command, cwd=root, env=environment, text=True,
                            capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"worker exit {result.returncode}: {result.stdout}\n{result.stderr}")
    if result.stderr:
        raise RuntimeError(f"worker stderr: {result.stderr}")
    return dict(json.loads(result.stdout))


def validate_manifest(manifest: dict[str, Any]) -> None:
    if sha(Path(__file__)) != manifest["runner_sha256"]:
        raise RuntimeError("runner changed after freeze")
    if sha(Path(manifest["fixture_copy"])) != manifest["fixture_identity_sha256"]:
        raise RuntimeError("fixture identity changed after freeze")
    for label in ENGINES:
        entry = manifest["engines"][label]
        if sha(Path(entry["archive"])) != entry["archive_sha256"]:
            raise RuntimeError("archive changed")
        root = Path(entry["root"])
        for relative, digest in entry["source"].items():
            if sha(root / relative) != digest:
                raise RuntimeError(f"source changed: {root / relative}")
        if invoke(root, {"mode": "identity"}) != entry["identity"]:
            raise RuntimeError("imported identity changed")


def freeze(args: argparse.Namespace) -> None:
    new_output(args.output)
    if sha(args.fixtures) != FROZEN_IDENTITY:
        raise RuntimeError("frozen fixture file identity mismatch")
    frozen = json.loads(args.fixtures.read_text())
    positions = frozen["positions"]
    if frozen["frozen_fens_sha256"] != FROZEN_FENS or len(positions) != 12:
        raise RuntimeError("unexpected frozen fixture identity")
    # Historical identity's digest is retained verbatim; the complete original
    # input and a canonical label/FEN digest are additionally frozen here.
    fixture_copy = args.output / "fixtures-original.json"
    fixture_copy.write_bytes(args.fixtures.read_bytes())
    manifest: dict[str, Any] = {"runner_sha256": sha(Path(__file__)),
        "fixture_identity_sha256": sha(fixture_copy), "fixture_copy": str(fixture_copy),
        "positions": positions, "positions_sha256": hashlib.sha256(canonical(positions)).hexdigest(),
        "original_fens_sha256": FROZEN_FENS, "engines": {},
        "protocol": {"blocks": 10, "repetitions": 5, "depth": 5, "hash_mb": 16,
                     "book": False, "warmup": "startpos depth 2 then ucinewgame",
                     "engine_cpu": 0, "harness_cpu": 1, "process_timeout": 120,
                     "measurement_timeout": 3600, "odd": "baseline,candidate",
                     "even": "candidate,baseline"},
        "host_before": host_state(), "compiler": subprocess.check_output(["g++", "--version"], text=True),
        "runtime_packages": subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)}
    for label, commit in zip(ENGINES, (BASELINE, CANDIDATE)):
        root, archive = Path(getattr(args, label)).resolve(), Path(getattr(args, label + "_archive")).resolve()
        sources = {}
        with tarfile.open(archive) as tar:
            if (tar.pax_headers or {}).get("comment") != commit:
                raise RuntimeError("archive commit identity mismatch")
            for member in tar.getmembers():
                if member.isfile() and (Path(member.name).suffix in {".py", ".pyi", ".pyx", ".pxd", ".h"}
                                        or member.name.endswith("cython_chess_backend.cpp")):
                    stream = tar.extractfile(member)
                    assert stream is not None
                    expected = hashlib.sha256(stream.read()).hexdigest()
                    if sha(root / member.name) != expected:
                        raise RuntimeError(f"archive/source mismatch: {member.name}")
                    sources[member.name] = expected
        manifest["engines"][label] = {"root": str(root), "commit": commit, "archive": str(archive),
                                      "archive_sha256": sha(archive), "source": sources,
                                      "identity": invoke(root, {"mode": "identity"})}
    if manifest["engines"]["baseline"]["root"] == manifest["engines"]["candidate"]["root"]:
        raise RuntimeError("build roots must be independent")
    write_json(args.output / "manifest.json", manifest)


def execute_gate(args: argparse.Namespace) -> dict[str, Any]:
    new_output(args.output)
    os.sched_setaffinity(0, {1})
    manifest = json.loads(args.manifest.read_text())
    report: dict[str, Any] = {"status": "RUNNING", "manifest_sha256": sha(args.manifest),
                             "runner_sha256": sha(Path(__file__)), "mode": args.stage,
                             "harness_affinity": sorted(os.sched_getaffinity(0))}
    write_json(args.output / "start.json", {**report, "host": host_state()})
    rows: list[dict[str, Any]] = []
    try:
        validate_manifest(manifest)
        positions = manifest["positions"]
        if args.stage == "parity":
            positions = positions + [
                {"label": "checkmate", "fen": "7k/6Q1/5K2/8/8/8/8/8 b - - 0 1"},
                {"label": "stalemate", "fen": "7k/5Q2/5K2/8/8/8/8/8 b - - 0 1"},
                {"label": "tied-captures", "fen": "4k3/8/8/8/2p1p3/3Q4/8/4K3 w - - 0 1"},
                {"label": "castling", "fen": "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"},
                {"label": "en-passant", "fen": "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1"},
                {"label": "promotion", "fen": "4k2r/6P1/8/8/8/8/8/4K3 w - - 0 1"},
                {"label": "repetition-history", "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                 "moves": ["g1f3", "g8f6", "f3g1", "f6g8"]},
                {**positions[0], "label": "cancellation", "cancel_at": 100},
            ]
            write_json(args.output / "replay-fixtures.json", positions)
            expected = {}
            for j, fixture in enumerate(positions):
                pair = {}
                for label in ENGINES:
                    entry = manifest["engines"][label]
                    result = invoke(Path(entry["root"]), {"mode": "parity", **fixture,
                                    "identity": entry["identity"], "trace_output": str(args.output / f"trace-{j}-{label}.json")})
                    write_json(args.output / f"result-{j}-{label}.json", result)
                    pair[label] = result["semantic"]
                if pair["baseline"] != pair["candidate"]:
                    raise RuntimeError(f"semantic parity mismatch: {fixture['label']}")
                expected[str(j)] = {key: pair["candidate"][key]
                                    for key in ("score", "bestmove", "completed_depth", "info")}
                print(f"parity {j + 1}/{len(positions)} {fixture['label']} PASS", flush=True)
            report.update(status="PASS", fixtures=len(positions), expected=expected)
        else:
            parity = json.loads(args.parity.read_text())
            if parity["status"] != "PASS" or parity["manifest_sha256"] != sha(args.manifest):
                raise RuntimeError("parity is not PASS for this manifest")
            report["parity_sha256"] = sha(args.parity)
            started = time.monotonic()
            with (args.output / "raw.jsonl").open("x") as raw:
                for block in range(10):
                    write_json(args.output / f"host-block-{block}.json", host_state())
                    order = ENGINES if block % 2 == 0 else tuple(reversed(ENGINES))
                    for j, fixture in enumerate(positions):
                        for label in order:
                            entry = manifest["engines"][label]
                            for repetition in range(5):
                                remaining = 3600 - (time.monotonic() - started)
                                if remaining <= 0:
                                    raise TimeoutError("measurement phase exceeds 3600 seconds")
                                result = invoke(Path(entry["root"]), {"mode": "measure", **fixture,
                                                "identity": entry["identity"]}, min(120, remaining))
                                row = {"block": block, "fixture": j, "label": fixture["label"],
                                       "engine": label, "repetition": repetition, **result}
                                raw.write(json.dumps(row) + "\n")
                                raw.flush()
                                rows.append(row)
                                if result["semantic"] != parity["expected"][str(j)]:
                                    raise RuntimeError(f"timed result mismatch: {block}/{j}/{label}/{repetition}")
                                if result["worker_affinity"] != [0] or result["options"] != {"hash_bytes": 16777216, "book": False}:
                                    raise RuntimeError("worker options/affinity mismatch")
                    print(f"block {block + 1}/10 complete; {len(rows)} independent processes", flush=True)
            validate_manifest(manifest)
            aggregate, fixtures = ratios(rows)
            report.update(decision(aggregate, fixtures), samples=len(rows),
                          phase_seconds=time.monotonic() - started,
                          raw_sha256=sha(args.output / "raw.jsonl"))
    except BaseException as exc:
        report.update(status="FAIL", error=repr(exc), samples=len(rows))
    finally:
        report["host_after"] = host_state()
        write_json(args.output / "result.json", report)
    print(json.dumps({key: value for key, value in report.items()
                      if key not in {"host_after", "expected"}}), flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="stage", required=True)
    child = sub.add_parser("worker")
    child.add_argument("payload")
    setup = sub.add_parser("freeze")
    setup.add_argument("--fixtures", type=Path, required=True)
    for label in ENGINES:
        setup.add_argument("--" + label, type=Path, required=True)
        setup.add_argument("--" + label + "-archive", type=Path, required=True)
    setup.add_argument("--output", type=Path, required=True)
    for name in ("parity", "run"):
        stage = sub.add_parser(name)
        stage.add_argument("--manifest", type=Path, required=True)
        stage.add_argument("--output", type=Path, required=True)
        if name == "run":
            stage.add_argument("--parity", type=Path, required=True)
    args = parser.parse_args()
    if args.stage == "worker":
        print(json.dumps(worker(json.loads(args.payload))))
    elif args.stage == "freeze":
        freeze(args)
    else:
        return 0 if execute_gate(args)["status"] == "PASS" else 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

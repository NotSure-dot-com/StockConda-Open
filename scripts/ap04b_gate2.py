"""AP-04B Gate 2: identity-bound semantic replay and laziness checks.

This runner deliberately contains no timing, performance, match or Elo path.
Every command that writes evidence requires a previously unused directory.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
from importlib import util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
from types import ModuleType
from typing import Any, NoReturn


ENGINES = ("baseline", "candidate")
STAGES = ("captures", "killers", "other")


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
    """Create an evidence directory, refusing overwrite or resume."""
    path.mkdir(parents=True, exist_ok=False)


def compare_semantics(baseline: Any, candidate: Any) -> None:
    """Require recursive, type-sensitive equality of replay semantics."""
    if canonical(baseline) != canonical(candidate):
        raise ValueError("semantic parity mismatch")


def validate_laziness(scenario: str, counts: dict[str, int]) -> None:
    """Reject any stage work after the controlled cutoff stage."""
    expected = {
        "tt": {"captures": 0, "killers": 0, "other": 0},
        "capture": {"captures": 1, "killers": 0, "other": 0},
        "killer": {"captures": 1, "killers": 1, "other": 0},
    }
    if scenario not in expected:
        raise ValueError(f"unknown laziness scenario: {scenario}")
    if set(counts) != set(STAGES) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in counts.values()
    ):
        raise ValueError("invalid laziness counters")
    wanted = expected[scenario]
    if counts != wanted:
        raise ValueError(
            f"later stage work or incomplete controlled stage: "
            f"{scenario}: expected {wanted}, got {counts}"
        )


def validate_counter_results(results: dict[str, dict[str, int]]) -> None:
    expected_scenarios = {"tt", "capture", "killer"}
    if set(results) != expected_scenarios:
        raise ValueError("counter scenarios are incomplete or unexpected")
    for scenario, counts in results.items():
        validate_laziness(scenario, counts)


def validate_runner_binding(manifest: dict[str, Any]) -> None:
    runner_sha256 = manifest.get("runner_sha256")
    try:
        sources = manifest["engines"]["candidate"]["source"]
        frozen_runner = sources["scripts/ap04b_gate2.py"]
        frozen_policy = sources["scripts/ap04b_runtime_policy.py"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Gate-2 runner is not the frozen candidate source") from exc
    if runner_sha256 != frozen_runner:
        raise RuntimeError("Gate-2 runner is not the frozen candidate source")
    if manifest.get("runtime_policy_sha256") != frozen_policy:
        raise RuntimeError("Gate-2 runtime policy is not the frozen candidate source")


def fail(message: str) -> NoReturn:
    raise RuntimeError(message)


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
            fail(f"module has no path: {name}")
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(root / "src"):
            fail(f"unbound import: {name}: {path}")
        if ".cython_ext." in name and path.suffix != ".so":
            fail(f"native extension missing: {path}")
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
        "chess": importlib.import_module("chess").__version__,
        "runtime": runtime,
    }


def history_state(table: Any) -> Any:
    plane = getattr(table, "plane", None)
    if not callable(plane):
        return table
    planes = (plane(0), plane(1))
    return [
        [
            [planes[turn][source][target] for turn in range(2)]
            for target in range(64)
        ]
        for source in range(64)
    ]


def worker(payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(payload["root"]).resolve()
    identity = imported_identity(root)
    if payload["mode"] == "identity":
        return identity
    if identity != payload["identity"]:
        fail("worker imported identity changed")

    engine_module: Any = importlib.import_module("chess_engine.stockconda")
    chess = importlib.import_module("chess")
    engine = engine_module.UCIEngine()
    output: list[str] = []

    def capture_output(*args: Any, **kwargs: Any) -> None:
        del kwargs
        output.append(" ".join(map(str, args)))

    engine._uci_print = capture_output
    task_type = importlib.import_module(
        "chess_engine.uci_search_worker"
    ).SearchTask
    measurement: dict[str, Any] = {}

    def execute(task: Any) -> None:
        engine._active_cancel = task.cancelled
        move, score = engine.execute_search_task(
            None, task.depth_limit, False, task.fen, task.moves
        )
        engine._active_cancel = None
        measurement.update(
            bestmove=move.uci() if move else None,
            score=score,
        )

    engine._search_worker._execute = execute
    original_pvs = engine.pvs_search
    original_qsearch = engine.quiescence_search
    original_store = engine_module.store_tt_entry
    trace: list[Any] = []
    counts = {"pvs": 0, "qsearch": 0, "stores": 0}

    def pvs(board: Any, *args: Any, **kwargs: Any) -> Any:
        counts["pvs"] += 1
        trace.append(
            [
                "pvs",
                board.fen(en_passant="fen"),
                args,
                kwargs,
                [move.uci() for move in board.move_stack],
            ]
        )
        if payload.get("cancel_at") == counts["pvs"]:
            engine._active_cancel.set()
        result = original_pvs(board, *args, **kwargs)
        trace.append(
            ["pvs_return", result[0], result[1].uci() if result[1] else None]
        )
        return result

    def qsearch(board: Any, *args: Any, **kwargs: Any) -> Any:
        counts["qsearch"] += 1
        trace.append(["qsearch", board.fen(en_passant="fen"), args, kwargs])
        result = original_qsearch(board, *args, **kwargs)
        trace.append(["qsearch_return", result])
        return result

    def store(
        board: Any,
        depth: int,
        value: int,
        kind: str,
        move: Any,
        **kwargs: Any,
    ) -> Any:
        counts["stores"] += 1
        trace.append(
            [
                "store",
                board.fen(en_passant="fen"),
                depth,
                value,
                kind,
                move.uci() if move else None,
                kwargs,
            ]
        )
        return original_store(board, depth, value, kind, move, **kwargs)

    try:
        engine.handle_setoption(["name", "Hash", "value", "16"])
        engine.handle_setoption(["name", "Book", "value", "false"])
        warm = engine._search_worker.submit(chess.STARTING_FEN, None, 2, False)
        if not warm.completed.wait(110) or engine._search_worker.failure:
            fail("warmup failed")
        engine.handle_ucinewgame()
        output.clear()
        engine.pvs_search = pvs
        engine.quiescence_search = qsearch
        engine_module.store_tt_entry = store
        task = engine._search_worker.submit(
            payload["fen"],
            None,
            payload["depth"],
            False,
            moves=tuple(payload.get("moves", [])),
        )
        if not isinstance(task, task_type):
            fail("unexpected search task type")
        if not task.completed.wait(110) or engine._search_worker.failure:
            fail(f"search failed: {engine._search_worker.failure!r}")
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
            "counts": counts,
            "trace": trace,
            "history": history_state(engine.history_table),
            "killers": [
                [move.uci() if move else None for move in row]
                for row in engine.killer_moves
            ],
            "board": engine.board.fen(),
            "stack": [move.uci() for move in engine.board.move_stack],
        }
        return {"identity": identity, "semantic": semantic, "pid": os.getpid()}
    finally:
        engine_module.store_tt_entry = original_store
        engine._shutdown()


def counter_worker(payload: dict[str, Any]) -> dict[str, int]:
    """Exercise one controlled cutoff through the real staged PVS body."""
    root = Path(payload["root"]).resolve()
    if imported_identity(root) != payload["identity"]:
        fail("counter worker imported identity changed")
    scenario = str(payload["scenario"])
    if scenario not in {"tt", "capture", "killer"}:
        fail(f"unknown counter scenario: {scenario}")

    engine_module: Any = importlib.import_module("chess_engine.stockconda")
    chess = importlib.import_module("chess")
    engine = engine_module.UCIEngine.__new__(engine_module.UCIEngine)
    engine.stop_flag = False
    engine.stop_flag_check = lambda: False
    engine.history_table = engine_module.HistoryTable()
    engine.killer_moves = [
        [None, None] for _ in range(engine.MAX_LMR_DEPTH)
    ]
    engine.lmr_reduction_table = [
        [0 for _ in range(engine.MAX_MOVES_PER_POSITION)]
        for _ in range(engine.MAX_LMR_DEPTH + 1)
    ]
    engine.history_factors = {
        "beta_cutoff": [1 for _ in range(engine.MAX_LMR_DEPTH + 1)],
        "alpha_improve": [1 for _ in range(engine.MAX_LMR_DEPTH + 1)],
        "regular_update": [1 for _ in range(engine.MAX_LMR_DEPTH + 1)],
    }

    if scenario == "capture":
        board = chess.Board("7k/8/8/3p4/8/2N5/8/K7 w - - 0 1")
        cutoff_move = chess.Move.from_uci("c3d5")
        tt_move = None
    else:
        board = chess.Board()
        cutoff_move = chess.Move.from_uci("e2e4")
        tt_move = cutoff_move if scenario == "tt" else None
    if scenario == "killer":
        engine.killer_moves[0] = [cutoff_move, None]

    counts = {"captures": 0, "killers": 0, "other": 0}
    names = {
        "_generate_pvs_captures": "captures",
        "_generate_pvs_killers": "killers",
        "_prepare_pvs_other": "other",
    }
    for method_name, counter_name in names.items():
        original = getattr(engine, method_name)

        def counted(
            *args: Any,
            _original: Any = original,
            _counter: str = counter_name,
            **kwargs: Any,
        ) -> Any:
            counts[_counter] += 1
            return _original(*args, **kwargs)

        setattr(engine, method_name, counted)

    original_probe_entry = engine_module.probe_tt_entry
    original_probe_move = engine_module.probe_tt_move
    original_store = engine_module.store_tt_entry
    engine_module.probe_tt_entry = lambda *_args, **_kwargs: None
    engine_module.probe_tt_move = (
        lambda *_args, **_kwargs: (tt_move, None)
    )
    engine_module.store_tt_entry = lambda *_args, **_kwargs: None
    real_pvs = engine_module.UCIEngine.pvs_search.__get__(engine)

    def dispatch(
        current: Any,
        depth: int,
        alpha: int,
        beta: int,
        lmr_depth: int = 3,
        ply: int = 0,
        *,
        tt_context: tuple[int, int] | None = None,
    ) -> Any:
        if ply:
            if current.move_stack[-1] != cutoff_move:
                fail("controlled cutoff did not search the expected first move")
            return -100, None
        return real_pvs(
            current,
            depth,
            alpha,
            beta,
            lmr_depth,
            ply,
            tt_context=tt_context,
        )

    engine.pvs_search = dispatch
    try:
        result = engine.pvs_search(
            board, 1, -200, 50, tt_context=(11, 22)
        )
        if result != (100, cutoff_move):
            fail(f"controlled cutoff returned {result!r}")
        validate_laziness(scenario, counts)
        return counts
    finally:
        engine_module.probe_tt_entry = original_probe_entry
        engine_module.probe_tt_move = original_probe_move
        engine_module.store_tt_entry = original_store


def invoke(
    root: Path, payload: dict[str, Any], timeout: float = 120.0
) -> dict[str, Any]:
    frozen_identity = payload.get("identity")
    packages = (
        frozen_identity.get("packages")
        if isinstance(frozen_identity, dict)
        else {"chess": RUNTIME_POLICY.imported_package_identity("chess")}
    )
    if not isinstance(packages, dict):
        fail("worker package identity is missing")
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
        fail(f"worker exit {result.returncode}: {result.stdout}\n{result.stderr}")
    if result.stderr:
        fail(f"worker stderr: {result.stderr}")
    loaded = json.loads(result.stdout)
    if not isinstance(loaded, dict):
        fail("worker output is not an object")
    return loaded


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
    """Bind every archived authored-source member to its build file."""
    if sha(archive) != expected_sha256:
        fail("archive SHA-256 mismatch")
    sources: dict[str, str] = {}
    with tarfile.open(archive) as stream:
        for member in stream.getmembers():
            relative = member.name.replace("\\", "/").removeprefix("./")
            relative_path = Path(relative)
            if (
                not member.isfile()
                or not is_authored_source(relative)
            ):
                continue
            if relative_path.is_absolute() or ".." in relative_path.parts:
                fail(f"unsafe archive member: {relative}")
            extracted = stream.extractfile(member)
            if extracted is None:
                fail(f"cannot read archive member: {relative}")
            digest = hashlib.sha256(extracted.read()).hexdigest()
            path = root / relative_path
            if not path.is_file() or sha(path) != digest:
                fail(f"archive/source mismatch: {relative}")
            sources[relative] = digest
    if not sources:
        fail("archive/source mismatch: no source files")
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
        fail(f"unexpected build-root source: {unexpected[0]}")
    return sources


def freeze(args: argparse.Namespace) -> None:
    new_output(args.output)
    fixtures = json.loads(args.fixtures.read_text(encoding="utf-8"))
    positions = fixtures.get("positions")
    if not isinstance(positions, list) or len(positions) != 20:
        fail("Gate-2 fixture file must contain exactly 20 positions")
    fixture_copy = args.output / "fixtures.json"
    fixture_copy.write_bytes(args.fixtures.read_bytes())
    manifest: dict[str, Any] = {
        "runner_sha256": sha(Path(__file__)),
        "runtime_policy_sha256": sha(
            Path(__file__).with_name("ap04b_runtime_policy.py")
        ),
        "fixture_copy": str(fixture_copy),
        "fixture_sha256": sha(fixture_copy),
        "positions": positions,
        "depths": [1, 2, 3, 4, 5],
        "engines": {},
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
        },
    }
    for label in ENGINES:
        root = Path(getattr(args, label)).resolve()
        archive = Path(getattr(args, f"{label}_archive")).resolve()
        archive_sha256 = sha(archive)
        manifest["engines"][label] = {
            "root": str(root),
            "archive": str(archive),
            "archive_sha256": archive_sha256,
            "source": verify_archive(root, archive, archive_sha256),
            "executables": RUNTIME_POLICY.inventory_executables(root),
            "identity": invoke(root, {"mode": "identity"}),
        }
    if (
        manifest["engines"]["baseline"]["root"]
        == manifest["engines"]["candidate"]["root"]
    ):
        fail("build roots must be independent")
    validate_runner_binding(manifest)
    write_json(args.output / "manifest.json", manifest)


def validate_manifest(manifest: dict[str, Any]) -> None:
    validate_runner_binding(manifest)
    if sha(Path(__file__)) != manifest["runner_sha256"]:
        fail("runner changed after freeze")
    if sha(Path(__file__).with_name("ap04b_runtime_policy.py")) != manifest[
        "runtime_policy_sha256"
    ]:
        fail("runtime policy changed after freeze")
    if sha(Path(manifest["fixture_copy"])) != manifest["fixture_sha256"]:
        fail("fixture file changed after freeze")
    for label in ENGINES:
        entry = manifest["engines"][label]
        root = Path(entry["root"])
        archive = Path(entry["archive"])
        try:
            current_sources = verify_archive(
                root, archive, entry["archive_sha256"]
            )
        except RuntimeError as error:
            fail(f"{label} {error}")
        if current_sources != entry["source"]:
            fail(f"{label} sources changed")
        RUNTIME_POLICY.verify_executable_root(root, entry["executables"])
        if invoke(root, {"mode": "identity"}) != entry["identity"]:
            fail(f"{label} imported identity changed")


def parity(args: argparse.Namespace) -> dict[str, Any]:
    new_output(args.output)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report: dict[str, Any] = {
        "status": "RUNNING",
        "manifest_sha256": sha(args.manifest),
        "runner_sha256": sha(Path(__file__)),
        "comparisons": 0,
    }
    write_json(args.output / "start.json", report)
    try:
        validate_manifest(manifest)
        for fixture_index, fixture in enumerate(manifest["positions"]):
            for depth in manifest["depths"]:
                pair: dict[str, Any] = {}
                for label in ENGINES:
                    entry = manifest["engines"][label]
                    result = invoke(
                        Path(entry["root"]),
                        {
                            "mode": "parity",
                            **fixture,
                            "depth": depth,
                            "identity": entry["identity"],
                        },
                    )
                    result_path = (
                        args.output
                        / f"result-{fixture_index:02d}-d{depth}-{label}.json"
                    )
                    write_json(result_path, result)
                    pair[label] = result["semantic"]
                compare_semantics(pair["baseline"], pair["candidate"])
                report["comparisons"] += 1
                print(
                    f"parity {report['comparisons']}/100 "
                    f"{fixture['label']} depth {depth} PASS",
                    flush=True,
                )
        validate_manifest(manifest)
        report["status"] = "PASS"
    except BaseException as exc:
        report.update(status="FAIL", error=repr(exc))
    write_json(args.output / "result.json", report)
    print(json.dumps(report, sort_keys=True), flush=True)
    return report


def counters(args: argparse.Namespace) -> dict[str, Any]:
    new_output(args.output)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report: dict[str, Any] = {
        "status": "RUNNING",
        "manifest_sha256": sha(args.manifest),
        "runner_sha256": sha(Path(__file__)),
    }
    write_json(args.output / "start.json", report)
    try:
        validate_manifest(manifest)
        candidate = manifest["engines"]["candidate"]
        results: dict[str, dict[str, int]] = {}
        for scenario in ("tt", "capture", "killer"):
            result = invoke(
                Path(candidate["root"]),
                {
                    "mode": "counter",
                    "scenario": scenario,
                    "identity": candidate["identity"],
                },
            )
            counts = result.get("counts")
            if not isinstance(counts, dict):
                fail("counter worker did not return counters")
            results[scenario] = counts
        validate_counter_results(results)
        report.update(status="PASS", results=results)
    except BaseException as exc:
        report.update(status="FAIL", error=repr(exc))
    write_json(args.output / "result.json", report)
    print(json.dumps(report, sort_keys=True), flush=True)
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
    freeze_parser.add_argument("--output", type=Path, required=True)
    parity_parser = commands.add_parser("parity")
    parity_parser.add_argument("--manifest", type=Path, required=True)
    parity_parser.add_argument("--output", type=Path, required=True)
    counter_parser = commands.add_parser("counter")
    counter_parser.add_argument("--manifest", type=Path, required=True)
    counter_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "worker":
        payload = json.loads(args.payload)
        if payload["mode"] == "counter":
            print(json.dumps({"counts": counter_worker(payload)}))
        else:
            print(json.dumps(worker(payload)))
        return 0
    if args.command == "freeze":
        freeze(args)
        return 0
    if args.command == "counter":
        return 0 if counters(args)["status"] == "PASS" else 3
    return 0 if parity(args)["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())

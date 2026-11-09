"""Collect untimed search/TT counters without adding production telemetry."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


COUNT_KEYS = (
    "pvs",
    "qsearch",
    "score_probes",
    "score_hits",
    "move_probes",
    "move_hits",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def counter_delta(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {key: after[key] - before[key] for key in COUNT_KEYS}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--position-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--disable-score", action="store_true")
    args_cli = parser.parse_args()

    source = args_cli.source.resolve(strict=True)
    position_file = args_cli.position_file.resolve(strict=True)
    output = args_cli.output.resolve(strict=False)
    assert output != position_file
    assert not output.exists(), f"refusing to overwrite {output}"
    output.parent.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(source))
    import chess
    import chess_engine.stockconda as module

    imported = Path(module.__file__).resolve(strict=True)
    assert imported.is_relative_to(source), (imported, source)
    assert imported.name == "stockconda.py"

    raw_lines = position_file.read_text(encoding="utf-8").splitlines()
    lines = [line.strip() for line in raw_lines if line.strip()]
    assert len(lines) == 1 and lines[0].startswith("position "), lines
    position_command = lines[0]

    engine = module.UCIEngine()
    try:
        engine.use_opening_book = False
        engine.handle_position(position_command.split()[1:])
        board_fen = engine.board.fen()
        move_stack_plies = len(engine.board.move_stack)

        counts = {key: 0 for key in COUNT_KEYS}
        real_pvs = engine.pvs_search
        real_qsearch = engine.quiescence_search
        real_score = module.probe_tt_entry
        real_move = module.probe_tt_move

        def pvs(*args: Any, **kwargs: Any) -> Any:
            counts["pvs"] += 1
            return real_pvs(*args, **kwargs)

        def qsearch(*args: Any, **kwargs: Any) -> Any:
            counts["qsearch"] += 1
            return real_qsearch(*args, **kwargs)

        def score_probe(*args: Any, **kwargs: Any) -> Any:
            counts["score_probes"] += 1
            hit = None if args_cli.disable_score else real_score(*args, **kwargs)
            counts["score_hits"] += int(hit is not None)
            return hit

        def move_probe(*args: Any, **kwargs: Any) -> Any:
            counts["move_probes"] += 1
            hit = real_move(*args, **kwargs)
            counts["move_hits"] += int(hit[0] is not None)
            return hit

        searches: list[dict[str, object]] = []
        engine.pvs_search = pvs
        engine.quiescence_search = qsearch
        module.probe_tt_entry = score_probe
        module.probe_tt_move = move_probe
        try:
            for phase in ("cold", "warm-identical-history"):
                before = counts.copy()
                move, score = engine.execute_search_task(None, 5, False)
                assert move is not None and move in engine.board.legal_moves
                searches.append(
                    {
                        "phase": phase,
                        "requested_depth": 5,
                        "move": move.uci(),
                        "score": score,
                        "legal_move": True,
                        "delta_counts": counter_delta(counts, before),
                    }
                )
        finally:
            engine.pvs_search = real_pvs
            engine.quiescence_search = real_qsearch
            module.probe_tt_entry = real_score
            module.probe_tt_move = real_move
    finally:
        engine.handle_quit()

    extensions = {
        path.name: sha256(path)
        for path in sorted((source / "chess_engine/cython_ext").glob("*.so"))
    }
    assert len(extensions) == 3, extensions
    document = {
        "status": "PASS",
        "instrumented": True,
        "instrumented_elapsed_time_is_cost_evidence": False,
        "requested_depth": 5,
        "disable_score": args_cli.disable_score,
        "score_reuse_disabled_only": args_cli.disable_score,
        "move_hints_enabled": True,
        "stores_enabled": True,
        "draw_policy_unchanged": True,
        "counts_include_pv_reconstruction": False,
        "move_probe_scope": (
            "PVS move ordering; direct execute_search_task does not reconstruct PV"
        ),
        "source": str(source),
        "imported_stockconda": str(imported),
        "imported_module_inside_source": True,
        "stockconda_sha256": sha256(imported),
        "extension_sha256": extensions,
        "tool_sha256": sha256(Path(__file__)),
        "position_file": str(position_file),
        "input_sha256": sha256(position_file),
        "position_command": position_command,
        "board_fen": board_fen,
        "input_move_stack_plies": move_stack_plies,
        "counts": counts,
        "searches": searches,
    }
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, indent=2)
        stream.write("\n")
    print(json.dumps(document, indent=2), flush=True)


if __name__ == "__main__":
    main()

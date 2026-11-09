"""Audit an AP04 match resumed from a durable Fastchess checkpoint."""
from __future__ import annotations

import argparse
import hashlib
from importlib import util
import json
import math
from pathlib import Path
import re
from typing import Any, Sequence, cast

import chess
import chess.pgn


NAMES = ("AP04_TTFirst_741b05c", "AP04_Baseline_106f500")
TC = "10+0.1"
CONCURRENCY = 4
SEED = 90210
EXPECTED_LAUNCHERS = (
    "/build/ap04_flat_gate_candidate_2/ap04-launch.sh",
    "/build/ap04_flat_gate_baseline_1/ap04-launch.sh",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_game_segments(
    prefix_path: Path,
    continuation_path: Path,
    names: Sequence[str],
    tc: str,
    checkpoint_games: int,
    expected_games: int,
    openings: Sequence[Sequence[chess.Move]],
) -> dict[str, Any]:
    assert checkpoint_games % 2 == expected_games % 2 == 0
    checkpoint_pairs = checkpoint_games // 2
    expected_pairs = expected_games // 2
    assert len(names) == 2 and len(set(names)) == 2
    assert len(openings) == expected_pairs
    seen: set[tuple[int, str]] = set()
    pair_scores: dict[int, list[int]] = {}
    results = [0, 0, 0]
    ignored = 0

    def consume(path: Path, prefix: bool) -> None:
        nonlocal ignored
        with path.open() as stream:
            while (game := chess.pgn.read_game(stream)) is not None:
                assert not game.errors
                pair = int(game.headers["Round"]) - 1
                assert 0 <= pair < expected_pairs
                if prefix and pair >= checkpoint_pairs:
                    ignored += 1
                    continue
                assert prefix == (pair < checkpoint_pairs), "segment crosses checkpoint"
                white, black = game.headers["White"], game.headers["Black"]
                assert {white, black} == set(names)
                key = pair, white
                assert key not in seen, "duplicate game"
                seen.add(key)
                assert game.headers["Termination"] in {"normal", "adjudication"}
                assert game.headers["TimeControl"] == tc
                result = game.headers["Result"]
                assert result in {"1-0", "0-1", "1/2-1/2"}
                if result == "1/2-1/2":
                    results[2] += 1
                    score = 1
                elif (result == "1-0") == (white == names[0]):
                    results[0] += 1
                    score = 2
                else:
                    results[1] += 1
                    score = 0
                pair_scores.setdefault(pair, []).append(score)
                moves = list(game.mainline_moves())
                opening = openings[pair]
                assert moves[:len(opening)] == list(opening)
                board = game.board()
                for move in moves:
                    assert move in board.legal_moves
                    board.push(move)

    consume(prefix_path, True)
    consume(continuation_path, False)
    assert len(seen) == expected_games
    penta = [0] * 5
    for pair in range(expected_pairs):
        scores = pair_scores[pair]
        assert len(scores) == 2
        penta[sum(scores)] += 1
    return {
        "candidate_wld": results,
        "penta": penta,
        "ignored_post_checkpoint_games": ignored,
    }


def audit_resume_console(console: str, checkpoint_games: int, expected_games: int) -> None:
    assert f"Resuming from {checkpoint_games} games" in console
    finished = [int(value) for value in re.findall(r"^Finished game (\d+)", console, re.M)]
    assert len(finished) == expected_games - checkpoint_games
    assert set(finished) == set(range(checkpoint_games + 1, expected_games + 1))
    assert "Finished match" in console


def elo_decision(estimate: float, half_width: float) -> dict[str, float | str]:
    assert math.isfinite(estimate) and math.isfinite(half_width) and half_width >= 0
    standard_error = half_width / 1.959964
    lower = estimate - 1.644854 * standard_error
    upper = estimate + 1.644854 * standard_error
    status = "PASS" if lower > -20 else "FAIL" if upper < -20 else "HOLD"
    return {
        "elo": estimate,
        "half_width": half_width,
        "lower": lower,
        "upper": upper,
        "status": status,
    }


def load_openings(book: Path, count: int) -> list[list[chess.Move]]:
    openings: list[list[chess.Move]] = []
    with book.open() as stream:
        for _ in range(count):
            game = chess.pgn.read_game(stream)
            assert game is not None and not game.errors
            openings.append(list(game.mainline_moves()))
    return openings


def validate_saved_config(saved: dict[str, Any], scheduled_games: int) -> None:
    assert saved["rounds"] * saved["games"] == scheduled_games
    assert saved["concurrency"] == CONCURRENCY and saved["seed"] == SEED
    assert not saved["sprt"]["enabled"] and not saved["recover"]
    assert [engine["name"] for engine in saved["engines"]] == list(NAMES)
    assert [engine["cmd"] for engine in saved["engines"]] == list(EXPECTED_LAUNCHERS)
    for engine in saved["engines"]:
        assert engine["options"] == [["Book", "false"], ["Hash", "16"]]
        assert engine["limit"]["tc"] == {
            "time": 10000,
            "increment": 100,
            "fixed_time": 0,
            "moves": 0,
            "timemargin": 0,
        }


def completed_games(saved: dict[str, Any]) -> int:
    stats = saved["stats"][NAMES[0] + " vs " + NAMES[1]]
    return int(stats["wins"] + stats["losses"] + stats["draws"])


def load_trace_auditor() -> Any:
    verifier = load_identity_verifier()
    module = verifier.load_match_auditor()
    module.NAMES = list(NAMES)
    return module


def load_identity_verifier() -> Any:
    path = Path(__file__).with_name("ap04_match_pipeline.py")
    spec = util.spec_from_file_location("ap04_recovery_identity", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load recovery identity verifier")
    module: Any = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recovery_identity_snapshot() -> dict[str, Any]:
    """Runner interface: capture at actual recovery start and after its exit."""
    verifier = load_identity_verifier()
    manifest = verifier.verify_identity()
    return {
        "manifest_sha256": verifier.EXPECTED_MANIFEST,
        "performance_sha256": verifier.EXPECTED_PERFORMANCE,
        "performance_audit_sha256": verifier.EXPECTED_PERFORMANCE_AUDIT,
        "match_auditor_sha256": verifier.EXPECTED_MATCH_AUDITOR,
        "uci_auditor_sha256": verifier.EXPECTED_UCI_AUDITOR,
        "auditor_hash_mode": verifier.AUDITOR_HASH_MODE,
        "launcher_sha256": dict(verifier.EXPECTED_LAUNCHER_SHA256),
        "book_sha256": verifier.EXPECTED_BOOK,
        "engines": manifest["engines"],
    }


def validate_recovery_record(
    path: Path, prefix_root: Path, continuation_root: Path, book: Path,
) -> str:
    """Validate runner-supplied pre/post evidence; never synthesize missing history."""
    raw = path.read_bytes()
    expected = recovery_identity_snapshot()
    try:
        record = json.loads(raw)
        before, after = record["before"], record["after"]
        valid = (
            record["version"] == 1 and record["exit_code"] == 0
            and before["identity"] == after["identity"] == expected
            and math.isfinite(before["epoch"]) and math.isfinite(after["epoch"])
            and 0 < before["epoch"] <= after["epoch"]
            and record["checkpoint_sha256"] == sha(prefix_root / "autosave.json")
            and record["book_sha256"] == sha(book) == expected["book_sha256"]
            and record["continuation_sha256"] == {
                name: sha(continuation_root / name)
                for name in ("autosave.json", "console.log", "protocol.log", "games.pgn", "exit_code")
            }
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid recovery identity record") from error
    if not valid:
        raise ValueError("recovery identity record does not bind this run")
    return hashlib.sha256(raw).hexdigest()


def audit_protocol_segments(
    prefix_path: Path, continuation_path: Path,
) -> dict[str, dict[str, Any]]:
    """Audit the interrupted prefix and the normally completed continuation."""
    trace_auditor = load_trace_auditor()
    with prefix_path.open() as stream:
        checkpoint = cast(
            dict[str, Any],
            trace_auditor.audit_trace(
                stream, CONCURRENCY, 100, allow_interrupted=True
            ),
        )
    with continuation_path.open() as stream:
        continuation = cast(
            dict[str, Any], trace_auditor.audit_trace(stream, CONCURRENCY, 100)
        )
    return {"checkpoint": checkpoint, "continuation": continuation}


def audit_release(
    prefix_root: Path,
    continuation_root: Path,
    book: Path,
    output: Path,
    checkpoint_games: int = 1620,
    expected_games: int = 2000,
    *,
    identity_record: Path | None = None,
    historical_unbound: bool = False,
) -> dict[str, Any]:
    if historical_unbound and identity_record is not None:
        raise ValueError("historical mode cannot accept an identity record")
    if not historical_unbound and identity_record is None:
        raise ValueError("recovery identity record required; use explicit historical mode for unbound evidence")
    record_digest = (
        validate_recovery_record(identity_record, prefix_root, continuation_root, book)
        if identity_record is not None else None
    )
    assert (continuation_root / "exit_code").read_text().strip() == "0"
    checkpoint = cast(
        dict[str, Any], json.loads((prefix_root / "autosave.json").read_text())
    )
    final = cast(
        dict[str, Any], json.loads((continuation_root / "autosave.json").read_text())
    )
    validate_saved_config(checkpoint, expected_games)
    validate_saved_config(final, expected_games)
    assert completed_games(checkpoint) == checkpoint_games
    assert completed_games(final) == expected_games
    openings = load_openings(book, expected_games // 2)
    games = audit_game_segments(
        prefix_root / "games.pgn",
        continuation_root / "games.pgn",
        NAMES,
        TC,
        checkpoint_games,
        expected_games,
        openings,
    )
    prefix_console = (prefix_root / "console.log").read_text()
    prefix_finished = {int(value) for value in re.findall(
        r"^Finished game (\d+)", prefix_console, re.M
    )}
    assert set(range(1, checkpoint_games + 1)) <= prefix_finished
    continuation_console = (continuation_root / "console.log").read_text()
    audit_resume_console(continuation_console, checkpoint_games, expected_games)
    summary = continuation_console.rsplit("Results of ", 1)[-1]
    totals = re.search(
        r"^Games: (\d+), Wins: (\d+), Losses: (\d+), Draws: (\d+),",
        summary,
        re.M,
    )
    assert totals and [int(value) for value in totals.groups()] == [
        expected_games,
        *games["candidate_wld"],
    ]
    estimate_match = re.search(r"^Elo: (-?[\d.]+) \+/- ([\d.]+),", summary, re.M)
    assert estimate_match
    decision = elo_decision(*(float(value) for value in estimate_match.groups()))
    stats = final["stats"][NAMES[0] + " vs " + NAMES[1]]
    assert [stats[key] for key in ("wins", "losses", "draws")] == games["candidate_wld"]
    assert games["penta"] == [
        stats["penta_LL"],
        stats["penta_LD"],
        stats["penta_WL"] + stats["penta_DD"],
        stats["penta_WD"],
        stats["penta_WW"],
    ]
    traces = audit_protocol_segments(
        prefix_root / "protocol.log", continuation_root / "protocol.log"
    )
    report: dict[str, Any] = {
        "status": "HOLD" if historical_unbound and decision["status"] == "PASS" else decision["status"],
        "identity_verdict": "HISTORICAL_UNBOUND" if historical_unbound else "PASS",
        "games": expected_games,
        "checkpoint_games": checkpoint_games,
        "candidate_wld": games["candidate_wld"],
        "penta": games["penta"],
        "ignored_post_checkpoint_games": games["ignored_post_checkpoint_games"],
        "strength_verdict": decision,
        "checkpoint_trace": traces["checkpoint"],
        "continuation_trace": traces["continuation"],
        "sha256": {
            "checkpoint_autosave.json": sha(prefix_root / "autosave.json"),
            "checkpoint_games.pgn": sha(prefix_root / "games.pgn"),
            "checkpoint_protocol.log": sha(prefix_root / "protocol.log"),
            "continuation_autosave.json": sha(continuation_root / "autosave.json"),
            "continuation_games.pgn": sha(continuation_root / "games.pgn"),
            "continuation_console.log": sha(continuation_root / "console.log"),
            "continuation_protocol.log": sha(continuation_root / "protocol.log"),
            "book": sha(book),
        },
    }
    if identity_record is not None:
        if validate_recovery_record(identity_record, prefix_root, continuation_root, book) != record_digest:
            raise ValueError("recovery identity record changed during audit")
        report["sha256"]["identity_record"] = record_digest
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix-root", type=Path, required=True)
    parser.add_argument("--continuation-root", type=Path, required=True)
    parser.add_argument("--book", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--identity-record", type=Path)
    identity.add_argument(
        "--historical-unbound", action="store_true",
        help="historical analysis only; missing run identity prevents release PASS",
    )
    args = parser.parse_args()
    report = audit_release(
        args.prefix_root, args.continuation_root, args.book, args.output,
        identity_record=args.identity_record, historical_unbound=args.historical_unbound,
    )
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 3 if report["status"] == "HOLD" else 1


if __name__ == "__main__":
    raise SystemExit(main())

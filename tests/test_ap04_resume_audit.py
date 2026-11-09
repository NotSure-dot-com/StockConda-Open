from importlib import util
import hashlib
import json
from pathlib import Path
from typing import Any

import chess
import chess.pgn
import pytest


NAMES = ("candidate", "baseline")


def auditor() -> Any:
    path = Path(__file__).parents[1] / "scripts/ap04_resume_audit.py"
    assert path.exists(), "AP04 resume auditor has not been implemented"
    spec = util.spec_from_file_location("ap04_resume_audit", path)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_game(stream: Any, round_number: int, white: str, result: str) -> None:
    game = chess.pgn.Game()
    game.headers.update({
        "Round": str(round_number),
        "White": white,
        "Black": NAMES[1] if white == NAMES[0] else NAMES[0],
        "Result": result,
        "Termination": "adjudication",
        "TimeControl": "10+0.1",
    })
    print(game, file=stream, end="\n\n")


def write_segments(root: Path) -> tuple[Path, Path]:
    prefix = root / "prefix.pgn"
    continuation = root / "continuation.pgn"
    with prefix.open("w") as stream:
        write_game(stream, 1, NAMES[0], "1-0")
        write_game(stream, 1, NAMES[1], "1/2-1/2")
        # This completed game is after the durable checkpoint and must be ignored.
        write_game(stream, 2, NAMES[0], "0-1")
    with continuation.open("w") as stream:
        write_game(stream, 2, NAMES[0], "1/2-1/2")
        write_game(stream, 2, NAMES[1], "0-1")
    return prefix, continuation


def test_segmented_games_use_only_checkpoint_prefix_and_unique_continuation(
    tmp_path: Path,
) -> None:
    prefix, continuation = write_segments(tmp_path)

    result = auditor().audit_game_segments(
        prefix, continuation, NAMES, "10+0.1", checkpoint_games=2,
        expected_games=4, openings=[[], []],
    )

    assert result == {
        "candidate_wld": [2, 0, 2],
        "penta": [0, 0, 0, 2, 0],
        "ignored_post_checkpoint_games": 1,
    }


def test_segmented_games_reject_duplicate_continuation_color(tmp_path: Path) -> None:
    prefix, continuation = write_segments(tmp_path)
    with continuation.open("a") as stream:
        write_game(stream, 2, NAMES[0], "1-0")

    with pytest.raises(AssertionError, match="duplicate game"):
        auditor().audit_game_segments(
            prefix, continuation, NAMES, "10+0.1", checkpoint_games=2,
            expected_games=4, openings=[[], []],
        )


def test_resume_console_must_cover_exact_remaining_game_ids() -> None:
    module = auditor()
    console = "Resuming from 2 games\nFinished game 3\nFinished game 4\nFinished match\n"
    module.audit_resume_console(console, checkpoint_games=2, expected_games=4)

    with pytest.raises(AssertionError):
        module.audit_resume_console(
            console + "Finished game 4\n", checkpoint_games=2, expected_games=4
        )


def test_noninferiority_uses_one_sided_95_percent_bound() -> None:
    decision = auditor().elo_decision(5.04, 12.00)

    assert decision["status"] == "PASS"
    assert -5.1 < decision["lower"] < -5.0


def test_checkpoint_config_keeps_scheduled_total_but_has_partial_stats() -> None:
    module = auditor()
    saved = {
        "rounds": 2,
        "games": 2,
        "concurrency": 4,
        "seed": 90210,
        "sprt": {"enabled": False},
        "recover": False,
        "engines": [
            {
                "name": name,
                "cmd": launcher,
                "options": [["Book", "false"], ["Hash", "16"]],
                "limit": {"tc": {
                    "time": 10000,
                    "increment": 100,
                    "fixed_time": 0,
                    "moves": 0,
                    "timemargin": 0,
                }},
            }
            for name, launcher in zip(module.NAMES, module.EXPECTED_LAUNCHERS)
        ],
        "stats": {
            module.NAMES[0] + " vs " + module.NAMES[1]: {
                "wins": 1, "losses": 0, "draws": 1,
            }
        },
    }

    module.validate_saved_config(saved, scheduled_games=4)
    assert module.completed_games(saved) == 2


def test_protocol_segments_audit_prefix_as_interrupted_and_continuation_as_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = auditor()
    prefix = tmp_path / "prefix.log"
    continuation = tmp_path / "continuation.log"
    prefix.write_text("prefix trace\n")
    continuation.write_text("continuation trace\n")
    calls: list[tuple[str, int, int | None, bool]] = []

    class FakeAuditor:
        @staticmethod
        def audit_trace(
            lines: Any, concurrency: int, expected_increment: int | None = None,
            *, allow_interrupted: bool = False,
        ) -> dict[str, bool]:
            calls.append((lines.read(), concurrency, expected_increment, allow_interrupted))
            return {"interrupted": allow_interrupted}

    monkeypatch.setattr(module, "load_trace_auditor", lambda: FakeAuditor())

    result = module.audit_protocol_segments(prefix, continuation)

    assert calls == [
        ("prefix trace\n", module.CONCURRENCY, 100, True),
        ("continuation trace\n", module.CONCURRENCY, 100, False),
    ]
    assert result == {
        "checkpoint": {"interrupted": True},
        "continuation": {"interrupted": False},
    }


def release_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, Path, Path]:
    module = auditor()
    monkeypatch.setattr(module, "NAMES", NAMES)
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    prefix_root, continuation_root = tmp_path / "prefix", tmp_path / "continuation"
    prefix_root.mkdir()
    continuation_root.mkdir()
    prefix, continuation = write_segments(tmp_path)
    prefix.rename(prefix_root / "games.pgn")
    continuation.rename(continuation_root / "games.pgn")
    book = tmp_path / "book.pgn"
    with book.open("w") as stream:
        for pair in (1, 2):
            write_game(stream, pair, NAMES[0], "1/2-1/2")
    for root, games in ((prefix_root, 2), (continuation_root, 4)):
        config = {
            "rounds": 2, "games": 2, "concurrency": 4, "seed": 90210,
            "sprt": {"enabled": False}, "recover": False,
            "engines": [{
                "name": name, "cmd": launcher,
                "options": [["Book", "false"], ["Hash", "16"]],
                "limit": {"tc": {"time": 10000, "increment": 100, "fixed_time": 0,
                                  "moves": 0, "timemargin": 0}},
            } for name, launcher in zip(NAMES, module.EXPECTED_LAUNCHERS)],
            "stats": {NAMES[0] + " vs " + NAMES[1]: {
                "wins": games // 2, "losses": 0, "draws": games // 2,
                "penta_LL": 0, "penta_LD": 0, "penta_WL": 0,
                "penta_DD": 0, "penta_WD": games // 2, "penta_WW": 0,
            }},
        }
        (root / "autosave.json").write_text(json.dumps(config))
        lines = []
        for thread in range(4):
            for index, name in enumerate(NAMES):
                engine = f"[Engine] <{thread}> {name}"
                lines.extend([
                    f"{engine} <--- position startpos",
                    f"{engine} <--- go wtime 10000 btime 10000 winc 100 binc 100",
                    f"{engine} ---> bestmove e2e4",
                ])
                if root == continuation_root:
                    lines.extend([
                        f"{engine} <--- quit",
                        f"[Info] <{thread}> fastchess --- Process with pid: {thread * 2 + index} terminated with status: 0",
                        f"[Engine] <{thread}> <stderr> {name} ---> Process exited normally with status 0",
                    ])
        (root / "protocol.log").write_text("\n".join(lines) + "\n")
    (prefix_root / "console.log").write_text("Finished game 1\nFinished game 2\n")
    (continuation_root / "console.log").write_text(
        "Resuming from 2 games\nFinished game 3\nFinished game 4\n"
        "Results of candidate vs baseline (10+0.1)\n"
        "Games: 4, Wins: 2, Losses: 0, Draws: 2,\n"
        "Elo: 5.04 +/- 12.00,\nFinished match\n"
    )
    (continuation_root / "exit_code").write_text("0\n")
    return module, prefix_root, continuation_root, book


def test_release_writes_new_report_from_real_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, prefix, continuation, book = release_fixture(tmp_path, monkeypatch)
    output = tmp_path / "report.json"
    record = recovery_record(module, prefix, continuation, book, tmp_path, monkeypatch)
    report = module.audit_release(prefix, continuation, book, output, 2, 4, identity_record=record)
    assert report["status"] == "PASS" and report["games"] == 4
    assert json.loads(output.read_text()) == report


def test_release_refuses_to_overwrite_existing_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, prefix, continuation, book = release_fixture(tmp_path, monkeypatch)
    output = tmp_path / "report.json"
    record = recovery_record(module, prefix, continuation, book, tmp_path, monkeypatch)
    output.write_text("existing audit evidence\n")
    with pytest.raises(FileExistsError):
        module.audit_release(prefix, continuation, book, output, 2, 4, identity_record=record)
    assert output.read_text() == "existing audit evidence\n"


def recovery_record(
    module: Any, prefix: Path, continuation: Path, book: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    # The external frozen-build identity check is tested by the pipeline tests.
    # Keep the record validator and both real segment auditors in this test.
    identity = {
        "manifest_sha256": "frozen-fixture", "launcher_sha256": {"candidate": "c", "baseline": "b"},
        "book_sha256": hashlib.sha256(book.read_bytes()).hexdigest(),
    }
    monkeypatch.setattr(module, "recovery_identity_snapshot", lambda: identity, raising=False)
    record = tmp_path / "recovery-identity.json"
    record.write_text(json.dumps({
        "version": 1, "exit_code": 0,
        "before": {"identity": identity, "epoch": 10.0},
        "after": {"identity": identity, "epoch": 20.0},
        "checkpoint_sha256": hashlib.sha256((prefix / "autosave.json").read_bytes()).hexdigest(),
        "book_sha256": hashlib.sha256(book.read_bytes()).hexdigest(),
        "continuation_sha256": {
            name: hashlib.sha256((continuation / name).read_bytes()).hexdigest()
            for name in ("autosave.json", "console.log", "protocol.log", "games.pgn", "exit_code")
        },
    }))
    return record


def test_release_requires_recovery_identity_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, prefix, continuation, book = release_fixture(tmp_path, monkeypatch)
    output = tmp_path / "report.json"
    with pytest.raises(ValueError, match="identity record"):
        module.audit_release(prefix, continuation, book, output, 2, 4)
    assert not output.exists()


def test_explicit_historical_audit_cannot_report_release_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, prefix, continuation, book = release_fixture(tmp_path, monkeypatch)
    report = module.audit_release(
        prefix, continuation, book, tmp_path / "historical.json", 2, 4,
        historical_unbound=True,
    )
    assert report["status"] == "HOLD"
    assert report["identity_verdict"] == "HISTORICAL_UNBOUND"
    assert report["strength_verdict"]["status"] == "PASS"


@pytest.mark.parametrize("field", ["before", "after", "checkpoint_sha256", "continuation_sha256", "book_sha256"])
def test_release_rejects_unbound_recovery_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    module, prefix, continuation, book = release_fixture(tmp_path, monkeypatch)
    record = recovery_record(module, prefix, continuation, book, tmp_path, monkeypatch)
    data = json.loads(record.read_text())
    if field in ("before", "after"):
        data[field]["identity"] = {"wrong": "engine"}
    else:
        data[field] = "wrong-artifact"
    record.write_text(json.dumps(data))
    output = tmp_path / "report.json"
    with pytest.raises(ValueError, match="identity record"):
        module.audit_release(prefix, continuation, book, output, 2, 4, identity_record=record)
    assert not output.exists()


def test_release_rejects_recovery_record_changed_during_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, prefix, continuation, book = release_fixture(tmp_path, monkeypatch)
    record = recovery_record(module, prefix, continuation, book, tmp_path, monkeypatch)
    original = module.audit_protocol_segments

    def replace_record(*args: Any) -> Any:
        result = original(*args)
        data = json.loads(record.read_text())
        data["after"]["epoch"] = 30.0
        record.write_text(json.dumps(data))
        return result

    monkeypatch.setattr(module, "audit_protocol_segments", replace_record)
    with pytest.raises(ValueError, match="identity record"):
        module.audit_release(prefix, continuation, book, tmp_path / "report.json", 2, 4, identity_record=record)

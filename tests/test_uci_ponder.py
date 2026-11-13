"""UCI-process contract for task-local pondering."""

import time

import chess
import pytest

from uci_harness import UciSession


@pytest.fixture
def session():
    engine = UciSession()
    try:
        engine.handshake()
        yield engine
    finally:
        print(engine.transcript())
        engine.close()


def no_result(session: UciSession, start: int, seconds: float = 0.15) -> None:
    deadline = time.monotonic() + seconds
    with session.condition:
        while True:
            assert not any(channel == "stdout" and line.startswith("bestmove ")
                           for _, channel, line in session.events[start:]), list(session.events[start:])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            session.condition.wait(remaining)


def test_ponder_option_and_hit_after_depth_cap(session: UciSession) -> None:
    transcript = session.transcript()
    assert "option name Ponder type check default false" in transcript
    start_option = session.send("setoption name Ponder value true")
    session.wait(lambda line: line == "info string Ponder enabled", start_option)
    session.fence()
    session.ready()
    board = chess.Board()
    session.position(board)
    start = session.go(board, "go ponder depth 1 wtime 5000 btime 5000")
    session.wait(lambda line: line.startswith("info depth 1 "), start, 5)
    session.ready()
    no_result(session, start)
    session.send("ponderhit")
    session.bestmove(start)
    session.finish()


def test_stop_without_hit(session: UciSession) -> None:
    board = chess.Board()
    session.position(board)
    start = session.go(board, "go ponder depth 1")
    session.wait(lambda line: line.startswith("info depth 1 "), start, 5)
    no_result(session, start)
    session.stop()
    session.send("stop")
    session.finish()


def test_terminal_waits_for_hit(session: UciSession) -> None:
    board = chess.Board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1")
    session.position(board)
    start = session.go(board, "go ponder depth 1")
    no_result(session, start)
    assert session.bestmove(session.send("ponderhit")) == "bestmove 0000"
    session.finish()


def test_wrong_prediction_replacement_uses_new_position(session: UciSession) -> None:
    predicted = chess.Board()
    predicted.push_uci("e2e4")
    predicted.push_uci("e7e5")
    actual = chess.Board()
    actual.push_uci("e2e4")
    actual.push_uci("c7c5")
    session.position(predicted)
    first = session.go(predicted, "go ponder depth 1")
    no_result(session, first)
    session.stop()
    session.position(actual)
    second = session.go(actual, "go depth 1")
    session.bestmove(second)
    session.send("ponderhit")  # Stale hit must not affect a finished task.
    session.finish()


@pytest.mark.parametrize("ending", ["quit", "eof"])
def test_shutdown_while_pondering(session: UciSession, ending: str) -> None:
    board = chess.Board()
    session.position(board)
    start = session.go(board, "go ponder depth 1")
    no_result(session, start)
    session.finish(eof=ending == "eof")

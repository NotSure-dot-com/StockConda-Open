"""UCI time and depth limits compose; an exhausted budget is not untimed."""
import threading
import time

import chess
import pytest

from chess_engine.stockconda import UCIEngine
from uci_harness import UciSession


@pytest.fixture
def engine():
    value = UCIEngine()
    try:
        yield value
    finally:
        value.handle_quit()


def submitted_search(engine, monkeypatch, command):
    seen = []
    finished = threading.Event()

    def search(time_limit, depth_limit, infinite, board_fen, moves):
        seen.append((time_limit, depth_limit, infinite, board_fen, moves))
        finished.set()
        board = chess.Board(board_fen)
        for move in moves:
            board.push_uci(move)
        return next(iter(board.legal_moves)), 0

    monkeypatch.setattr(engine, 'execute_search_task', search)
    engine.handle_go(command.split())
    assert finished.wait(2), 'worker did not receive the go command'
    engine._search_worker.cancel_and_wait()
    assert len(seen) == 1
    return seen[0]


def test_depth_and_movetime_reach_same_worker_task(engine, monkeypatch):
    limit, depth, infinite, fen, moves = submitted_search(
        engine, monkeypatch, 'depth 20 movetime 1000'
    )
    assert (limit, depth, infinite) == (1.0, 20, False)
    assert chess.Board(fen).is_valid() and moves == ()


@pytest.mark.parametrize('side,command,expected_limit', [
    (chess.WHITE, 'depth 20 wtime 30000 btime 20000', 1.3),
    (chess.BLACK, 'depth 20 wtime 30000 btime 20000', 20 / 30 * 1.3),
])
def test_depth_and_game_clock_reach_same_worker_task(
    engine, monkeypatch, side, command, expected_limit
):
    if side == chess.BLACK:
        engine.board.push_uci('e2e4')
    limit, depth, infinite, fen, moves = submitted_search(engine, monkeypatch, command)
    assert limit == pytest.approx(expected_limit)
    assert (depth, infinite) == (20, False)
    board = chess.Board(fen)
    for move in moves:
        board.push_uci(move)
    assert board.turn == side


@pytest.mark.parametrize('command,expected', [
    ('depth 20 movetime 1000 wtime 30000 btime 20000', (1.0, 20, False)),
    ('depth 2', (None, 2, False)),
    ('movetime 1000', (1.0, None, False)),
    ('infinite', (None, None, True)),
])
def test_existing_single_limit_and_movetime_precedence(engine, monkeypatch, command, expected):
    assert submitted_search(engine, monkeypatch, command)[:3] == expected


@pytest.mark.parametrize('budget,expected', [
    (None, False), (0.0, True), (0.001, True),
])
def test_expired_search_budget_stops_at_first_check(engine, monkeypatch, budget, expected):
    monkeypatch.setattr(time, 'monotonic', lambda: 20.0)
    engine._search_start_time = 19.0
    engine._search_time_limit = budget
    engine._stop_check_counter = 0
    engine._stop_check_threshold = 1
    assert engine.stop_flag_check() is expected


@pytest.mark.parametrize('command', ['movetime 0', 'wtime 0 btime 0'])
def test_zero_uci_budget_remains_explicit(engine, monkeypatch, command):
    assert submitted_search(engine, monkeypatch, command)[:3] == (0.0, None, False)


@pytest.mark.parametrize('command', [
    'go depth 64 movetime 50',
    'go depth 64 wtime 1000 btime 1000',
    'go wtime 0 btime 0',
])
def test_uci_deadline_completes_without_external_stop(command):
    session = UciSession()
    try:
        session.handshake()
        board = chess.Board()
        session.position(board)
        start = session.go(board, command)
        session.bestmove(start)
        session.finish()
    finally:
        session.close()


def test_uci_zero_budget_keeps_worker_usable_and_terminal_move_legal():
    session = UciSession()
    try:
        session.handshake()
        board = chess.Board()
        session.position(board)
        session.bestmove(session.go(board, 'go wtime 0 btime 0'))
        session.bestmove(session.go(board, 'go depth 2'))
        terminal = chess.Board('7k/6Q1/5K2/8/8/8/8/8 b - - 0 1')
        session.position(terminal)
        assert session.bestmove(session.go(terminal, 'go movetime 0')) == 'bestmove 0000'
        session.finish()
    finally:
        session.close()

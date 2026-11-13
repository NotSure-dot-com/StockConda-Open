"""Ponder clock and search continuation without restarting the worker task."""

import threading

import chess

import chess_engine.stockconda as engine_module
import chess_engine.uci_search_worker as worker_module
from chess_engine.uci_search_worker import SearchTask


def test_clock_starts_at_hit_not_go(monkeypatch) -> None:
    engine = engine_module.UCIEngine()
    clock = [100.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: clock[0])
    task = SearchTask(1, chess.STARTING_FEN, 0.5, 1, False, ponder=True)
    engine._active_task = task
    engine.pvs_search = lambda *args, **kwargs: (10, chess.Move.from_uci("e2e4"))
    try:
        engine.execute_search_task(0.5, 1, False)
        assert engine._search_time_limit is None
        clock[0] = 105.0
        assert not engine.stop_flag_check()
        assert task.mark_hit()
        assert not task.mark_hit()
        assert not engine.stop_flag_check()
        assert engine._search_start_time == 105.0
        assert engine._search_time_limit == 0.5
        clock[0] = 105.4
        assert not any(engine.stop_flag_check() for _ in range(2048))
        clock[0] = 105.5
        assert any(engine.stop_flag_check() for _ in range(2048))
    finally:
        engine._active_task = None
        engine.handle_quit()


def test_info_time_remains_monotonic_across_hit(monkeypatch, capsys) -> None:
    engine = engine_module.UCIEngine()
    clock = [100.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: clock[0])
    task = SearchTask(1, chess.STARTING_FEN, 1.0, 2, False, ponder=True)
    engine._active_task = task

    def search(board, depth, *args, **kwargs):
        if depth == 1:
            clock[0] += 5.0
        else:
            assert task.mark_hit()
            assert not engine.stop_flag_check()
            clock[0] += 0.125
        return depth, chess.Move.from_uci("e2e4")

    engine.pvs_search = search
    try:
        engine.execute_search_task(1.0, 2, False)
        lines = [line for line in capsys.readouterr().out.splitlines()
                 if line.startswith("info depth ")]
        times = [int(line.split(" time ")[1].split()[0]) for line in lines]
        assert times == [5000, 5125]
    finally:
        engine._active_task = None
        engine.handle_quit()


def test_hit_continues_same_board_and_search(capsys) -> None:
    engine = engine_module.UCIEngine()
    entered = threading.Event()
    release = threading.Event()
    boards = []
    depths = []

    def search(board, depth, *args, **kwargs):
        boards.append(board)
        depths.append(depth)
        if depth == 2:
            entered.set()
            assert release.wait(3)
        return depth, chess.Move.from_uci("e2e4")

    engine.pvs_search = search
    try:
        engine.handle_go(["ponder", "depth", "2", "movetime", "500"])
        assert entered.wait(2)
        task = engine._active_task
        assert task is not None and task.ponder and task.time_limit == 0.5
        assert task.hit_at is None
        engine.handle_ponderhit()
        release.set()
        assert task.completed.wait(2)
        assert depths == [1, 2]
        assert boards[0] is boards[1]
        assert task.hit_at is not None
        lines = [line for line in capsys.readouterr().out.splitlines()
                 if line.startswith("bestmove ")]
        assert len(lines) == 1 and lines[0].split()[1] == "e2e4"
    finally:
        release.set()
        engine.handle_quit()


def test_early_result_waits_for_hit(capsys) -> None:
    engine = engine_module.UCIEngine()
    entered = threading.Event()

    def search(*args):
        entered.set()
        return chess.Move.from_uci("e2e4"), 0

    engine.execute_search_task = search
    try:
        engine.handle_go(["ponder", "depth", "1"])
        assert entered.wait(2)
        task = engine._active_task
        assert task is not None
        assert not task.completed.wait(0.1)
        assert "bestmove " not in capsys.readouterr().out
        engine.handle_ponderhit()
        assert task.completed.wait(2)
        lines = [line for line in capsys.readouterr().out.splitlines()
                 if line.startswith("bestmove ")]
        assert len(lines) == 1
    finally:
        engine.handle_quit()

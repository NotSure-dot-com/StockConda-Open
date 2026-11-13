"""The advertised MaxDepth default is the effective finite ceiling."""

import chess
import pytest

from chess_engine.stockconda import UCIEngine


@pytest.fixture
def engine():
    value = UCIEngine()
    try:
        yield value
    finally:
        value.handle_quit()


def _cheap_search(engine, monkeypatch):
    depths = []
    move = chess.Move.from_uci("e2e4")

    def search(_board, depth, _alpha, _beta, *_args, **_kwargs):
        depths.append(depth)
        return 0, move

    monkeypatch.setattr(engine, "pvs_search", search)
    monkeypatch.setattr(engine, "reconstruct_pv", lambda *_args, **_kwargs: [move])
    return depths


def test_default_64_is_advertised_and_used_by_worker(engine, monkeypatch, capsys):
    depths = _cheap_search(engine, monkeypatch)
    engine.handle_uci()
    assert "option name MaxDepth type spin default 64 min 1 max 64" in capsys.readouterr().out
    board = chess.Board()
    task = engine._search_worker.submit(board.fen(), None, None, False)
    assert task.completed.wait(5)
    assert engine._search_worker.failure is None
    assert depths == list(range(1, 65))
    assert capsys.readouterr().out.count("bestmove ") == 1


def test_default_64_is_used_by_direct_search(engine, monkeypatch):
    depths = _cheap_search(engine, monkeypatch)
    engine.start_search(None, None, False)
    assert depths == list(range(1, 65))


def test_smaller_limit_survives_new_game_and_remains_finite(engine, monkeypatch):
    depths = _cheap_search(engine, monkeypatch)
    engine.handle_setoption(["name", "MaxDepth", "value", "3"])
    engine.handle_ucinewgame()
    board = chess.Board()
    task = engine._search_worker.submit(board.fen(), None, None, False)
    assert task.completed.wait(5)
    assert depths == [1, 2, 3]
    depths.clear()
    engine.start_search(None, None, False)
    assert depths == [1, 2, 3]


def test_depth_64_tables_and_ply_63_are_addressable(engine):
    assert len(engine.lmr_reduction_table) > 64
    assert len(engine.history_factors["beta_cutoff"]) > 64
    assert len(engine.history_factors["alpha_improve"]) > 64
    assert len(engine.history_factors["regular_update"]) > 64
    assert len(engine.killer_moves) >= 64


def test_explicit_depth_cannot_exceed_precomputed_ceiling(engine, monkeypatch):
    depths = _cheap_search(engine, monkeypatch)
    board = chess.Board()
    task = engine._search_worker.submit(board.fen(), None, 65, False)
    assert task.completed.wait(5)
    assert engine._search_worker.failure is None
    assert depths == list(range(1, 65))
    depths.clear()
    engine.start_search(None, 65, False)
    assert depths == list(range(1, 65))

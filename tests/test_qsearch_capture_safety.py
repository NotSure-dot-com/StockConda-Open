"""The capture fast path must not admit pinned/king/EP self-checks."""
from collections.abc import Iterator
import random

import chess
import pytest

from chess_engine.cython_ext import cython_chess
from chess_engine.cython_ext.transposition import MATE_SCORE
from chess_engine.stockconda import UCIEngine


def selected(board: chess.Board, from_mask: int = chess.BB_ALL,
             to_mask: int = chess.BB_ALL) -> tuple[bool, bool, list[chess.Move]]:
    check, legal, moves = cython_chess.generate_qsearch_moves_with_status(
        board, from_mask, to_mask)
    return bool(check), bool(legal), list(moves)


class SafetyTrap(chess.Board):
    def _is_safe(self, king: int, blockers: int, move: chess.Move) -> bool:
        raise AssertionError("unneeded Python safety call for unpinned capture")


def test_unpinned_nonking_captures_need_no_python_safety_callback() -> None:
    board = SafetyTrap("4k3/8/8/8/2p1p3/3Q4/8/4K3 w - - 0 1")
    check, legal, moves = selected(board)
    assert (check, legal) == (False, True)
    assert [move.uci() for move in moves] == ["d3e4", "d3c4"]


@pytest.mark.parametrize("fen,forbidden,required", [
    ("k3r3/8/8/8/8/8/3nR3/4K3 w - - 0 1", "e2d2", "e2e8"),
    ("k2r4/8/8/8/8/8/3n4/4K3 w - - 0 1", "e1d2", None),
    ("7k/8/8/r4pPK/8/8/8/8 w - f6 0 1", "g5f6", None),
    ("7k/8/8/3pP3/8/8/8/K7 w - d6 0 1", None, "e5d6"),
])
def test_special_safety_checks_are_retained(
    fen: str, forbidden: str | None, required: str | None,
) -> None:
    board = chess.Board(fen)
    assert board.is_valid() and not board.is_check()
    before = board.fen()
    _, has_legal, moves = selected(board)
    assert has_legal == bool(list(board.legal_moves))
    assert set(moves) == set(board.generate_legal_captures())
    strings = [move.uci() for move in moves]
    if forbidden is not None:
        assert forbidden not in strings
    if required is not None:
        assert required in strings
    assert board.fen() == before and not board.move_stack


def positions() -> Iterator[chess.Board]:
    for fen in [
        chess.STARTING_FEN,
        "k3r3/8/8/8/8/8/8/4KB2 w - - 0 1",
        "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1",
        "7k/5K2/6Q1/8/8/8/8/8 b - - 0 1",
        "k6r/6P1/8/8/8/8/8/K7 w - - 0 1",
        "k7/6P1/8/8/8/8/8/K7 w - - 0 1",
        "4k3/8/8/8/2p1p3/3Q4/8/4K3 w - - 0 1",
        "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
    ]:
        yield chess.Board(fen)
    rng = random.Random(403)
    board = chess.Board()
    for _ in range(100):
        moves = list(board.legal_moves)
        if not moves:
            break
        board.push(rng.choice(moves))
        yield board.copy()


@pytest.mark.parametrize("restricted", [False, True])
def test_move_order_matches_unchanged_full_legal_generator(restricted: bool) -> None:
    # The full generator has a separate safety path: neither uses the new helper.
    for board in positions():
        source = chess.BB_RANK_1 | chess.BB_RANK_2 | chess.BB_RANK_5 if restricted else chess.BB_ALL
        target = chess.BB_LIGHT_SQUARES if restricted else chess.BB_ALL
        before = (board.fen(), list(board.move_stack))
        check, has_legal, moves = selected(board, source, target)
        legal = list(cython_chess.generate_legal_moves(board, source, target))
        expected = legal if board.is_check() else [
            move for move in legal
            if board.is_capture(move) or move.promotion is not None
        ]
        assert check == board.is_check()
        assert has_legal == bool(legal)
        assert moves == expected
        assert len(moves) == len(set(moves))
        assert (board.fen(), board.move_stack) == before


@pytest.mark.parametrize("exception", [False, True])
def test_qsearch_restores_board_after_child_cancellation_or_exception(exception: bool) -> None:
    engine = UCIEngine.__new__(UCIEngine)
    calls = 0

    def stop() -> bool:
        nonlocal calls
        calls += 1
        if calls == 2 and exception:
            raise RuntimeError("child interruption")
        return calls == 2

    engine.stop_flag_check = stop  # type: ignore[method-assign]
    board = chess.Board("4k3/8/8/8/2p1p3/3Q4/8/4K3 w - - 0 1")
    before = board.fen()
    if exception:
        with pytest.raises(RuntimeError, match="child interruption"):
            engine.quiescence_search(board, -MATE_SCORE, MATE_SCORE, 1)
    else:
        assert engine.quiescence_search(board, -MATE_SCORE, MATE_SCORE, 1) is None
    assert calls == 2
    assert board.fen() == before and not board.move_stack

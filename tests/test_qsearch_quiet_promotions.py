"""QSearch must treat every legal promotion as a noisy move."""

from collections.abc import Iterable

import chess
import pytest

from chess_engine.cython_ext import cython_chess
from chess_engine.cython_ext.evaluation import evaluate_board_static
from chess_engine.stockconda import MATE_SCORE, UCIEngine


def independent_noisy_moves(
    board: chess.Board,
    from_mask: int = chess.BB_ALL,
    to_mask: int = chess.BB_ALL,
) -> list[chess.Move]:
    """Select QSearch moves from python-chess legal moves, not production APIs."""
    return [
        move
        for move in board.legal_moves
        if chess.BB_SQUARES[move.from_square] & from_mask
        and chess.BB_SQUARES[move.to_square] & to_mask
        and (board.is_capture(move) or move.promotion is not None)
    ]


def selected(
    board: chess.Board,
    from_mask: int = chess.BB_ALL,
    to_mask: int = chess.BB_ALL,
) -> tuple[bool, bool, list[chess.Move]]:
    in_check, has_legal, moves = cython_chess.generate_qsearch_moves_with_status(
        board, from_mask, to_mask
    )
    return bool(in_check), bool(has_legal), list(moves)


@pytest.mark.parametrize(
    "fen",
    [
        "8/P6k/8/8/8/8/8/K7 w - - 0 1",
        "K7/8/8/8/8/8/p6k/8 b - - 0 1",
        "k6r/6P1/8/8/8/8/8/K7 w - - 0 1",
        "7k/P7/8/8/2p1p3/3Q4/8/K7 w - - 0 1",
    ],
)
def test_qsearch_noisy_moves_match_independent_legal_oracle(fen: str) -> None:
    board = chess.Board(fen)
    assert board.is_valid() and not board.is_check()
    before = board.fen()

    in_check, has_legal, moves = selected(board)

    assert not in_check
    assert has_legal == bool(list(board.legal_moves))
    assert moves == independent_noisy_moves(board)
    assert len(moves) == len(set(moves))
    assert board.fen() == before and not board.move_stack


def test_quiet_promotions_include_every_underpromotion_in_generator_order() -> None:
    board = chess.Board("8/P6k/8/8/8/8/8/K7 w - - 0 1")

    _, _, moves = selected(board)

    assert [move.uci() for move in moves] == [
        "a7a8q",
        "a7a8r",
        "a7a8b",
        "a7a8n",
    ]


def test_quiet_promotions_honor_source_and_target_masks() -> None:
    board = chess.Board("8/P6k/8/8/8/8/8/K7 w - - 0 1")
    a7 = chess.BB_SQUARES[chess.A7]
    a8 = chess.BB_SQUARES[chess.A8]

    assert selected(board, a7, a8)[2] == independent_noisy_moves(board, a7, a8)
    assert selected(board, chess.BB_SQUARES[chess.A1], a8)[2] == []
    assert selected(board, a7, chess.BB_SQUARES[chess.B8])[2] == []


def test_pinned_quiet_promotions_are_excluded_by_both_qsearch_apis() -> None:
    board = chess.Board("2b5/1P5k/K7/8/8/8/8/8 w - - 0 1")
    assert board.is_valid() and not board.is_check()
    expected = independent_noisy_moves(board)

    assert {move.uci() for move in expected} == {
        "b7c8q",
        "b7c8r",
        "b7c8b",
        "b7c8n",
    }
    assert not any(move.to_square == chess.B8 for move in expected)

    in_check, has_legal_move, status_moves = selected(board)
    lazy_in_check, lazy_moves = cython_chess.generate_qsearch_moves(
        board, chess.BB_ALL, chess.BB_ALL
    )

    assert not in_check and not lazy_in_check
    assert has_legal_move
    assert status_moves == expected
    assert list(lazy_moves) == expected


def reference_qsearch(
    board: chess.Board, alpha: int, beta: int, max_q_depth: int
) -> int:
    """Small independent move-selection oracle for a nonterminal test tree."""
    stand_pat = evaluate_board_static(board, board.turn)
    if board.is_check():
        moves: Iterable[chess.Move] = list(board.legal_moves)
    else:
        if stand_pat >= beta or max_q_depth <= 0:
            return stand_pat
        alpha = max(alpha, stand_pat)
        moves = independent_noisy_moves(board)

    for move in moves:
        board.push(move)
        try:
            score = -reference_qsearch(board, -beta, -alpha, max_q_depth - 1)
        finally:
            board.pop()
        if score >= beta:
            return score
        alpha = max(alpha, score)
    return alpha


def qsearch_engine() -> UCIEngine:
    engine = UCIEngine.__new__(UCIEngine)
    engine.stop_flag = False
    engine._stop_check_counter = 0
    engine._stop_check_threshold = 10**9
    engine._search_time_limit = None
    engine._search_start_time = 0.0
    return engine


def test_qsearch_evaluates_quiet_promotion_and_restores_board() -> None:
    board = chess.Board("8/P6k/8/8/8/8/8/K7 w - - 0 1")
    before = board.fen()
    expected = reference_qsearch(board.copy(), -MATE_SCORE, MATE_SCORE, 1)

    actual = qsearch_engine().quiescence_search(
        board, -MATE_SCORE, MATE_SCORE, max_q_depth=1
    )

    assert actual == expected
    assert board.fen() == before and not board.move_stack


def test_qsearch_depth_boundary_does_not_extend_quiet_promotion() -> None:
    board = chess.Board("8/P6k/8/8/8/8/8/K7 w - - 0 1")
    stand_pat = evaluate_board_static(board, board.turn)

    assert qsearch_engine().quiescence_search(
        board, -MATE_SCORE, MATE_SCORE, max_q_depth=0
    ) == stand_pat


def test_qsearch_stand_pat_cutoff_does_not_extend_quiet_promotion() -> None:
    board = chess.Board("8/P6k/8/8/8/8/8/K7 w - - 0 1")
    stand_pat = evaluate_board_static(board, board.turn)

    assert qsearch_engine().quiescence_search(
        board, -MATE_SCORE, stand_pat, max_q_depth=1
    ) == stand_pat


@pytest.mark.parametrize("raises", [False, True])
def test_quiet_promotion_restores_board_after_child_interruption(
    raises: bool,
) -> None:
    engine = UCIEngine.__new__(UCIEngine)
    calls = 0

    def stop() -> bool:
        nonlocal calls
        calls += 1
        if calls == 2 and raises:
            raise RuntimeError("promotion child interruption")
        return calls == 2

    engine.stop_flag_check = stop  # type: ignore[method-assign]
    board = chess.Board("8/P6k/8/8/8/8/8/K7 w - - 0 1")
    before = board.fen()

    if raises:
        with pytest.raises(RuntimeError, match="promotion child interruption"):
            engine.quiescence_search(board, -MATE_SCORE, MATE_SCORE, 1)
    else:
        assert engine.quiescence_search(board, -MATE_SCORE, MATE_SCORE, 1) is None

    assert calls == 2
    assert board.fen() == before and not board.move_stack


def test_capture_only_api_remains_capture_only() -> None:
    board = chess.Board("8/P6k/8/8/8/8/8/K7 w - - 0 1")

    assert list(
        cython_chess.generate_legal_captures(board, chess.BB_ALL, chess.BB_ALL)
    ) == []

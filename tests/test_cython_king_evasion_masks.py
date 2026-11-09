from __future__ import annotations

from collections.abc import Iterable

import chess
import pytest

from chess_engine.cython_ext import cython_chess


REPRO_FEN = (
    "r2qkbnr/1ppQ1pp1/4pB2/p2p3p/2P3bP/3PPP1R/"
    "PP1K2P1/RN3BN1 b kq - 0 11"
)


def oracle(
    board: chess.Board, from_mask: int, to_mask: int
) -> list[chess.Move]:
    return [
        move
        for move in board.generate_legal_moves()
        if chess.BB_SQUARES[move.from_square] & from_mask
        and chess.BB_SQUARES[move.to_square] & to_mask
    ]


def materialize(moves: Iterable[chess.Move] | None) -> list[chess.Move]:
    return list(moves or ())


def observed(
    board: chess.Board, from_mask: int, to_mask: int
) -> tuple[
    list[chess.Move],
    list[chess.Move],
    list[chess.Move],
    list[chess.Move],
    bool,
]:
    search_check, search_moves = cython_chess.generate_search_moves(
        board, from_mask, to_mask
    )
    legal_moves = list(
        cython_chess.generate_legal_moves(board, from_mask, to_mask)
    )
    status_check, has_legal_move, status_moves = (
        cython_chess.generate_qsearch_moves_with_status(
            board, from_mask, to_mask
        )
    )
    lazy_check, lazy_moves = cython_chess.generate_qsearch_moves(
        board, from_mask, to_mask
    )

    assert search_check and status_check and lazy_check
    return (
        materialize(search_moves),
        legal_moves,
        list(status_moves),
        materialize(lazy_moves),
        bool(has_legal_move),
    )


def assert_all_apis_match_oracle(
    board: chess.Board, from_mask: int, to_mask: int
) -> None:
    expected = oracle(board, from_mask, to_mask)
    before_fen = board.fen()
    before_stack = list(board.move_stack)

    search, legal, status, lazy, has_legal_move = observed(
        board, from_mask, to_mask
    )

    assert search == expected
    assert legal == expected
    assert status == expected
    assert lazy == expected
    assert has_legal_move is bool(expected)
    assert board.fen() == before_fen
    assert board.move_stack == before_stack


def checked_board(king: int) -> chess.Board:
    board = chess.Board.empty()
    board.turn = chess.BLACK
    board.set_piece_at(king, chess.Piece(chess.KING, chess.BLACK))
    board.set_piece_at(king - 8, chess.Piece(chess.ROOK, chess.WHITE))
    board.set_piece_at(chess.A1, chess.Piece(chess.KING, chess.WHITE))
    assert board.is_valid()
    assert board.is_check()
    return board


def test_e8_source_mask_includes_king_evasion() -> None:
    board = chess.Board(REPRO_FEN)
    assert board.is_valid() and board.is_check()

    assert [move.uci() for move in oracle(board, chess.BB_E8, chess.BB_D7)] == [
        "e8d7"
    ]
    assert_all_apis_match_oracle(board, chess.BB_E8, chess.BB_D7)


def test_e4_source_mask_does_not_alias_e8() -> None:
    board = chess.Board(REPRO_FEN)
    assert board.is_valid() and board.is_check()

    assert oracle(board, chess.BB_E4, chess.BB_D7) == []
    assert_all_apis_match_oracle(board, chess.BB_E4, chess.BB_D7)


@pytest.mark.parametrize("king", range(32, 64))
def test_high_king_source_masks_match_python_chess(king: int) -> None:
    board = checked_board(king)
    source_mask = chess.BB_SQUARES[king]
    expected = oracle(board, source_mask, chess.BB_ALL)
    assert expected

    assert_all_apis_match_oracle(board, source_mask, chess.BB_ALL)


def test_bb_all_control_matches_python_chess_for_high_kings() -> None:
    for king in range(32, 64):
        board = checked_board(king)
        assert_all_apis_match_oracle(board, chess.BB_ALL, chess.BB_ALL)


def test_checked_generation_preserves_nonempty_move_history() -> None:
    board = chess.Board()
    for uci in ("e2e4", "e7e5", "f1c4", "b8c6", "d1h5", "g8f6", "h5f7"):
        board.push_uci(uci)

    assert board.is_check()
    assert len(board.move_stack) == 7
    assert_all_apis_match_oracle(board, chess.BB_E8, chess.BB_ALL)

"""Static evaluation keeps mask semantics while avoiding standard-Board callbacks."""

import random

import chess
import numpy as np
import pytest

from chess_engine.cython_ext.evaluation import (
    evaluate_board_from_bitboards,
    evaluate_board_static,
)


def _board_after(*moves: str) -> chess.Board:
    board = chess.Board()
    for move in moves:
        board.push_san(move)
    return board


def _positions() -> list[chess.Board]:
    return [
        chess.Board(),
        chess.Board(
            "r3k2r/p1ppqpb1/bn2pnp1/2pP4/1p2P3/2N2N2/PPQBBPPP/R3K2R w KQkq - 0 1"
        ),
        _board_after("e4", "a6", "e5", "d5"),
        chess.Board("4k3/P7/8/8/8/8/7p/4K3 w - - 0 1"),
        chess.Board("8/8/8/4k3/8/8/4K3/3R4 w - - 0 1"),
        _board_after("e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6", "Nc3", "a6"),
    ]


def _oracle(board: chess.Board, white_to_move: bool) -> int:
    """Use the separate native bitboard entry point and public mask contract."""
    white = np.array(
        [board.pieces_mask(piece_type, chess.WHITE) for piece_type in chess.PIECE_TYPES],
        dtype=np.uint64,
    )
    black = np.array(
        [board.pieces_mask(piece_type, chess.BLACK) for piece_type in chess.PIECE_TYPES],
        dtype=np.uint64,
    )
    return evaluate_board_from_bitboards(white, black, white_to_move)[0]


@pytest.mark.parametrize("white_to_move", [False, True])
def test_standard_positions_match_independent_bitboard_entry(white_to_move: bool) -> None:
    for board in _positions():
        assert board.is_valid(), board.fen()
        assert evaluate_board_static(board, white_to_move) == _oracle(board, white_to_move)


def test_deterministic_legal_positions_match_independent_bitboard_entry() -> None:
    rng = random.Random(20260927)
    board = chess.Board()
    for _ in range(48):
        moves = list(board.legal_moves)
        if not moves:
            break
        board.push(rng.choice(moves))
        for white_to_move in (False, True):
            assert evaluate_board_static(board, white_to_move) == _oracle(board, white_to_move)


def test_exact_standard_board_needs_no_pieces_mask_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    board = _positions()[1]
    expected = _oracle(board, board.turn)
    original = chess.Board.pieces_mask
    calls: list[tuple[int, bool]] = []

    def track(self: chess.Board, piece_type: int, color: bool) -> int:
        calls.append((piece_type, color))
        return original(self, piece_type, color)

    monkeypatch.setattr(chess.Board, "pieces_mask", track)
    assert evaluate_board_static(board, board.turn) == expected
    assert calls == []


def test_subclass_override_keeps_public_mask_semantics() -> None:
    class OverrideMaskBoard(chess.Board):
        def pieces_mask(self, piece_type: int, color: bool) -> int:
            if piece_type == chess.PAWN and color == chess.WHITE:
                return 0
            return super().pieces_mask(piece_type, color)

    fen = "4k3/8/8/8/4P3/8/8/4K3 w - - 0 1"
    board = OverrideMaskBoard(fen)
    assert board.is_valid()
    for white_to_move in (False, True):
        assert evaluate_board_static(board, white_to_move) == _oracle(board, white_to_move)
        assert evaluate_board_static(board, white_to_move) != evaluate_board_static(
            chess.Board(fen), white_to_move
        )

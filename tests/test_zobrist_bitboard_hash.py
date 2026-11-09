"""Zobrist hashing must preserve the old piece-map contract bit for bit."""
from __future__ import annotations

import random

import chess
import pytest

from chess_engine.cython_ext import transposition as tt


def legacy_zobrist_hash(board: chess.Board) -> int:
    """Independent reconstruction of the pre-optimization implementation."""
    generator = random.Random(42)
    piece_keys = [[
        [generator.getrandbits(64) for _color in (False, True)]
        for _piece_type in range(1, 7)
    ] for _square in range(64)]
    castling = [generator.getrandbits(64) for _ in range(4)]
    en_passant = [generator.getrandbits(64) for _ in range(8)]
    side = generator.getrandbits(64)

    value = 0
    for square, piece in board.piece_map().items():
        value ^= piece_keys[square][piece.piece_type - 1][int(piece.color)]
    if board.has_kingside_castling_rights(chess.WHITE):
        value ^= castling[0]
    if board.has_queenside_castling_rights(chess.WHITE):
        value ^= castling[1]
    if board.has_kingside_castling_rights(chess.BLACK):
        value ^= castling[2]
    if board.has_queenside_castling_rights(chess.BLACK):
        value ^= castling[3]
    if board.ep_square is not None:
        value ^= en_passant[chess.square_file(board.ep_square)]
    if board.turn == chess.BLACK:
        value ^= side
    return value


class PieceMapTrapBoard(chess.Board):
    def piece_map(self, *, mask: chess.Bitboard = chess.BB_ALL) -> dict[chess.Square, chess.Piece]:
        raise AssertionError("zobrist_hash must not materialize board.piece_map()")


@pytest.fixture(autouse=True)
def initialized_zobrist() -> None:
    tt.init_zobrist()


@pytest.mark.parametrize("fen", [
    chess.STARTING_FEN,
    "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
    "r3k2r/8/8/8/8/8/8/R3K2R b Kq - 99 45",
    "7k/8/8/3pP3/8/8/8/K7 w - d6 0 1",
    "7k/8/8/8/8/8/8/K7 b - e3 17 90",
    "k6r/6P1/8/8/8/8/8/K7 w - - 0 1",
    "7k/P7/8/8/8/8/8/7K b - - 0 1",
    "7k/6Q1/6K1/8/8/8/8/8 b - - 100 1",
    "7k/5K2/6Q1/8/8/8/8/8 b - - 0 1",
    "8/8/8/8/8/8/8/8 w - - 0 1",
])
def test_hash_matches_legacy_piece_map_rule(fen: str) -> None:
    board = chess.Board(fen)
    assert tt.zobrist_hash(board) == legacy_zobrist_hash(board)


def test_hash_does_not_materialize_piece_map() -> None:
    fen = "r3k2r/8/8/3pP3/8/8/8/R3K2R w KQkq d6 0 1"
    expected = legacy_zobrist_hash(chess.Board(fen))
    assert tt.zobrist_hash(PieceMapTrapBoard(fen)) == expected


def test_hash_matches_legacy_rule_through_deterministic_push_and_pop() -> None:
    board = chess.Board()
    generator = random.Random(404)
    keys = [(board.fen(), tt.zobrist_hash(board))]
    for _ in range(120):
        moves = list(board.legal_moves)
        if not moves:
            break
        board.push(generator.choice(moves))
        assert tt.zobrist_hash(board) == legacy_zobrist_hash(board)
        keys.append((board.fen(), tt.zobrist_hash(board)))

    while board.move_stack:
        board.pop()
        keys.pop()
        assert board.fen() == keys[-1][0]
        assert tt.zobrist_hash(board) == keys[-1][1]
        assert tt.zobrist_hash(board) == legacy_zobrist_hash(board)


@pytest.mark.parametrize("fen,move", [
    ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"),
    ("7k/8/8/3pP3/8/8/8/K7 w - d6 0 1", "e5d6"),
    ("k6r/6P1/8/8/8/8/8/K7 w - - 0 1", "g7h8n"),
])
def test_special_move_push_pop_restores_exact_hash(fen: str, move: str) -> None:
    board = chess.Board(fen)
    before = tt.zobrist_hash(board)
    selected = chess.Move.from_uci(move)
    assert selected in board.legal_moves
    board.push(selected)
    assert tt.zobrist_hash(board) == legacy_zobrist_hash(board)
    board.pop()
    assert tt.zobrist_hash(board) == before


def test_halfmove_and_fullmove_counters_remain_outside_position_hash() -> None:
    first = chess.Board("7k/8/8/8/8/8/8/KR6 w - - 0 1")
    second = chess.Board("7k/8/8/8/8/8/8/KR6 w - - 99 200")
    assert tt.zobrist_hash(first) == tt.zobrist_hash(second)

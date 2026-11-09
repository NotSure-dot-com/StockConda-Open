from collections import Counter

import chess
import pytest

from chess_engine.cython_ext import cython_chess
from chess_engine.stockconda import MATE_SCORE, UCIEngine


def qsearch_engine() -> UCIEngine:
    engine = UCIEngine.__new__(UCIEngine)
    engine.stop_flag = False
    engine._stop_check_counter = 0
    engine._stop_check_threshold = 10**9
    engine._search_time_limit = None
    engine._search_start_time = 0.0
    return engine


def qsearch_status(board: chess.Board):
    generator = getattr(cython_chess, "generate_qsearch_moves_with_status", None)
    assert generator is not None, (
        "cython_chess.generate_qsearch_moves_with_status() is required"
    )
    return generator(board, chess.BB_ALL, chess.BB_ALL)


@pytest.mark.parametrize(
    "fen",
    [
        chess.STARTING_FEN,
        "k3r3/8/8/8/8/8/8/4KB2 w - - 0 1",
    ],
)
def test_search_metadata_matches_python_legal_moves_and_check_status(fen):
    board = chess.Board(fen)

    in_check, moves = cython_chess.generate_search_moves(
        board, chess.BB_ALL, chess.BB_ALL
    )

    assert in_check == board.is_check()
    assert list(moves) == list(board.legal_moves)


def test_qsearch_status_distinguishes_quiet_legal_position_from_stalemate():
    quiet = chess.Board()
    in_check, has_legal_move, moves = qsearch_status(quiet)
    assert not in_check
    assert has_legal_move
    assert list(moves) == []

    stalemate = chess.Board("7k/5K2/6Q1/8/8/8/8/8 b - - 0 1")
    in_check, has_legal_move, moves = qsearch_status(stalemate)
    assert not in_check
    assert not has_legal_move
    assert list(moves) == []


def test_qsearch_status_preserves_checkmate_and_en_passant_moves():
    checkmate = chess.Board("7k/6Q1/6K1/8/8/8/8/8 b - - 0 1")
    in_check, has_legal_move, moves = qsearch_status(checkmate)
    assert in_check
    assert not has_legal_move
    assert list(moves) == []

    en_passant = chess.Board("7k/8/8/3pP3/8/8/8/K7 w - d6 0 1")
    in_check, has_legal_move, moves = qsearch_status(en_passant)
    assert not in_check
    assert has_legal_move
    assert [move.uci() for move in moves] == ["e5d6"]

    promotion_capture = chess.Board("k6r/6P1/8/8/8/8/8/K7 w - - 0 1")
    in_check, has_legal_move, moves = qsearch_status(promotion_capture)
    assert not in_check
    assert has_legal_move
    expected = [
        move
        for move in promotion_capture.legal_moves
        if promotion_capture.is_capture(move) or move.promotion is not None
    ]
    assert list(moves) == expected
    assert sum(promotion_capture.is_capture(move) for move in moves) == 4
    assert sum(not promotion_capture.is_capture(move) for move in moves) == 4
    assert {move.promotion for move in moves} == {
        chess.QUEEN,
        chess.ROOK,
        chess.BISHOP,
        chess.KNIGHT,
    }


class SafetyCountingBoard(chess.Board):
    def __init__(self, fen: str | None = chess.STARTING_FEN) -> None:
        super().__init__(fen)
        self.safety_queries = 0

    def _is_safe(
        self,
        king: chess.Square,
        blockers: chess.Bitboard,
        move: chess.Move,
    ) -> bool:
        self.safety_queries += 1
        return super()._is_safe(king, blockers, move)


class SafetyTrapBoard(chess.Board):
    def _is_safe(
        self,
        king: chess.Square,
        blockers: chess.Bitboard,
        move: chess.Move,
    ) -> bool:
        raise AssertionError("settled rule draw generated a legal move")


def test_qsearch_uses_one_legal_move_probe_at_quiet_leaf(monkeypatch):
    def duplicate_generation(*_args, **_kwargs):
        raise AssertionError("QSearch repeated legal move generation")

    monkeypatch.setattr(cython_chess, "generate_legal_moves", duplicate_generation)
    board = SafetyCountingBoard()

    score = qsearch_engine().quiescence_search(
        board, -MATE_SCORE, MATE_SCORE, max_q_depth=0
    )

    assert isinstance(score, int)
    assert board.safety_queries == 1


def test_qsearch_rule_draw_without_check_skips_legal_move_probe():
    board = SafetyTrapBoard("7k/8/8/8/8/8/8/KR6 w - - 100 51")

    assert not board.is_check()
    assert qsearch_engine().quiescence_search(
        board, -MATE_SCORE, MATE_SCORE, max_q_depth=1
    ) == 0


class CheckTrapBoard(chess.Board):
    def is_check(self) -> bool:
        raise AssertionError("PVS repeated the Cython check calculation")


class CaptureCountingBoard(chess.Board):
    def __init__(self, fen: str | None = chess.STARTING_FEN) -> None:
        super().__init__(fen)
        self.capture_queries: Counter[chess.Move] = Counter()

    def is_capture(self, move: chess.Move) -> bool:
        self.capture_queries[move] += 1
        return super().is_capture(move)


@pytest.fixture
def engine():
    instance = UCIEngine()
    try:
        yield instance
    finally:
        instance.handle_quit()


def test_pvs_reuses_check_status_from_legal_move_generation(engine, monkeypatch):
    def duplicate_check(*_args, **_kwargs):
        raise AssertionError("PVS repeated the Cython check calculation")

    monkeypatch.setattr(cython_chess, "is_check", duplicate_check)
    board = CheckTrapBoard()
    before = board.fen()

    score, move = engine.pvs_search(board, 1, -MATE_SCORE, MATE_SCORE)

    assert isinstance(score, int)
    assert move in chess.Board(before).legal_moves
    assert board.fen() == before


@pytest.mark.parametrize(
    "fen",
    [
        chess.STARTING_FEN,
        "4k3/4p3/8/8/1B6/8/8/K3R3 w - - 0 1",
        "7k/8/8/3pP3/8/8/8/K7 w - d6 0 1",
    ],
)
def test_pvs_classifies_each_root_move_capture_once(engine, fen):
    board = CaptureCountingBoard(fen)
    legal_moves = set(board.legal_moves)
    assert any(board.is_capture(move) for move in legal_moves) == (
        fen != chess.STARTING_FEN
    )
    board.capture_queries.clear()

    score, move = engine.pvs_search(board, 1, -MATE_SCORE, MATE_SCORE)

    assert isinstance(score, int)
    assert move in legal_moves
    assert set(board.capture_queries) == legal_moves
    assert set(board.capture_queries.values()) == {1}

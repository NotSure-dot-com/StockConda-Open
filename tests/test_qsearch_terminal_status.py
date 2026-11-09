import chess

from chess_engine.cython_ext.evaluation import evaluate_board, evaluate_board_static
from chess_engine.stockconda import MATE_SCORE, UCIEngine


def qsearch_engine() -> UCIEngine:
    engine = UCIEngine.__new__(UCIEngine)
    engine.stop_flag = False
    engine._stop_check_counter = 0
    engine._stop_check_threshold = 10**9
    engine._search_time_limit = None
    engine._search_start_time = 0.0
    return engine


class TerminalMethodTrapBoard(chess.Board):
    def is_checkmate(self) -> bool:
        raise AssertionError("QSearch must not call board.is_checkmate()")

    def is_stalemate(self) -> bool:
        raise AssertionError("QSearch must not call board.is_stalemate()")

    def is_fifty_moves(self) -> bool:
        raise AssertionError("QSearch must not call board.is_fifty_moves()")


class StaticEvaluationTrapBoard(chess.Board):
    def pieces_mask(self, piece_type: chess.PieceType, color: chess.Color) -> int:
        raise AssertionError("terminal draw must return before static evaluation")


def test_qsearch_uses_shared_terminal_status_instead_of_python_terminal_wrappers():
    board = TerminalMethodTrapBoard()

    assert qsearch_engine().quiescence_search(
        board, -MATE_SCORE, -MATE_SCORE + 1, max_q_depth=0
    ) > -MATE_SCORE + 1


def test_qsearch_keeps_mate_precedence_over_fifty_move_clock():
    board = chess.Board("7k/6Q1/6K1/8/8/8/8/8 b - - 100 1")

    assert board.is_checkmate()
    assert qsearch_engine().quiescence_search(
        board, -MATE_SCORE, MATE_SCORE, max_q_depth=0
    ) == -MATE_SCORE + board.ply()


def test_qsearch_keeps_stalemate_as_draw():
    board = chess.Board("7k/5K2/6Q1/8/8/8/8/8 b - - 0 1")

    assert board.is_stalemate()
    assert qsearch_engine().quiescence_search(
        board, -MATE_SCORE, MATE_SCORE, max_q_depth=0
    ) == 0


def test_qsearch_keeps_fifty_move_draw_when_legal_move_exists():
    board = chess.Board("7k/8/8/8/8/8/8/KR6 w - - 100 51")

    assert board.is_fifty_moves()
    assert qsearch_engine().quiescence_search(
        board, -MATE_SCORE, MATE_SCORE, max_q_depth=0
    ) == 0


def test_qsearch_keeps_fifty_move_draw_while_in_check_with_escape():
    board = chess.Board("k3r3/8/8/8/8/8/8/4K3 w - - 100 51")

    assert board.is_check()
    assert board.is_fifty_moves()
    assert qsearch_engine().quiescence_search(
        board, -MATE_SCORE, MATE_SCORE, max_q_depth=0
    ) == 0


def test_qsearch_short_circuits_rule_draw_before_static_evaluation():
    board = StaticEvaluationTrapBoard("1r5k/8/8/8/8/8/8/KR6 w - - 100 51")

    assert any(board.generate_legal_captures())
    assert qsearch_engine().quiescence_search(
        board, -MATE_SCORE, MATE_SCORE, max_q_depth=1
    ) == 0


def test_qsearch_short_circuits_repetition_before_static_evaluation():
    board = StaticEvaluationTrapBoard()
    for move in ("g1f3", "g8f6", "f3g1", "f6g8") * 2:
        board.push_uci(move)

    assert board.is_repetition(3)
    assert qsearch_engine().quiescence_search(
        board, -MATE_SCORE, MATE_SCORE, max_q_depth=1
    ) == 0


def test_static_evaluation_matches_public_nonterminal_score():
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")

    score, terminal_draw = evaluate_board(board, board.turn)

    assert not terminal_draw
    assert evaluate_board_static(board, board.turn) == score

import unittest

import chess

from chess_engine.stockconda import UCIEngine, evaluate_board


def reference_qsearch(board, alpha, beta, max_q_depth):
    """Move-selection oracle sharing the production evaluator, not move generation."""
    stand_pat, terminal_draw = evaluate_board(board, board.turn)
    if terminal_draw:
        return stand_pat

    in_check = board.is_check()
    if in_check:
        moves = list(board.legal_moves)
    else:
        if stand_pat >= beta:
            return stand_pat
        if max_q_depth <= 0:
            return stand_pat
        alpha = max(alpha, stand_pat)
        moves = [
            move
            for move in board.legal_moves
            if board.is_capture(move) or move.promotion is not None
        ]

    if in_check and not moves:
        return stand_pat

    for move in moves:
        board.push(move)
        score = -reference_qsearch(board, -beta, -alpha, max_q_depth - 1)
        board.pop()
        if score >= beta:
            return score
        alpha = max(alpha, score)
    return alpha


class QSearchCheckEvasionTests(unittest.TestCase):
    def setUp(self):
        # QSearch only needs the stop-check state and class-level piece values.
        # Bypass the full UCI constructor so the tests do not start worker threads.
        self.engine = UCIEngine.__new__(UCIEngine)
        self.engine.stop_flag = False
        self.engine._stop_check_counter = 0
        self.engine._stop_check_threshold = 10**9
        self.engine._search_time_limit = None
        self.engine._search_start_time = 0.0

    def test_resolves_quiet_interposition_at_qsearch_depth_limit(self):
        board = chess.Board("k3r3/8/8/8/8/8/8/4KB2 w - - 0 1")

        self.assertTrue(board.is_check())
        self.assertIn(chess.Move.from_uci("f1e2"), board.legal_moves)
        expected = reference_qsearch(board.copy(), -10_000, 10_000, max_q_depth=0)
        self.assertEqual(
            self.engine.quiescence_search(board, -10_000, 10_000, max_q_depth=0),
            expected,
        )

    def test_resolves_quiet_king_evasion_at_qsearch_depth_limit(self):
        board = chess.Board("k3r3/8/8/8/8/8/8/4K3 w - - 0 1")

        self.assertTrue(board.is_check())
        expected = reference_qsearch(board.copy(), -10_000, 10_000, max_q_depth=0)
        self.assertEqual(
            self.engine.quiescence_search(board, -10_000, 10_000, max_q_depth=0),
            expected,
        )

    def test_resolves_check_created_by_qsearch_capture(self):
        board = chess.Board("4k3/4p3/8/8/1B6/8/8/K3R3 w - - 0 1")

        checking_capture = chess.Move.from_uci("e1e7")
        self.assertIn(checking_capture, board.legal_moves)
        self.assertTrue(board.is_capture(checking_capture))
        self.assertTrue(board.gives_check(checking_capture))
        expected = reference_qsearch(board.copy(), -10_000, 10_000, max_q_depth=1)
        self.assertEqual(
            self.engine.quiescence_search(board, -10_000, 10_000, max_q_depth=1),
            expected,
        )


if __name__ == "__main__":
    unittest.main()

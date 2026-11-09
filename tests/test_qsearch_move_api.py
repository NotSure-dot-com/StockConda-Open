import unittest

import chess

from chess_engine.cython_ext import cython_chess


class QSearchMoveApiTests(unittest.TestCase):
    def qsearch_moves(self, board):
        generator = getattr(cython_chess, "generate_qsearch_moves", None)
        self.assertIsNotNone(
            generator,
            "cython_chess.generate_qsearch_moves() is required",
        )
        return generator(board, chess.BB_ALL, chess.BB_ALL)

    def test_returns_all_legal_evasions_when_in_check(self):
        board = chess.Board("k3r3/8/8/8/8/8/8/4KB2 w - - 0 1")

        in_check, moves = self.qsearch_moves(board)

        self.assertTrue(in_check)
        moves = list(moves)
        self.assertEqual(len(moves), len(set(moves)), 'Duplicate evasions')
        self.assertEqual(set(moves), set(board.legal_moves))
        self.assertEqual(
            {move.uci() for move in moves},
            {"e1f2", "e1d2", "e1d1", "f1e2"},
        )

    def test_returns_noisy_moves_when_not_in_check(self):
        board = chess.Board("4k3/4p3/8/8/1B6/8/8/K3R3 w - - 0 1")

        in_check, moves = self.qsearch_moves(board)

        self.assertFalse(in_check)
        moves = list(moves)
        self.assertEqual(len(moves), len(set(moves)), 'Duplicate captures')
        self.assertEqual(set(moves), set(board.generate_legal_captures()))
        self.assertEqual(
            {move.uci() for move in moves},
            {"b4e7", "e1e7"},
        )

    def test_returns_quiet_promotions_without_changing_capture_api(self):
        board = chess.Board("8/P6k/8/8/8/8/8/K7 w - - 0 1")

        in_check, moves = self.qsearch_moves(board)

        self.assertFalse(in_check)
        self.assertEqual(
            [move.uci() for move in moves],
            ["a7a8q", "a7a8r", "a7a8b", "a7a8n"],
        )
        self.assertEqual(
            list(cython_chess.generate_legal_captures(
                board, chess.BB_ALL, chess.BB_ALL
            )),
            [],
        )

    def test_can_skip_unused_captures_but_not_check_detection(self):
        board = chess.Board()
        generator = getattr(cython_chess, "generate_qsearch_moves", None)
        self.assertIsNotNone(generator)

        try:
            in_check, moves = generator(
                board, chess.BB_ALL, chess.BB_ALL, search_captures=False
            )
        except TypeError as exc:
            self.fail(f"QSearch move API cannot skip unused captures: {exc}")

        self.assertFalse(in_check)
        self.assertIsNone(moves)


if __name__ == "__main__":
    unittest.main()

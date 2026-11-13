"""Verify mate distances and ordinary scores on the UCI wire."""

import chess
import pytest

from uci_harness import UciSession


@pytest.mark.parametrize(
    ('fen', 'depth', 'expected'),
    [
        ('7k/5Q2/6K1/8/8/8/8/8 w - - 0 1', 2, 'mate 1'),
        ('7k/5Q2/6K1/8/8/8/8/8 w - - 0 42', 2, 'mate 1'),
        ('8/8/8/Q7/6k1/8/6K1/8 w - - 0 1', 5, 'mate 3'),
        ('7k/8/5K2/8/8/8/8/6Q1 b - - 0 42', 2, 'mate -1'),
        (chess.STARTING_FEN, 2, 'cp'),
    ],
)
def test_uci_reports_root_score(fen: str, depth: int, expected: str) -> None:
    session = UciSession()
    try:
        session.handshake()
        session.send('setoption name Book value false')
        session.fence()
        session.ready()
        board = chess.Board(fen)
        session.position(board)
        start = session.go(board, f'go depth {depth}')
        info = session.wait(lambda line: line.startswith(f'info depth {depth} '), start, 5)
        session.bestmove(start)
        session.finish()
        score = info.split(' score ', 1)[1].split()[:2]
        if expected == 'cp':
            assert score[0] == 'cp', info
            int(score[1])
        else:
            assert score == expected.split(), info
    finally:
        session.close()

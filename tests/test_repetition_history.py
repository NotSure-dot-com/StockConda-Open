"""History handoff regressions, not a new draw policy or a TT/GHI solution.

The paired legal histories end at exactly the same FEN (including clock 7).
Only the first permits the defending king to return to h8/h1 for a third
occurrence. Removing history at either handoff must lose that draw escape.
"""
import re
import threading

import chess
import pytest

from chess_engine.stockconda import UCIEngine
from uci_harness import UciSession


CASES = [
    pytest.param(
        '7k/8/8/8/8/8/8/KR6 w - - 0 1',
        'b1b2 h8g8 b2b1 g8h8 b1b2 h8g8 b2b1',
        'b1b3 h8h7 b3b2 h7h8 b2b4 h8g8 b4b1',
        '6k1/8/8/8/8/8/8/KR6 b - - 7 4', 'g8h8', id='black'),
    pytest.param(
        'kr6/8/8/8/8/8/8/7K b - - 0 1',
        'b8b7 h1g1 b7b8 g1h1 b8b7 h1g1 b7b8',
        'b8b6 h1h2 b6b7 h2h1 b7b5 h1g1 b5b8',
        'kr6/8/8/8/8/8/8/6K1 w - - 7 5', 'g1h1', id='white'),
]


def played(fen, moves):
    board = chess.Board(fen)
    assert board.is_valid()
    for move in moves.split():
        board.push_uci(move)  # Reject an illegal fixture rather than testing it.
    return board


@pytest.fixture
def engine():
    value = UCIEngine()
    try:
        yield value
    finally:
        value.handle_quit()


@pytest.fixture
def session():
    value = UciSession()
    try:
        value.handshake()
        yield value
    finally:
        print(value.transcript())
        value.close()


def search(session, command, board, depth):
    session.send(command)
    start = session.go(board, f'go depth {depth}')
    # Fixed-depth correctness probe; this is not a latency measurement.
    result = session.wait(lambda s: s.startswith('bestmove '), start, 15)
    with session.condition:
        lines = [line for _, channel, line in session.events[start:]
                 if channel == 'stdout']
    scores = [re.search(r'\bscore cp (-?\d+)\b', line)
              for line in lines if line.startswith(f'info depth {depth} ')]
    assert len(scores) == 1 and scores[0] is not None, session.transcript()
    assert not any('error' in line.lower() or 'fallback' in line.lower()
                   for line in lines), session.transcript()
    return int(scores[0].group(1)), result.split()[1]


@pytest.mark.parametrize('fen,moves,control,final,escape', CASES)
@pytest.mark.parametrize('depth', [3, 5])
@pytest.mark.parametrize('warm', [False, True], ids=['cold', 'prior-control-search'])
def test_uci_history_preserves_threefold_escape(
        session, fen, moves, control, final, escape, depth, warm):
    board, other = played(fen, moves), played(fen, control)
    assert board.fen() == other.fen() == final
    assert board.is_repetition(2) and not board.is_repetition(3)
    assert not other.can_claim_threefold_repetition()
    after = board.copy()
    after.push_uci(escape)
    assert after.is_repetition(3)
    if warm:
        score, _ = search(session, f'position fen {fen} moves {control}', other, depth)
        assert score < 0
    score, move = search(session, f'position fen {fen} moves {moves}', board, depth)
    assert (score, move) == (0, escape), session.transcript()
    session.finish()


@pytest.mark.parametrize('fen,moves,control,final,escape', CASES)
@pytest.mark.parametrize('source', ['control-history', 'bare-fen'])
def test_uci_does_not_invent_an_unprovided_repetition(
        session, fen, moves, control, final, escape, source):
    if source == 'bare-fen':
        board, command = chess.Board(final), f'position fen {final}'
    else:
        board = played(fen, control)
        command = f'position fen {fen} moves {control}'
    score, _ = search(session, command, board, 3)
    assert score < 0, session.transcript()
    session.finish()


@pytest.mark.parametrize('fen,moves,control,final,escape', CASES)
@pytest.mark.parametrize('depth', [3, 5])
def test_direct_search_default_preserves_history_and_caller_board(
        engine, fen, moves, control, final, escape, depth):
    engine.board = played(fen, moves)
    before = engine.board.fen(), tuple(move.uci() for move in engine.board.move_stack)
    move, score = engine.execute_search_task(None, depth, False)
    assert (score, move) == (0, chess.Move.from_uci(escape))
    assert (engine.board.fen(), tuple(move.uci() for move in engine.board.move_stack)) == before


@pytest.mark.parametrize('params,expected_moves', [
    ('startpos moves g1f3 g8f6 f3g1 f6g8 g1f3 g8f6 f3g1',
     'g1f3 g8f6 f3g1 f6g8 g1f3 g8f6 f3g1'),
    ('fen r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1 moves e1g1 e8c8', 'e1g1 e8c8'),
    ('startpos moves e2e4 a7a6 e4e5 d7d5 e5d6', 'e2e4 a7a6 e4e5 d7d5 e5d6'),
    ('fen 7k/P7/8/8/8/8/8/7K w - - 0 1 moves a7a8n', 'a7a8n'),
], ids=['startpos-repetition', 'castling', 'en-passant', 'underpromotion'])
def test_search_receives_complete_position_history(engine, params, expected_moves):
    """Observe the real search boundary; do not replace its result."""
    engine.handle_position(params.split())
    expected_fen = engine.board.fen(en_passant='fen')
    observed, published = [], threading.Event()
    real_search, real_output = engine.pvs_search, engine._uci_print

    def observe(board, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply == 0:
            observed.append((board.fen(en_passant='fen'),
                             ' '.join(move.uci() for move in board.move_stack)))
        return real_search(board, depth, alpha, beta, lmr_depth, ply,
                           tt_context=tt_context)

    def output(*args, **kwargs):
        real_output(*args, **kwargs)
        if str(args[0]).startswith('bestmove '):
            published.set()

    engine.pvs_search, engine._uci_print = observe, output
    engine.handle_go(['depth', '1'])
    assert published.wait(5)
    engine.handle_stop()
    assert observed == [(expected_fen, expected_moves)]
    assert (engine.board.fen(en_passant='fen'),
            ' '.join(move.uci() for move in engine.board.move_stack)) == (expected_fen, expected_moves)


def test_queued_search_is_isolated_from_later_board_and_move_mutations(monkeypatch, capsys):
    """Gate the real queue, so mutations precede restoration/search in the worker."""
    from queue import Queue
    import chess_engine.uci_search_worker as module

    entered, release, published = (threading.Event() for _ in range(3))

    class GatedQueue(Queue):
        def get(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            return super().get(*args, **kwargs)

    monkeypatch.setattr(module, 'Queue', GatedQueue)
    engine = UCIEngine()
    real_output = engine._uci_print

    def output(*args, **kwargs):
        real_output(*args, **kwargs)
        if str(args[0]).startswith('bestmove '):
            published.set()

    engine._uci_print = output
    try:
        assert entered.wait(2)
        engine.handle_position(('fen 7k/8/8/8/8/8/8/KR6 w - - 0 1 moves '
                                'b1b2 h8g8 b2b1 g8h8 b1b2 h8g8 b2b1').split())
        engine.handle_go(['depth', '3'])
        engine.board.move_stack[0].to_square = chess.B3
        engine.board.reset()
        release.set()
        assert published.wait(15)
        engine.handle_stop()
        lines = capsys.readouterr().out.splitlines()
        assert [s.split()[1] for s in lines if s.startswith('bestmove ')] == ['g8h8']
        assert any(s.startswith('info depth 3 score cp 0 ') for s in lines), lines
        assert not any('error' in s.lower() or 'fallback' in s.lower() for s in lines), lines
        assert engine.board == chess.Board()
    finally:
        release.set()
        engine.handle_quit()

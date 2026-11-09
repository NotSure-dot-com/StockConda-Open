"""Public UCI behavior; no assumptions about per-search thread internals."""
import chess
import pytest

from uci_harness import UciSession


@pytest.fixture
def session():
    engine = UciSession()
    try:
        engine.handshake()
        yield engine
    finally:
        # Included in pytest failure capture, with stdin/stdout/stderr ordering.
        print(engine.transcript())
        engine.close()


def test_regular_start(session):
    session.ready()
    session.finish()


def test_ready_during_search_and_stop(session):
    board = chess.Board()
    session.position(board)
    session.go(board)
    session.ready()
    session.stop()
    session.finish()


def test_two_positions(session):
    board = chess.Board()
    for _ in range(2):
        session.position(board)
        session.go(board)
        session.stop()
        if board.turn:
            board.push_uci('e2e4')
    session.finish()


def test_replacement_search(session):
    board = chess.Board()
    session.position(board)
    session.go(board)
    start = session.go(board)
    session.bestmove(start)  # predecessor's required terminal output
    session.ready()
    session.stop()
    session.finish()


@pytest.mark.parametrize('mutation', ['position', 'ucinewgame', 'setoption'])
def test_mutation_during_search(session, mutation):
    old = chess.Board()
    old.push_uci('e2e4')
    session.position(old)
    session.go(old)
    if mutation == 'position':
        new = old.copy()
        new.push_uci('e7e5')
        start = session.send('position fen ' + new.fen())
    elif mutation == 'ucinewgame':
        new = chess.Board()
        start = session.send('ucinewgame')
    else:
        new = old.copy()
        start = session.send('setoption name Hash value 32')
    session.bestmove(start)
    session.fence()
    session.ready()
    # No position command here: observe the state resulting from mutation.
    session.go(new)
    session.stop()
    session.finish()


@pytest.mark.parametrize('ending', ['quit', 'eof', 'stop_quit'])
def test_shutdown_during_search(session, ending):
    board = chess.Board()
    session.position(board)
    session.go(board)
    if ending == 'stop_quit':
        session.send('stop')
    session.finish(eof=ending == 'eof')


def test_terminal_root(session):
    board = chess.Board('7k/6Q1/5K2/8/8/8/8/8 b - - 0 1')
    assert board.is_checkmate()
    session.position(board)
    start = session.go(board, 'go depth 1')
    session.bestmove(start)
    session.finish()


@pytest.mark.parametrize('command', ['go depth 1', 'go infinite'])
def test_100_search_cycles(session, command):
    board = chess.Board()
    for _ in range(100):
        session.position(board)
        start = session.go(board, command)
        if command == 'go infinite':
            session.stop()
        else:
            session.bestmove(start)
    session.finish()


def test_100_replacement_searches(session):
    board = chess.Board()
    session.position(board)
    session.go(board)
    for _ in range(100):
        start = session.go(board)
        session.bestmove(start)  # terminal output belongs to the predecessor
    session.stop()
    session.finish()

from collections import defaultdict
from pathlib import Path

import chess
import pytest

from chess_engine.cython_ext import transposition as tt
from test_tt_repetition_context import engine, history_pair
from test_repetition_history import played
from uci_harness import UciSession


MASK = (1 << 64) - 1


def mix(value):
    value = ((value ^ (value >> 30)) * 0xbf58476d1ce4e5b9) & MASK
    value = ((value ^ (value >> 27)) * 0x94d049bb133111eb) & MASK
    return (value ^ (value >> 31)) & MASK


def oracle(board):
    replay = board.root()
    key = tt.zobrist_hash(replay)
    history = mix(key ^ 0x243f6a8885a308d3)
    for move in board.move_stack:
        zeroing = replay.is_zeroing(move)
        replay.push(move)
        key = tt.zobrist_hash(replay)
        history = (mix(key ^ 0x243f6a8885a308d3) if zeroing else
                   mix(history ^ key ^ 0x9e3779b97f4a7c15))
    return key, history


def test_history_known_vectors_and_order():
    tt.init_zobrist()
    assert tt.seed_history_cy(0) == 0xe9e0033e3badaf36
    assert tt.seed_history_cy(1) == 0x820477342d9c40c0
    one = tt.advance_history_cy(tt.seed_history_cy(0), 1, False)
    assert one == 0x799eaf5dd592afb2
    assert tt.advance_history_cy(one, 0, False) == 0xd987c92967cea972
    assert tt.advance_history_cy(tt.seed_history_cy(1), 0, False) == 0xb4323b83f39c325e
    assert tt.advance_history_cy(one, 0, True) == tt.seed_history_cy(0)


@pytest.mark.parametrize('repeated', [False, True])
@pytest.mark.parametrize('mirror', [False, True])
def test_bootstrap_is_ordered_and_preserves_the_board(repeated, mirror):
    tt.init_zobrist()
    fen, a, b, _ = history_pair(repeated, mirror)
    boards = [played(fen, a), played(fen, b)]
    contexts = []
    for board in boards:
        before = board.fen(), tuple(m.uci() for m in board.move_stack)
        context = tt.tt_context_from_board(board)
        assert context == oracle(board)
        assert (board.fen(), tuple(m.uci() for m in board.move_stack)) == before
        bare = chess.Board(board.fen())
        assert context != tt.tt_context_from_board(bare)
        board.fullmove_number += 10
        assert tt.tt_context_from_board(board) == context
        contexts.append(context)
    assert contexts[0][0] == contexts[1][0]
    assert contexts[0][1] != contexts[1][1]


@pytest.mark.parametrize('fen,move', [
    ('7k/8/8/8/8/8/P7/KR6 w - - 99 1', 'a2a3'),
    ('7k/8/8/8/8/8/1r6/KR6 w - - 99 1', 'b1b2'),
    ('7k/8/8/3pP3/8/8/8/K7 w - d6 99 1', 'e5d6'),
    ('7k/P7/8/8/8/8/8/7K w - - 99 1', 'a7a8n'),
])
def test_zeroing_seeds_child_position(fen, move):
    tt.init_zobrist()
    board = chess.Board(fen)
    parent = tt.tt_context_from_board(board)
    assert board.is_zeroing(chess.Move.from_uci(move))
    board.push_uci(move)
    key, history = tt.tt_context_from_board(board)
    assert history == tt.advance_history_cy(parent[1], key, True)
    assert (key, history) == tt.tt_context_from_board(chess.Board(board.fen()))


def test_repeated_occurrences_do_not_cancel_and_king_move_does_not_reset():
    tt.init_zobrist()
    board = chess.Board('7k/8/8/8/8/8/8/KR6 w - - 0 1')
    states = [tt.tt_context_from_board(board)]
    for _ in range(2):
        for token in 'b1b2 h8g8 b2b1 g8h8'.split():
            move = chess.Move.from_uci(token)
            parent = tt.tt_context_from_board(board)
            assert not board.is_zeroing(move)
            board.push(move)
            child = tt.tt_context_from_board(board)
            assert child == oracle(board)
            assert child[1] == tt.advance_history_cy(parent[1], child[0], False)
        states.append(tt.tt_context_from_board(board))
    assert len({key for key, _ in states}) == 1
    assert len({history for _, history in states}) == 3


def test_sibling_and_research_contexts_match_independent_history(engine, monkeypatch):
    board = chess.Board()
    before = board.fen(), tuple(board.move_stack)
    real = engine.pvs_search
    visits, first_root_move = defaultdict(list), []

    def leaf(current, alpha, beta, max_q_depth=17):
        root_move = current.move_stack[0]
        if not first_root_move:
            first_root_move.append(root_move)
        return 0 if root_move == first_root_move[0] else 10

    def observe(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply > 0 and depth > 0:
            assert tt_context == oracle(current)
            path = tuple(move.uci() for move in current.move_stack)
            visits[path].append((tt_context, alpha, beta))
        return real(current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context)

    monkeypatch.setattr(engine, 'quiescence_search', leaf)
    monkeypatch.setattr(engine, 'pvs_search', observe)
    score, move = engine.pvs_search(board, 2, -100, 100)
    assert score == 10 and move in board.legal_moves
    assert (board.fen(), tuple(board.move_stack)) == before
    roots = [entries for path, entries in visits.items() if len(path) == 1]
    assert len(roots) >= 2
    repeated = [entries for entries in roots if len(entries) > 1]
    assert repeated
    for entries in repeated:
        assert len({entry[0] for entry in entries}) == 1
        assert len({entry[1:] for entry in entries}) > 1


def test_cancelled_child_does_not_leak_context_or_board(engine, monkeypatch):
    board = chess.Board()
    before = board.fen(), tuple(board.move_stack)
    real = engine.pvs_search
    seen = []

    def cancel(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply == 1:
            assert tt_context == oracle(current)
            seen.append(tt_context)
            engine.stop_flag = True
        return real(current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context)

    monkeypatch.setattr(engine, 'pvs_search', cancel)
    assert engine.pvs_search(board, 2, -10000, 10000) == (None, None)
    assert seen
    assert (board.fen(), tuple(board.move_stack)) == before
    monkeypatch.setattr(engine, 'pvs_search', real)
    engine.stop_flag = False
    fen, a, _, escape = history_pair(False, False)
    other = played(fen, a)
    assert engine.pvs_search(other, 3, -10000, 10000) == (
        0, chess.Move.from_uci(escape))


@pytest.mark.parametrize('entry', ['execute_search_task', 'start_search'])
def test_root_context_built_once_per_search(engine, monkeypatch, entry):
    import chess_engine.stockconda as module
    calls = []
    real = module.tt_context_from_board

    def record(board):
        calls.append(board.fen())
        return real(board)

    monkeypatch.setattr(module, 'tt_context_from_board', record)
    engine.use_opening_book = False
    getattr(engine, entry)(None, 3, False)
    assert calls == [engine.board.fen()]


def test_qsearch_leaf_does_not_compute_child_hash(engine, monkeypatch):
    import chess_engine.stockconda as module

    def forbidden(board):
        raise AssertionError('extra leaf position hash')

    monkeypatch.setattr(module, 'zobrist_hash', forbidden)
    assert module._child_tt_context(chess.Board(), (1, 2), False, 0) is None
    board = chess.Board()
    score, move = engine.pvs_search(board, 1, -10000, 10000)
    assert score is not None and move in board.legal_moves


@pytest.mark.parametrize('ending', ['stop', 'quit', 'eof', 'stop-quit'])
def test_long_history_remains_cancellable_and_shuts_down(ending):
    command = (Path(__file__).parent / 'fixtures' / 'tt_history_long.uci').read_text().strip()
    prefix = 'position startpos moves '
    assert command.startswith(prefix)
    board = chess.Board()
    moves = command[len(prefix):].split()
    assert len(moves) == 120
    for token in moves:
        board.push_uci(token)
    assert not board.is_repetition(3) and not board.is_game_over()
    session = UciSession()
    try:
        session.handshake()
        session.send(command)
        marker = session.go(board)
        session.ready(timeout=1)
        if ending == 'stop':
            session.send('stop')
            session.bestmove(marker)
            session.position(chess.Board())
            following = session.go(chess.Board(), 'go depth 1')
            session.bestmove(following)
            session.finish()
        elif ending == 'quit':
            session.finish()
        elif ending == 'eof':
            session.finish(eof=True)
        else:
            session.send('stop')
            session.finish()  # Writes quit immediately, without waiting for stop.
    finally:
        print(session.transcript())
        session.close()

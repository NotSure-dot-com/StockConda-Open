from contextlib import contextmanager

import chess
import pytest

from chess_engine.cython_ext import transposition as tt
from chess_engine.stockconda import UCIEngine
from test_repetition_history import played, search
from uci_harness import UciSession


def history_pair(repeated=False, mirror=False):
    fen = '7k/8/8/8/8/8/8/KR6 w - - 0 1'
    a = ('b1b2 h8g8 b2b1 g8h8 b1b2 h8g8 b2b1' if repeated else
         'b1b2 h8g8 b2b1 g8h8 b1b2 h8g7 b2b1')
    b = ('b1b3 h8h7 b3b2 h7h8 b2b4 h8g8 b4b1' if repeated else
         'b1b3 h8h7 b3b2 h7h8 b2b4 h8g7 b4b1')
    escape = 'g8h8' if repeated else 'g7h8'
    if mirror:
        fen = chess.Board(fen).mirror().fen()
        def flip(text):
            result = []
            for token in text.split():
                move = chess.Move.from_uci(token)
                result.append(chess.Move(chess.square_mirror(move.from_square),
                                         chess.square_mirror(move.to_square),
                                         promotion=move.promotion).uci())
            return ' '.join(result)
        a, b, escape = flip(a), flip(b), flip(escape)
    first, second = played(fen, a), played(fen, b)
    assert first.fen() == second.fen()
    assert first.is_repetition(2) == repeated
    assert not first.is_repetition(3)
    assert not second.can_claim_threefold_repetition()
    after = first.copy()
    after.push_uci(escape)
    assert after.is_repetition(3)
    return fen, a, b, escape


@contextmanager
def opened():
    session = UciSession()
    try:
        session.handshake()
        yield session
        session.finish()  # Exactly one legal result per go, through EOF.
    finally:
        print(session.transcript())
        session.close()


@pytest.fixture
def engine():
    instance = UCIEngine()
    try:
        yield instance
    finally:
        instance.handle_quit()


@pytest.mark.parametrize('repeated', [False, True])
@pytest.mark.parametrize('mirror', [False, True])
@pytest.mark.parametrize('depth', [3, 5])
@pytest.mark.parametrize('order', ['A-B', 'B-A', 'A-FEN'])
def test_uci_scores_do_not_cross_histories(repeated, mirror, depth, order):
    fen, a, b, escape = history_pair(repeated, mirror)
    first, second = played(fen, a), played(fen, b)
    commands = {
        'A': (f'position fen {fen} moves {a}', first),
        'B': (f'position fen {fen} moves {b}', second),
        'FEN': (f'position fen {first.fen()}', chess.Board(first.fen())),
    }
    seed, query = order.split('-')
    observed = []
    for warm in (False, True):
        with opened() as session:
            if warm:
                command, board = commands[seed]
                score, move = search(session, command, board, depth)
                if seed == 'A':
                    assert (score, move) == (0, escape)
                else:
                    assert score < 0
            command, board = commands[query]
            score, move = search(session, command, board, depth)
            assert chess.Move.from_uci(move) in board.legal_moves
            if query == 'A':
                assert (score, move) == (0, escape)
            else:
                assert score < 0
            observed.append((score, move))
    # cp/PV are recorded, not hardcoded from an old engine's LMR behavior.
    print({'cold': observed[0], 'warm': observed[1]})


@pytest.mark.parametrize('kind', [0, 1, 2])
@pytest.mark.parametrize('tag', [0, 17, 2**64 - 1])
def test_matching_history_values_and_move_hints(engine, kind, tag):
    board = chess.Board('7k/8/8/8/8/8/8/KR6 w - - 7 4')
    key, move = tt.zobrist_hash(board), chess.Move.from_uci('b1b7')
    tt.store_tt_entry_cy(key, 3, 71, kind, move, board.ply(), 7, tag)
    hit = tt.probe_tt_entry_cy(key, 3, board.ply(), 7, tag)
    assert hit is not None
    assert (hit.value, hit.depth, hit.node_type, hit.best_move) == (
        71, 3, ('EXACT', 'LOWERBOUND', 'UPPERBOUND')[kind], move)
    assert tt.probe_tt_entry_cy(key, 4, board.ply(), 7, tag) is None
    assert tt.probe_tt_entry_cy(key, 3, board.ply(), 7, tag ^ 1) is None
    assert tt.probe_tt_move_cy(key) == (move, 3)


@pytest.mark.parametrize('bad,valid', [
    pytest.param(None, 0, id='None'),
    pytest.param(True, 1, id='True'),
    pytest.param(-1, 2**64 - 1, id='-1'),
    pytest.param(2**64, 0, id=str(2**64)),
    pytest.param(1.0, 1, id='1.0'),
    pytest.param('1', 1, id='1'),
])
@pytest.mark.parametrize('side', ['store', 'probe'])
def test_invalid_history_is_move_only(engine, bad, valid, side):
    board = chess.Board('7k/8/8/8/8/8/8/KR6 w - - 0 1')
    key, move = tt.zobrist_hash(board), chess.Move.from_uci('b1b7')
    # Pair every invalid value with the uint64 value a permissive coercion
    # could produce, so rejection cannot hide behind an unrelated tag miss.
    stored, queried = (bad, valid) if side == 'store' else (valid, bad)
    tt.store_tt_entry_cy(key, 3, 17, 0, move, 0, 0, stored)
    assert tt.probe_tt_entry_cy(key, 3, 0, 0, queried) is None
    assert tt.probe_tt_move_cy(key) == (move, 3)


def test_raw_missing_history_is_not_inferred(engine):
    board = chess.Board('7k/8/8/8/8/8/8/KR6 w - - 0 1')
    key = tt.zobrist_hash(board)
    move = chess.Move.from_uci('b1b7')
    tt.store_tt_entry_cy(key, 3, 17, 0, move, 0, halfmove_clock=0)
    assert tt.probe_tt_entry(board, 3) is None
    assert tt.probe_tt_entry_cy(key, 3, 0, halfmove_clock=0) is None
    assert tt.probe_tt_move_cy(key) == (move, 3)


def test_replacement_keeps_complete_history_metadata(engine):
    key = tt.zobrist_hash(chess.Board())
    first, second = chess.Move.from_uci('e2e4'), chess.Move.from_uci('d2d4')
    tt.store_tt_entry_cy(key, 3, 11, 0, first, 0, 0, 10)
    tt.store_tt_entry_cy(key, 2, 22, 1, second, 0, 0, 20)
    assert tt.probe_tt_entry_cy(key, 3, 0, 0, 10).value == 11
    assert tt.probe_tt_entry_cy(key, 2, 0, 0, 20) is None
    assert tt.probe_tt_move_cy(key) == (first, 3)
    tt.store_tt_entry_cy(key, 3, 22, 1, second, 0, 0, 20)
    assert tt.probe_tt_entry_cy(key, 3, 0, 0, 10) is None
    assert tt.probe_tt_entry_cy(key, 3, 0, 0, 20).value == 22
    assert tt.probe_tt_move_cy(key) == (second, 3)
    tt.store_tt_entry_cy(key, 4, 33, 0, first, 0, 0, None)
    assert tt.probe_tt_entry_cy(key, 3, 0, 0, 20) is None
    assert tt.probe_tt_entry_cy(key, 3, 0, 0, 0) is None
    assert tt.probe_tt_move_cy(key) == (first, 4)


@pytest.mark.parametrize('mib', [1, 3, 16, 17])
def test_native_allocation_stays_inside_requested_budget(engine, mib):
    requested = mib * 2**20
    tt.init_cython_tt(requested)
    size, slots, allocated = tt.tt_layout_cy()
    assert size >= 24 and slots > 0
    assert slots & (slots - 1) == 0
    assert allocated == size * slots <= requested
    assert 2 * allocated > requested
    print({'entry_bytes': size, 'slots': slots, 'allocated': allocated})


@pytest.mark.parametrize('depth', [3, 5])
@pytest.mark.parametrize('mirror', [False, True])
def test_ancestor_values_stay_with_their_history(engine, depth, mirror):
    fen, a, b, escape = history_pair(False, mirror)
    first, second = played(fen, a), played(fen, b)
    assert not first.is_repetition(2) and not second.is_repetition(2)
    score, move = engine.pvs_search(first, depth, -10000, 10000)
    assert (score, move) == (0, chess.Move.from_uci(escape))
    hit = tt.probe_tt_entry(first, depth)
    assert hit is not None and hit.value == 0
    assert tt.probe_tt_entry(second, depth) is None
    score, move = engine.pvs_search(second, depth, -10000, 10000)
    assert score < 0 and move in second.legal_moves
    hit = tt.probe_tt_entry(second, depth)
    assert hit is not None and hit.value == score
    assert tt.probe_tt_entry(first, depth) is None
    score, move = engine.pvs_search(first, depth, -10000, 10000)
    assert (score, move) == (0, chess.Move.from_uci(escape))


@pytest.mark.parametrize('kind,value', [('LOWERBOUND', 5), ('UPPERBOUND', -5)])
def test_history_mismatch_cannot_narrow_child_window(engine, monkeypatch, kind, value):
    fen, a, b, escape = history_pair(False, False)
    query, source = played(fen, a), played(fen, b)
    move = chess.Move.from_uci(escape)
    tt.store_tt_entry(source, 3, value, kind, move)
    real, windows = engine.pvs_search, []

    def observe(board, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply == 1:
            windows.append((alpha, beta))
        return real(board, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context)

    monkeypatch.setattr(engine, 'pvs_search', observe)
    score, result = engine.pvs_search(query, 1, -10, 10)
    assert (score, result) == (0, move)
    assert windows[0] == (-10, 10)

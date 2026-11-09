"""TT values require exact halfmove context; move hints remain independent.

Negative controls: the old extension and a removed context comparison must
fail cross-clock cases. Reject-all-values must fail same-context cases;
clock coercion/omission must fail invalid-context and boundary cases.
"""
import chess
import pytest

from chess_engine.cython_ext import transposition as tt
from chess_engine.cython_ext.evaluation import evaluate_board
from chess_engine.stockconda import UCIEngine
from uci_harness import UciSession


def rook_board(clock=0, fullmove=1):
    return chess.Board(f'7k/8/8/8/8/8/8/KR6 w - - {clock} {fullmove}')


@pytest.fixture
def engine():
    value = UCIEngine()
    try:
        yield value
    finally:
        value.handle_quit()


@pytest.mark.parametrize('kind', ['EXACT', 'LOWERBOUND', 'UPPERBOUND'])
@pytest.mark.parametrize('stored,queried', [(0, 99), (99, 0)])
def test_cross_clock_values_rejected_but_move_available(engine, kind, stored, queried):
    move = chess.Move.from_uci('b1b7')
    tt.store_tt_entry(rook_board(stored), 3, 17, kind, move)
    assert tt.probe_tt_entry(rook_board(queried), 3) is None
    assert tt.probe_tt_move(rook_board(queried)) == (move, 3)


@pytest.mark.parametrize('kind', ['EXACT', 'LOWERBOUND', 'UPPERBOUND'])
@pytest.mark.parametrize('clock', [0, 99, 100, 65536, 2**32 - 2])
def test_matching_context_still_reuses_values(engine, kind, clock):
    board = rook_board(clock)
    move = chess.Move.from_uci('b1b7')
    tt.store_tt_entry(board, 3, 17, kind, move)
    hit = tt.probe_tt_entry(board, 3)
    assert hit is not None
    assert (hit.value, hit.depth, hit.node_type, hit.best_move) == (17, 3, kind, move)
    assert tt.probe_tt_entry(board, 4) is None


@pytest.mark.parametrize('bad', [None, -1, 2**32 - 1, 2**40, 1.5, '0', True])
def test_raw_api_unknown_or_invalid_context_cannot_publish_value(engine, bad):
    key = tt.zobrist_hash(rook_board())
    history = tt.tt_context_from_board(rook_board())[1]
    move = chess.Move.from_uci('b1b7')
    tt.store_tt_entry_cy(
        key, 3, 17, 0, move, 0, halfmove_clock=bad, history_context=history)
    assert tt.probe_tt_entry_cy(
        key, 3, 0, halfmove_clock=0, history_context=history) is None
    assert tt.probe_tt_move_cy(key) == (move, 3)


@pytest.mark.parametrize('bad', [None, -1, 2**32 - 1, 2**40, 1.5, '0', True])
def test_raw_api_unknown_or_invalid_query_is_miss(engine, bad):
    key = tt.zobrist_hash(rook_board())
    history = tt.tt_context_from_board(rook_board())[1]
    tt.store_tt_entry(rook_board(), 3, 17, 'EXACT', chess.Move.from_uci('b1b7'))
    assert tt.probe_tt_entry_cy(
        key, 3, 0, halfmove_clock=bad, history_context=history) is None


def test_legacy_raw_calls_without_context_are_move_only(engine):
    key = tt.zobrist_hash(rook_board())
    move = chess.Move.from_uci('b1b7')
    tt.store_tt_entry_cy(key, 3, 17, 0, move, 0)
    assert tt.probe_tt_entry_cy(key, 3, 0) is None
    assert tt.probe_tt_move_cy(key) == (move, 3)


def test_replacement_updates_value_and_context_together(engine):
    zero, ninety_nine = rook_board(0), rook_board(99)
    first, second = chess.Move.from_uci('b1b7'), chess.Move.from_uci('b1b8')
    tt.store_tt_entry(zero, 3, 17, 'EXACT', first)
    # A rejected shallower store must not change either context or move.
    tt.store_tt_entry(ninety_nine, 2, 19, 'EXACT', second)
    assert tt.probe_tt_entry(zero, 3).value == 17
    assert tt.probe_tt_entry(ninety_nine, 2) is None
    assert tt.probe_tt_move(ninety_nine) == (first, 3)
    # Equal/deeper accepted stores carry the new context with the value.
    tt.store_tt_entry(ninety_nine, 3, 19, 'EXACT', second)
    assert tt.probe_tt_entry(zero, 3) is None
    assert tt.probe_tt_entry(ninety_nine, 3).value == 19
    assert tt.probe_tt_move(zero) == (second, 3)
    tt.store_tt_entry(zero, 4, 23, 'EXACT', first)
    assert tt.probe_tt_entry(zero, 4).value == 23
    assert tt.probe_tt_entry(ninety_nine, 3) is None
    # A contextless replacement must not inherit a formerly valid context.
    tt.store_tt_entry_cy(tt.zobrist_hash(zero), 5, 29, 0, second, 0)
    assert tt.probe_tt_entry(zero, 3) is None
    assert tt.probe_tt_entry(ninety_nine, 3) is None
    assert tt.probe_tt_move(zero) == (second, 5)


def test_empty_slot_cannot_supply_clock_zero_value(engine):
    assert tt.probe_tt_entry_cy(
        0, 0, 0, halfmove_clock=0, history_context=0) is None


@pytest.mark.parametrize('clock', [0, 99, 100, 65536, 2**32 - 2])
def test_raw_clock_guard_is_independent_of_history(engine, clock):
    board = rook_board(clock)
    key, history = tt.tt_context_from_board(board)
    move = chess.Move.from_uci('b1b7')
    tt.store_tt_entry_cy(key, 3, 17, 0, move, 0, clock, history)
    assert tt.probe_tt_entry_cy(key, 3, 0, clock, history).value == 17
    assert tt.probe_tt_entry_cy(key, 3, 0, clock + 1, history) is None
    assert tt.probe_tt_move_cy(key) == (move, 3)


@pytest.mark.parametrize('bad', [-1, 2**32 - 1, 2**40])
def test_board_wrapper_does_not_wrap_context(engine, bad):
    board = rook_board()
    board.halfmove_clock = bad
    tt.store_tt_entry(board, 3, 17, 'EXACT', chess.Move.from_uci('b1b7'))
    assert tt.probe_tt_entry(board, 3) is None
    assert tt.probe_tt_entry(rook_board(), 3) is None


@pytest.mark.parametrize('value,want', [(9500, 9480), (-9500, -9480)])
def test_mate_normalization_uses_ply_not_halfmove_context(engine, value, want):
    tt.store_tt_entry(rook_board(0, 1), 3, value, 'EXACT', chess.Move.from_uci('b1b7'))
    hit = tt.probe_tt_entry(rook_board(0, 11), 3)
    assert hit is not None and hit.value == want
    assert tt.probe_tt_entry(rook_board(99, 11), 3) is None


@pytest.mark.parametrize('kind,value,alpha,beta', [
    ('EXACT', 534, -10000, 10000), ('LOWERBOUND', 1, -1, 1), ('UPPERBOUND', -1, -1, 1)])
def test_stale_values_cannot_cut_off_real_search(engine, kind, value, alpha, beta):
    tt.store_tt_entry(rook_board(0), 3, value, kind, chess.Move.from_uci('b1b7'))
    board = rook_board(99)
    score, move = engine.pvs_search(board, 1, alpha, beta)
    assert score == 0
    assert move in board.legal_moves


@pytest.mark.parametrize('kind,value', [('LOWERBOUND', 5), ('UPPERBOUND', -5)])
def test_stale_bounds_cannot_narrow_child_window(engine, monkeypatch, kind, value):
    tt.store_tt_entry(rook_board(0), 3, value, kind, chess.Move.from_uci('b1b7'))
    original = engine.pvs_search
    windows = []

    def observe(board, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply == 1:
            windows.append((alpha, beta))
        return original(board, depth, alpha, beta, lmr_depth, ply,
                        tt_context=tt_context)

    monkeypatch.setattr(engine, 'pvs_search', observe)
    score, move = engine.pvs_search(rook_board(99), 1, -10, 10)
    assert score == 0
    assert windows[0] == (-10, 10)
    assert move == chess.Move.from_uci('b1b7')  # Legal TT move remains first.


def test_distinct_paths_in_one_tree_do_not_share_incompatible_bound(engine):
    origin = rook_board(94)
    query = rook_board(94)
    for move in ['b1b8', 'h8h7', 'a1b2']:
        origin.push_uci(move)
    for move in ['b1b6', 'h8g8', 'a1b2', 'g8h7', 'b6b8']:
        query.push_uci(move)
    assert (origin.halfmove_clock, query.halfmove_clock) == (97, 99)
    assert not query.is_repetition(2)
    tt.store_tt_entry(origin, 2, -1, 'UPPERBOUND', chess.Move.from_uci('h7g7'))
    score, move = engine.pvs_search(query, 1, -1, 1)
    assert score == 0
    assert move in query.legal_moves


@pytest.mark.parametrize('fen,move', [
    ('7k/8/8/8/8/8/P7/KR6 w - - 99 1', 'a2a3'),
    ('7k/8/8/8/8/8/1r6/KR6 w - - 99 1', 'b1b2'),
    ('7k/8/8/3pP3/8/8/8/K7 w - d6 99 1', 'e5d6')])
def test_reset_move_context_reaches_tt_correctly(engine, fen, move):
    board = chess.Board(fen)
    board.push_uci(move)
    tt.store_tt_entry(board, 1, 17, 'EXACT', next(iter(board.legal_moves)))
    query = board.copy()
    query.halfmove_clock = 0
    hit = tt.probe_tt_entry(query, 1)
    assert hit is not None and hit.value == 17
    query.halfmove_clock = 99
    assert tt.probe_tt_entry(query, 1) is None


def test_terminal_draw_and_mate_precedence_unchanged(engine):
    board = rook_board(99)
    board.push_uci('b1b7')
    assert evaluate_board(board, board.turn) == (0, True)
    mate = chess.Board('7k/6Q1/6K1/8/8/8/8/8 b - - 100 1')
    assert mate.is_checkmate()
    assert evaluate_board(mate, mate.turn) == (-9999, False)


def search(session, board):
    session.position(board)
    marker = session.go(board, 'go depth 3')
    session.wait(lambda line: line.startswith('bestmove '), marker, 30)
    info = [line for _, channel, line in session.events[marker:]
            if channel == 'stdout' and line.startswith('info depth ')][-1].split()
    assert info[2:5] == ['3', 'score', 'cp']
    return int(info[5])


@pytest.mark.parametrize('seed_clock,target_clock', [(0, 99), (99, 0)])
def test_uci_previous_search_cannot_poison_different_clock(seed_clock, target_clock):
    results = []
    for warm in [False, True]:
        session = UciSession()
        try:
            session.handshake()
            if warm:
                search(session, rook_board(seed_clock))
            results.append(search(session, rook_board(target_clock)))
            session.finish()
        finally:
            session.close()
    if target_clock == 99:
        assert results == [0, 0]
    else:
        assert results[0] > 0
        assert results[1] == results[0]

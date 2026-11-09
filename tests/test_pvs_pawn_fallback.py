"""Prospectively fixed material boundary and one-pass ordering contracts."""
import chess
import pytest
import chess_engine.stockconda as module
from chess_engine.stockconda import UCIEngine
from test_pvs_lazy_ordering import bare_engine, capture_entry_ordering, baseline_order

PAWNS = (
    '8/5k2/6p1/3p4/3P4/6P1/5K2/8 w - - 0 1',
    '7k/8/8/3pP3/8/8/8/K7 w - d6 0 1',
    '7k/8/8/8/3p4/4K3/P7/8 w - - 0 1',
    '7k/P7/8/8/8/8/8/K7 w - - 0 1',
)

@pytest.mark.parametrize('fen', PAWNS)
@pytest.mark.parametrize('hint', ('missing', 'legal', 'illegal'))
@pytest.mark.parametrize('extra', (None, 'N', 'B', 'R', 'Q', 'n', 'b', 'r', 'q'))
def test_material_boundary_order_and_restoration(monkeypatch, fen, hint, extra):
    board = chess.Board(fen)
    if extra:
        board.set_piece_at(chess.H5, chess.Piece.from_symbol(extra))
    engine = bare_engine()
    legal = list(board.legal_moves)
    assert legal
    tt = legal[-1] if hint == 'legal' else chess.Move.from_uci('a1a8') if hint == 'illegal' else None
    if hint == 'illegal':
        assert not board.is_legal(tt)
    engine.killer_moves[0] = [legal[0], legal[-1]]
    entry = capture_entry_ordering(board, engine, 0)
    expected = baseline_order(board, entry, tt if hint == 'legal' else None)
    monkeypatch.setattr(module, 'probe_tt_entry', lambda *a, **k: None)
    monkeypatch.setattr(module, 'probe_tt_move', lambda *a, **k: (tt, None))
    monkeypatch.setattr(module, 'store_tt_entry', lambda *a, **k: None)
    full, staged, visits = [], [], []
    old_full = engine._prepare_pvs_moves
    old_stage = engine._generate_pvs_captures
    def prepare(*a, **k):
        full.append(1)
        return old_full(*a, **k)
    def captures(*a, **k):
        staged.append(1)
        return old_stage(*a, **k)
    engine._prepare_pvs_moves = prepare
    engine._generate_pvs_captures = captures
    real = UCIEngine.pvs_search.__get__(engine)
    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, **kwargs):
        if ply:
            visits.append(current.peek())
            # Recursive changes must not change later sibling ordering.
            engine.history_table.add(entry.turn, legal[-1].from_square, legal[-1].to_square, 999)
            return 0, None
        return real(current, depth, alpha, beta, lmr_depth, ply, **kwargs)
    engine.pvs_search = dispatch
    before = board.fen(), tuple(board.move_stack)
    engine.pvs_search(board, 1, 0, 100, tt_context=(1, 2))
    assert tuple(visits) == expected
    assert (board.fen(), tuple(board.move_stack)) == before
    assert len(full) == (1 if extra is None else 0)
    assert len(staged) == (0 if extra is None else 1)

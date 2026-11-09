"""Depth-table regressions using real PVS and the failed preflight position.

Restoring the old 0..15 history-factor allocation must reject every depth-16
and depth-64 case. Literal low-depth bonuses protect the existing formulas.
The 253-ply fixture is the exact game-three position from the retained strict
preflight, not an opening selected for a favourable strength result.
"""

from pathlib import Path

import chess
import pytest

from chess_engine.stockconda import UCIEngine
from uci_harness import UciSession


@pytest.fixture
def engine():
    value = UCIEngine()
    try:
        yield value
    finally:
        value.handle_quit()


@pytest.mark.parametrize('depth,bonuses', [
    (1, (12, 4, 1)),
    (15, (3610, 270, 120)),
    (16, (4362, 304, 136)),
    (64, (266250, 4288, 2080)),
])
@pytest.mark.parametrize('alpha,beta,bonus_index', [
    (-1, 0, 0),
    (-1, 1, 1),
    (1, 2, 2),
], ids=['quiet-beta-cutoff', 'alpha-improvement', 'regular-update'])
def test_real_pvs_history_updates_cover_supported_depths(
        engine, depth, bonuses, alpha, beta, bonus_index):
    # Every legal move is quiet and reaches the existing fifty-move draw rule.
    # Children therefore return zero without an expensive depth-64 search tree.
    board = chess.Board('7k/8/8/8/8/8/8/KR6 w - - 99 50')
    assert board.is_valid() and not board.is_fifty_moves()
    before = board.fen()
    legal = list(board.legal_moves)
    assert legal
    for move in legal:
        assert not board.is_zeroing(move)
        child = board.copy()
        child.push(move)
        assert child.is_fifty_moves()

    try:
        score, move = engine.pvs_search(board, depth, alpha, beta)
    except IndexError as exc:
        raise AssertionError(
            f'PVS history update must support depth {depth}: {exc}'
        ) from exc

    assert score == 0 and move in legal
    assert board.fen() == before and not board.move_stack
    assert engine.history_table[move.from_square][move.to_square][board.turn] == (
        bonuses[bonus_index]
    )


@pytest.mark.parametrize('depth', [15, 16, 64])
def test_uci_preflight_position_completes_requested_depth(depth):
    """An error/fallback, silently capped depth or lost result must fail."""
    fixture = Path(__file__).parent / 'fixtures/history_depth_preflight.uci'
    command = fixture.read_text(encoding='utf-8').strip()
    parts = command.split()
    assert parts[:3] == ['position', 'startpos', 'moves']
    assert len(parts[3:]) == 253
    board = chess.Board()
    for token in parts[3:]:
        board.push_uci(token)
    assert board.is_valid() and any(board.legal_moves)

    session = UciSession()
    try:
        session.handshake()
        session.send('setoption name Hash value 16')
        session.send('setoption name MaxDepth value 64')
        session.fence()
        session.ready()
        session.send(command)
        start = session.go(board, f'go depth {depth}')
        session.wait(lambda line: line.startswith('bestmove '), start, 15)
        # Count and validate every result through normal shutdown and reader EOF.
        session.finish()
        lines = [line for _, channel, line in session.events[start:]
                 if channel == 'stdout']
        assert not any('error' in line.lower() or 'fallback' in line.lower()
                       for line in lines), session.transcript()
        infos = [line for line in lines if line.startswith('info depth ')]
        assert infos and infos[-1].startswith(f'info depth {depth} '), (
            session.transcript()
        )
        last_info = infos[-1].split()
        assert last_info[last_info.index('score') + 1:last_info.index('score') + 3] == (
            ['cp', '0']
        ), session.transcript()
        pv_head = last_info[last_info.index('pv') + 1]
        bestmoves = [line for line in lines if line.startswith('bestmove ')]
        assert len(bestmoves) == 1 and bestmoves[0].split()[1] == pv_head, (
            session.transcript()
        )
    finally:
        try:
            session.close()
        finally:
            print(session.transcript())

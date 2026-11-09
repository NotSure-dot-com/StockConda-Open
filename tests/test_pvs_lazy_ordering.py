"""Behavioral contract for AP-04A's TT-first PVS move preparation.

The expected ordering in this file is deliberately independent of the engine's
move-ordering implementation.  It uses python-chess legal moves, literal AP-04
priorities, and immutable history/killer values captured before any child is
searched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator

import chess
import pytest

import chess_engine.stockconda as stockconda
from chess_engine.cython_ext import cython_chess
from chess_engine.stockconda import HistoryTable, MATE_SCORE, UCIEngine


PIECE_VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 20,
}


@dataclass(frozen=True)
class EntryOrdering:
    """Immutable, test-local view of the inputs that define baseline order."""

    turn: chess.Color
    killers: tuple[chess.Move | None, chess.Move | None]
    history: tuple[tuple[int, ...], ...]
    legal_moves: tuple[chess.Move, ...]


def capture_entry_ordering(
    board: chess.Board,
    engine: UCIEngine,
    ply: int,
) -> EntryOrdering:
    """Capture ordering inputs without calling a candidate ordering helper."""

    turn = board.turn
    return EntryOrdering(
        turn=turn,
        killers=tuple(engine.killer_moves[ply]),
        history=tuple(
            tuple(engine.history_table[source][target][turn] for target in range(64))
            for source in range(64)
        ),
        legal_moves=tuple(board.legal_moves),
    )


def literal_priority(
    board: chess.Board,
    move: chess.Move,
    tt_move: chess.Move | None,
    killers: tuple[chess.Move | None, chess.Move | None],
) -> int:
    """AP-04 priority rules, written as literals instead of engine helpers."""

    if move == tt_move:
        return 100000
    if board.is_capture(move):
        attacker = board.piece_type_at(move.from_square)
        victim = board.piece_type_at(move.to_square)
        if victim is None:  # En-passant captures a pawn off the destination square.
            victim = chess.PAWN
        return 10000 + PIECE_VALUES[victim] * 10 - PIECE_VALUES[attacker]
    if move == killers[0]:
        return 6000
    if move == killers[1]:
        return 5000
    return 0


def baseline_order(
    board: chess.Board,
    entry: EntryOrdering,
    tt_move: chess.Move | None,
) -> tuple[chess.Move, ...]:
    """Stable baseline order from an already captured node-entry view."""

    decorated = []
    for move in entry.legal_moves:
        priority = literal_priority(board, move, tt_move, entry.killers)
        history = entry.history[move.from_square][move.to_square]
        decorated.append((-priority, -history, move))
    decorated.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in decorated)


def entry_for_moves(
    board: chess.Board,
    engine: UCIEngine,
    ply: int,
    moves: list[chess.Move],
) -> EntryOrdering:
    """Use the same immutable entry values with a deliberately small legal fixture."""

    entry = capture_entry_ordering(board, engine, ply)
    assert all(move in entry.legal_moves for move in moves)
    return EntryOrdering(entry.turn, entry.killers, entry.history, tuple(moves))


def bare_engine() -> UCIEngine:
    """Construct the real PVS owner without allocating a worker or a large TT."""

    engine = UCIEngine.__new__(UCIEngine)
    engine.stop_flag = False
    engine.stop_flag_check = lambda: False
    engine.history_table = HistoryTable()
    engine.killer_moves = [[None, None] for _ in range(engine.MAX_LMR_DEPTH)]
    engine.lmr_reduction_table = [
        [0 for _ in range(engine.MAX_MOVES_PER_POSITION)]
        for _ in range(engine.MAX_LMR_DEPTH + 1)
    ]
    engine.history_factors = {
        "beta_cutoff": [1 for _ in range(engine.MAX_LMR_DEPTH + 1)],
        "alpha_improve": [1 for _ in range(engine.MAX_LMR_DEPTH + 1)],
        "regular_update": [1 for _ in range(engine.MAX_LMR_DEPTH + 1)],
    }
    return engine


def isolate_tt(
    monkeypatch: pytest.MonkeyPatch,
    tt_move: chess.Move | None | Callable[[chess.Board], chess.Move | None],
    stores: list[tuple[object, ...]] | None = None,
) -> list[chess.Board]:
    """Disable bound hits while retaining a controlled move-only TT hint."""

    probes: list[chess.Board] = []

    def probe(board: chess.Board, **_kwargs: object):
        probes.append(board)
        move = tt_move(board) if callable(tt_move) else tt_move
        return move, 7 if move is not None else None

    monkeypatch.setattr(stockconda, "probe_tt_entry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(stockconda, "probe_tt_move", probe)
    monkeypatch.setattr(
        stockconda,
        "store_tt_entry",
        lambda *args, **_kwargs: stores.append(args) if stores is not None else None,
    )
    return probes


def selected_generator(
    moves_for: Callable[[chess.Board], list[chess.Move]],
) -> Callable[..., tuple[bool, Iterator[chess.Move]]]:
    def generate(board: chess.Board, *_masks: object):
        moves = moves_for(board)
        assert all(move in board.legal_moves for move in moves)
        return board.is_check(), iter(moves)

    return generate


ORDERING_FIXTURES = [
    pytest.param(chess.STARTING_FEN, "e2e4", False, id="ordinary"),
    pytest.param(
        "k3r3/8/8/8/8/8/8/4KB2 w - - 0 1",
        "e1d1",
        True,
        id="check",
    ),
    pytest.param(
        "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
        "e1g1",
        False,
        id="castling",
    ),
    pytest.param(
        "7k/8/8/3pP3/8/8/8/K7 w - d6 0 1",
        "e5d6",
        False,
        id="en-passant",
    ),
    pytest.param(
        "7k/P7/8/8/8/8/8/7K w - - 0 1",
        "a7a8q",
        False,
        id="promotions",
    ),
    pytest.param(
        "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1",
        None,
        True,
        id="checkmate",
    ),
    pytest.param(
        "7k/5K2/6Q1/8/8/8/8/8 b - - 0 1",
        None,
        False,
        id="stalemate",
    ),
]


@pytest.mark.parametrize("fen,distinguishing_move,in_check", ORDERING_FIXTURES)
def test_full_generator_and_literal_oracle_cover_required_positions(
    fen: str,
    distinguishing_move: str | None,
    in_check: bool,
):
    """Catches legality drift or an ordering oracle coupled to candidate code."""

    board = chess.Board(fen)
    engine = bare_engine()
    entry = capture_entry_ordering(board, engine, 0)
    native_check, native_moves = cython_chess.generate_search_moves(
        board, chess.BB_ALL, chess.BB_ALL
    )

    assert native_check is in_check
    assert tuple(native_moves) == entry.legal_moves
    assert set(baseline_order(board, entry, None)) == set(entry.legal_moves)
    if distinguishing_move is not None:
        assert chess.Move.from_uci(distinguishing_move) in entry.legal_moves
    else:
        assert entry.legal_moves == ()


def test_tied_captures_use_entry_history_then_stable_generator_order():
    """Catches omission of capture history or an added tie-breaker."""

    board = chess.Board("7k/8/8/3pp3/8/2N2N2/8/K7 w - - 0 1")
    engine = bare_engine()
    first = chess.Move.from_uci("c3d5")
    second = chess.Move.from_uci("f3e5")
    assert board.is_capture(first) and board.is_capture(second)
    assert literal_priority(board, first, None, (None, None)) == literal_priority(
        board, second, None, (None, None)
    )

    native = tuple(board.legal_moves)
    generator_pair = tuple(move for move in native if move in (first, second))
    assert len(generator_pair) == 2
    later = generator_pair[1]
    engine.history_table[later.from_square][later.to_square][board.turn] = 90
    order = baseline_order(board, capture_entry_ordering(board, engine, 0), None)
    ordered_pair = tuple(move for move in order if move in generator_pair)
    assert ordered_pair == (later, generator_pair[0])

    engine.history_table[generator_pair[0].from_square][generator_pair[0].to_square][
        board.turn
    ] = 90
    entry = capture_entry_ordering(board, engine, 0)
    tied = [move for move in entry.legal_moves if move in (first, second)]
    ordered_tied = [move for move in baseline_order(board, entry, None) if move in tied]
    assert ordered_tied == tied


def test_quiet_promotions_keep_non_capture_killer_priority():
    """Catches classifying every promotion as a capture or excluding it as a killer."""

    board = chess.Board("7k/P7/8/8/8/8/8/7K w - - 0 1")
    engine = bare_engine()
    promotion = chess.Move.from_uci("a7a8n")
    engine.killer_moves[0] = [promotion, None]
    entry = capture_entry_ordering(board, engine, 0)
    assert not board.is_capture(promotion)
    assert literal_priority(board, promotion, None, entry.killers) == 6000


@pytest.mark.parametrize("later_history", [0, 90], ids=["equal-full-key", "unequal-history"])
def test_real_pvs_orders_equal_priority_captures_by_entry_history_stably(
    monkeypatch: pytest.MonkeyPatch,
    later_history: int,
):
    """Catches a SUT remainder key that omits history for equal-priority captures."""

    board = chess.Board("7k/8/8/3pp3/8/2N2N2/8/K7 w - - 0 1")
    before = board.fen(), tuple(board.move_stack)
    engine = bare_engine()
    tt_move = chess.Move.from_uci("a1b1")
    captures = tuple(
        move
        for move in board.legal_moves
        if move in (chess.Move.from_uci("c3d5"), chess.Move.from_uci("f3e5"))
    )
    assert len(captures) == 2
    later = captures[1]
    engine.history_table[later.from_square][later.to_square][board.turn] = later_history
    moves = [tt_move, *captures]
    entry = entry_for_moves(board, engine, 0, moves)
    expected = baseline_order(board, entry, tt_move)
    expected_captures = tuple(move for move in expected if move in captures)
    assert expected_captures == (
        (later, captures[0]) if later_history else captures
    )

    isolate_tt(monkeypatch, tt_move)
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        selected_generator(lambda _board: moves.copy()),
    )
    real = UCIEngine.pvs_search.__get__(engine)
    visits: list[chess.Move] = []

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            visits.append(current.move_stack[-1])
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    engine.pvs_search(board, 1, 0, 100, tt_context=(11, 22))
    assert tuple(visits) == expected
    assert (board.fen(), tuple(board.move_stack)) == before


@pytest.mark.parametrize(
    "fen,tt_uci",
    [
        pytest.param(chess.STARTING_FEN, "e2e4", id="quiet"),
        pytest.param("7k/8/8/3p4/8/2N5/8/K7 w - - 0 1", "c3d5", id="capture"),
        pytest.param(
            "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1", id="castle"
        ),
        pytest.param(
            "k3r3/8/8/8/8/8/8/4KB2 w - - 0 1", "e1f2", id="check"
        ),
        pytest.param(
            "7k/8/8/3pP3/8/8/8/K7 w - d6 0 1", "e5d6", id="en-passant"
        ),
        pytest.param("7k/P7/8/8/8/8/8/7K w - - 0 1", "a7a8q", id="promotion"),
        pytest.param(
            "k6r/6P1/8/8/8/8/8/K7 w - - 0 1", "g7h8q", id="capture-promotion"
        ),
    ],
)
def test_legal_tt_move_is_first_unique_and_preserves_literal_remainder(
    monkeypatch: pytest.MonkeyPatch,
    fen: str,
    tt_uci: str,
):
    """Catches duplicate TT dispatch and category-specific reorderings."""

    board = chess.Board(fen)
    before = board.fen(), tuple(board.move_stack)
    engine = bare_engine()
    tt_move = chess.Move.from_uci(tt_uci)
    entry = capture_entry_ordering(board, engine, 0)
    expected = baseline_order(board, entry, tt_move)
    visits: list[chess.Move] = []
    isolate_tt(monkeypatch, tt_move)
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        selected_generator(lambda current: list(current.legal_moves)),
    )
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            visits.append(current.move_stack[-1])
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    value, move = engine.pvs_search(board, 1, 0, 100, tt_context=(11, 22))

    assert isinstance(value, int) and move in entry.legal_moves
    assert tuple(visits) == expected
    assert visits[0] == tt_move and visits.count(tt_move) == 1
    assert (board.fen(), tuple(board.move_stack)) == before


def test_tt_cutoff_does_not_prepare_remainder(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches eager full generation/decorating before a real TT-first cutoff."""

    board = chess.Board()
    before = board.fen(), tuple(board.move_stack)
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    stores: list[tuple[object, ...]] = []
    isolate_tt(monkeypatch, tt_move, stores)

    def forbidden_generation(*_args, **_kwargs):
        raise AssertionError("TT cutoff prepared the remainder")

    monkeypatch.setattr(
        stockconda.cython_chess, "generate_search_moves", forbidden_generation
    )
    real = UCIEngine.pvs_search.__get__(engine)
    visits: list[chess.Move] = []

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            visits.append(current.move_stack[-1])
            return -10, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    assert engine.pvs_search(board, 1, -100, 5, tt_context=(11, 22)) == (
        10,
        tt_move,
    )
    assert visits == [tt_move]
    assert len(stores) == 1
    assert (board.fen(), tuple(board.move_stack)) == before


def test_absent_hint_stages_capture_probe_before_full_other_order(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches losing baseline order after the new no-hint capture stage."""

    board = chess.Board()
    engine = bare_engine()
    entry = capture_entry_ordering(board, engine, 0)
    expected = baseline_order(board, entry, None)
    events: list[str] = []
    isolate_tt(monkeypatch, None)

    def generate(current, *_masks):
        events.append("generated")
        return current.is_check(), iter(list(current.legal_moves))

    monkeypatch.setattr(stockconda.cython_chess, "generate_search_moves", generate)
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            events.append(current.move_stack[-1].uci())
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    engine.pvs_search(board, 1, 0, 100, tt_context=(11, 22))
    assert events == ["generated", "generated", *(move.uci() for move in expected)]


def test_illegal_tt_hint_falls_back_to_python_chess_legal_order(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches dispatching an unvalidated move-only TT hint."""

    board = chess.Board()
    engine = bare_engine()
    illegal = chess.Move.from_uci("e2e5")
    entry = capture_entry_ordering(board, engine, 0)
    expected = baseline_order(board, entry, illegal)
    visits: list[chess.Move] = []
    events: list[str] = []
    isolate_tt(monkeypatch, illegal)

    def generate(current, *_masks):
        events.append("generated")
        return current.is_check(), iter(list(current.legal_moves))

    monkeypatch.setattr(stockconda.cython_chess, "generate_search_moves", generate)
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            visits.append(current.move_stack[-1])
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    engine.pvs_search(board, 1, 0, 100, tt_context=(11, 22))
    assert events == ["generated", "generated"]
    assert tuple(visits) == expected
    assert illegal not in visits


def test_history_ranking_is_frozen_before_tt_child_but_live_updates_survive(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches dynamic history reads and restoring the entry snapshot to live state."""

    board = chess.Board()
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    high = chess.Move.from_uci("d2d4")
    low = chess.Move.from_uci("g1f3")
    moves = [tt_move, high, low]
    entry_turn = board.turn
    engine.history_table[high.from_square][high.to_square][entry_turn] = 80
    engine.history_table[low.from_square][low.to_square][entry_turn] = 20
    expected = baseline_order(board, entry_for_moves(board, engine, 0, moves), tt_move)
    assert expected == tuple(moves)
    isolate_tt(monkeypatch, tt_move)
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        selected_generator(lambda _board: moves.copy()),
    )
    real = UCIEngine.pvs_search.__get__(engine)
    visits: list[chess.Move] = []

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            root_move = current.move_stack[-1]
            visits.append(root_move)
            if root_move == tt_move:
                engine.history_table[high.from_square][high.to_square][entry_turn] = 1
                engine.history_table[low.from_square][low.to_square][entry_turn] = 900
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    engine.pvs_search(board, 1, 0, 100, tt_context=(11, 22))
    assert tuple(visits) == expected
    assert engine.history_table[low.from_square][low.to_square][entry_turn] == 900
    assert engine.history_table[high.from_square][high.to_square][entry_turn] == 1


def test_snapshot_rows_are_values_not_aliases(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches retaining a mutable history row for delayed decoration."""

    board = chess.Board()
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    high = chess.Move.from_uci("g1f3")
    low = chess.Move.from_uci("g1h3")
    moves = [tt_move, high, low]
    entry_turn = board.turn
    engine.history_table[high.from_square][high.to_square][entry_turn] = 70
    engine.history_table[low.from_square][low.to_square][entry_turn] = 10
    expected = baseline_order(board, entry_for_moves(board, engine, 0, moves), tt_move)
    isolate_tt(monkeypatch, tt_move)
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        selected_generator(lambda _board: moves.copy()),
    )
    real = UCIEngine.pvs_search.__get__(engine)
    visits: list[chess.Move] = []

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            root_move = current.move_stack[-1]
            visits.append(root_move)
            if root_move == tt_move:
                row = engine.history_table[high.from_square]
                row[high.to_square][entry_turn] = 0
                row[low.to_square][entry_turn] = 500
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    engine.pvs_search(board, 1, 0, 100, tt_context=(11, 22))
    assert tuple(visits) == expected


def test_killers_are_frozen_and_capture_priority_wins(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches late killer reads and killer-before-capture classification."""

    board = chess.Board("7k/8/8/3p4/8/2N5/8/KR6 w - - 0 1")
    engine = bare_engine()
    tt_move = chess.Move.from_uci("b1b2")
    capture = chess.Move.from_uci("c3d5")
    killer = chess.Move.from_uci("c3b5")
    other = chess.Move.from_uci("c3e4")
    moves = [tt_move, capture, killer, other]
    duplicate_entry = EntryOrdering(
        board.turn,
        (killer, killer),
        capture_entry_ordering(board, engine, 0).history,
        tuple(moves),
    )
    assert baseline_order(board, duplicate_entry, tt_move).count(killer) == 1
    engine.killer_moves[0] = [capture, killer]
    entry = entry_for_moves(board, engine, 0, moves)
    expected = baseline_order(board, entry, tt_move)
    assert expected == (tt_move, capture, killer, other)
    isolate_tt(monkeypatch, tt_move)
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        selected_generator(lambda _board: moves.copy()),
    )
    real = UCIEngine.pvs_search.__get__(engine)
    visits: list[chess.Move] = []

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            root_move = current.move_stack[-1]
            visits.append(root_move)
            if root_move == tt_move:
                engine.killer_moves[0] = [other, other]
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    engine.pvs_search(board, 1, 0, 100, tt_context=(11, 22))
    assert tuple(visits) == expected


def test_nested_tt_nodes_keep_private_entry_snapshots(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches ancestor/descendant aliasing across same-color history updates."""

    parent = chess.Board()
    grandchild = chess.Board()
    engine = bare_engine()
    parent_tt = chess.Move.from_uci("e2e4")
    nested_tt = chess.Move.from_uci("d2d4")
    first = chess.Move.from_uci("g1f3")
    second = chess.Move.from_uci("g1h3")
    engine.history_table[first.from_square][first.to_square][chess.WHITE] = 100
    engine.history_table[second.from_square][second.to_square][chess.WHITE] = 10
    parent_expected = baseline_order(
        parent,
        entry_for_moves(parent, engine, 0, [parent_tt, first, second]),
        parent_tt,
    )
    nested_expected: list[tuple[chess.Move, ...]] = []

    def hint(current: chess.Board):
        return nested_tt if current is grandchild else parent_tt

    isolate_tt(monkeypatch, hint)

    def moves_for(current: chess.Board):
        if current is grandchild:
            return [nested_tt, first, second]
        return [parent_tt, first, second]

    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        selected_generator(moves_for),
    )
    real = UCIEngine.pvs_search.__get__(engine)
    parent_visits: list[chess.Move] = []
    nested_visits: list[chess.Move] = []
    nested_started = False

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        nonlocal nested_started
        if current is parent and ply:
            move = current.move_stack[-1]
            parent_visits.append(move)
            if move == parent_tt and not nested_started:
                nested_started = True
                engine.history_table[first.from_square][first.to_square][chess.WHITE] = 5
                engine.history_table[second.from_square][second.to_square][chess.WHITE] = 50
                nested_expected.append(
                    baseline_order(
                        grandchild,
                        entry_for_moves(
                            grandchild, engine, 2, [nested_tt, first, second]
                        ),
                        nested_tt,
                    )
                )
                real(grandchild, 1, 0, 100, 0, 2, tt_context=(33, 44))
            return 0, None
        if current is grandchild and ply == 3:
            move = current.move_stack[-1]
            nested_visits.append(move)
            if move == nested_tt:
                engine.history_table[first.from_square][first.to_square][chess.WHITE] = 900
                engine.history_table[second.from_square][second.to_square][chess.WHITE] = 0
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    engine.pvs_search(parent, 1, 0, 100, 0, 0, tt_context=(11, 22))
    assert tuple(parent_visits) == parent_expected
    assert nested_expected == [(nested_tt, second, first)]
    assert tuple(nested_visits) == nested_expected[0]
    assert engine.history_table[first.from_square][first.to_square][chess.WHITE] == 900


def test_remainder_uses_global_lmr_index_and_research_windows(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches resetting the remainder index and changing PVS re-search windows."""

    board = chess.Board()
    before = board.fen(), tuple(board.move_stack)
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    remainder = chess.Move.from_uci("d2d4")
    engine.lmr_reduction_table[5][0] = 3
    engine.lmr_reduction_table[5][1] = 1
    isolate_tt(monkeypatch, tt_move)
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        selected_generator(lambda _board: [tt_move, remainder]),
    )
    real = UCIEngine.pvs_search.__get__(engine)
    calls: list[tuple[chess.Move, int, int, int]] = []

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            move = current.move_stack[-1]
            calls.append((move, depth, alpha, beta))
            if move == remainder:
                return -5, None
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    engine.pvs_search(board, 5, -10, 100, 0, 0, tt_context=(11, 22))
    assert calls == [
        (tt_move, 4, -100, 10),
        (remainder, 3, -1, 0),
        (remainder, 4, -100, 0),
    ]
    assert (board.fen(), tuple(board.move_stack)) == before


def test_checked_tt_continuation_keeps_full_depth_for_quiet_remainder(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches losing entry check status and applying LMR to a checked remainder."""

    board = chess.Board("k3r3/8/8/8/8/8/8/4KB2 w - - 0 1")
    before = board.fen(), tuple(board.move_stack)
    assert board.is_check()
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e1f2")
    remainder = chess.Move.from_uci("e1d2")
    moves = [tt_move, remainder]
    assert all(not board.is_capture(move) and not move.promotion for move in moves)
    engine.lmr_reduction_table[5][1] = 3
    isolate_tt(monkeypatch, tt_move)

    def generate(current, *_masks):
        assert current is board and current.is_check()
        return True, iter(moves)

    monkeypatch.setattr(stockconda.cython_chess, "generate_search_moves", generate)
    real = UCIEngine.pvs_search.__get__(engine)
    calls: list[tuple[chess.Move, int]] = []

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            calls.append((current.move_stack[-1], depth))
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    engine.pvs_search(board, 5, 0, 100, 0, 0, tt_context=(11, 22))
    assert calls == [(tt_move, 4), (remainder, 4)]
    assert (board.fen(), tuple(board.move_stack)) == before


@pytest.mark.parametrize(
    "fen,expected",
    [
        pytest.param(
            "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1",
            (-MATE_SCORE + 1, None),
            id="checkmate",
        ),
        pytest.param(
            "7k/5K2/6Q1/8/8/8/8/8 b - - 0 1",
            (0, None),
            id="stalemate",
        ),
    ],
)
def test_real_pvs_returns_terminal_mate_and_stalemate(
    monkeypatch: pytest.MonkeyPatch,
    fen: str,
    expected: tuple[int, None],
):
    """Catches treating a genuinely empty eager node as a searched-TT continuation."""

    board = chess.Board(fen)
    before = board.fen(), tuple(board.move_stack)
    engine = bare_engine()
    isolate_tt(monkeypatch, None)
    assert engine.pvs_search(board, 1, -MATE_SCORE, MATE_SCORE, tt_context=(11, 22)) == expected
    assert (board.fen(), tuple(board.move_stack)) == before


def test_fixed_depth_microtrace_matches_literal_search_contract(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches score/move/visit/history/killer drift in a deterministic depth-2 tree."""

    board = chess.Board()
    before = board.fen(), tuple(board.move_stack)
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    cutoff = chess.Move.from_uci("d2d4")
    moves = [tt_move, cutoff]
    stores: list[tuple[object, ...]] = []
    isolate_tt(monkeypatch, tt_move, stores)
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        selected_generator(lambda _board: moves.copy()),
    )
    real = UCIEngine.pvs_search.__get__(engine)
    visits: list[tuple[chess.Move, int, int, int]] = []

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            move = current.move_stack[-1]
            visits.append((move, depth, alpha, beta))
            return (-10, None) if move == cutoff else (0, None)
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    result = engine.pvs_search(board, 2, -10, 5, 3, 0, tt_context=(11, 22))
    assert result == (10, cutoff)
    assert visits == [
        (tt_move, 1, -5, 10),
        (cutoff, 1, -1, 0),
        (cutoff, 1, -5, 0),
    ]
    assert engine.history_table[tt_move.from_square][tt_move.to_square][chess.WHITE] == 1
    assert engine.history_table[cutoff.from_square][cutoff.to_square][chess.WHITE] == 1
    assert engine.killer_moves[0] == [cutoff, None]
    assert len(stores) == 1
    assert (board.fen(), tuple(board.move_stack)) == before


def test_generator_is_exhausted_before_any_remainder_push(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches suspending a board-reading native iterator across mutation."""

    board = chess.Board()
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    remainder = chess.Move.from_uci("d2d4")
    isolate_tt(monkeypatch, tt_move)
    exhausted = False

    def generate(current, *_masks):
        def iterator():
            nonlocal exhausted
            yield tt_move
            yield remainder
            exhausted = True

        return current.is_check(), iterator()

    monkeypatch.setattr(stockconda.cython_chess, "generate_search_moves", generate)
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            if current.move_stack[-1] == remainder:
                assert exhausted
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    engine.pvs_search(board, 1, 0, 100, tt_context=(11, 22))
    assert exhausted


def test_sole_legal_tt_move_continuation_is_not_reclassified_terminal(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches treating an empty post-TT remainder as mate or stalemate."""

    board = chess.Board("7k/8/5K2/6Q1/8/8/8/8 b - - 0 1")
    only = chess.Move.from_uci("h8h7")
    assert tuple(board.legal_moves) == (only,)
    engine = bare_engine()
    isolate_tt(monkeypatch, only)
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        selected_generator(lambda _board: [only]),
    )
    real = UCIEngine.pvs_search.__get__(engine)
    visits: list[chess.Move] = []

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            visits.append(current.move_stack[-1])
            return -17, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    assert engine.pvs_search(board, 1, -100, 100, tt_context=(11, 22)) == (
        17,
        only,
    )
    assert visits == [only]


@pytest.mark.parametrize("outcome", ["cancel", "exception"])
def test_tt_child_unwinds_board_and_does_not_publish_unfinished_node(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
):
    """Catches a missing TT-child finally/pop or a TT store on interruption."""

    board = chess.Board()
    before = board.fen(), tuple(board.move_stack)
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    stores: list[tuple[object, ...]] = []
    isolate_tt(monkeypatch, tt_move, stores)
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        selected_generator(lambda _board: [tt_move]),
    )
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            assert current.move_stack[-1] == tt_move
            if outcome == "exception":
                raise RuntimeError("child failure")
            return None, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    if outcome == "exception":
        with pytest.raises(RuntimeError, match="child failure"):
            engine.pvs_search(board, 1, -100, 100, tt_context=(11, 22))
    else:
        assert engine.pvs_search(board, 1, -100, 100, tt_context=(11, 22)) == (
            None,
            None,
        )
    assert stores == []
    assert (board.fen(), tuple(board.move_stack)) == before


@pytest.mark.parametrize("failure_site", ["tt_child", "remainder_factory", "remainder_iterator"])
def test_retained_exception_releases_ordering_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
):
    """Catches snapshots kept alive by the worker's retained failure traceback."""

    board = chess.Board()
    before = board.fen(), tuple(board.move_stack)
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    remainder = chess.Move.from_uci("d2d4")
    stores: list[tuple[object, ...]] = []
    isolate_tt(monkeypatch, tt_move, stores)
    visits: list[chess.Move] = []

    def generate(current, *_masks):
        assert visits == [tt_move]
        assert (current.fen(), tuple(current.move_stack)) == before
        if failure_site == "remainder_factory":
            raise RuntimeError("retained search failure")

        def broken_iterator():
            yield tt_move
            yield remainder
            raise RuntimeError("retained search failure")

        return current.is_check(), broken_iterator()

    monkeypatch.setattr(stockconda.cython_chess, "generate_search_moves", generate)
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            visits.append(current.move_stack[-1])
            if failure_site == "tt_child":
                raise RuntimeError("retained search failure")
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    retained_failure = None
    try:
        engine.pvs_search(board, 1, -100, 100, tt_context=(11, 22))
    except RuntimeError as exc:
        # Like SearchWorker._failure, preserve the exception after unwinding.
        retained_failure = exc

    assert retained_failure is not None
    assert str(retained_failure) == "retained search failure"
    assert visits == [tt_move]
    assert stores == []
    assert (board.fen(), tuple(board.move_stack)) == before

    def contains_ordering_payload(value):
        if isinstance(value, stockconda._OrderingSnapshot):
            return True
        if isinstance(value, tuple):
            # Detect a detached immutable history row as well as the snapshot.
            if len(value) == 64 and all(isinstance(item, int) for item in value):
                return True
            return any(contains_ordering_payload(item) for item in value)
        return False

    frames = set()
    retained_locals = []
    traceback = retained_failure.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code.co_filename == real.__func__.__code__.co_filename:
            frames.add(frame.f_code.co_name)
            retained_locals.extend(
                (frame.f_code.co_name, name)
                for name, value in frame.f_locals.items()
                if contains_ordering_payload(value)
            )
        traceback = traceback.tb_next
    assert "pvs_search" in frames
    assert retained_locals == []


def test_cancellation_after_tt_child_skips_remainder_preparation(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches preparing delayed work after a child requested cancellation."""

    board = chess.Board()
    before = board.fen(), tuple(board.move_stack)
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    stores: list[tuple[object, ...]] = []
    isolate_tt(monkeypatch, tt_move, stores)

    def forbidden_generation(*_args, **_kwargs):
        raise AssertionError("cancelled TT node prepared the remainder")

    monkeypatch.setattr(
        stockconda.cython_chess, "generate_search_moves", forbidden_generation
    )
    real = UCIEngine.pvs_search.__get__(engine)
    checks = 0

    def stop_check():
        nonlocal checks
        checks += 1
        return checks >= 3

    engine.stop_flag_check = stop_check

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    assert engine.pvs_search(board, 1, -100, 100, tt_context=(11, 22)) == (
        None,
        None,
    )
    assert stores == []
    assert (board.fen(), tuple(board.move_stack)) == before


def test_remainder_consumption_error_occurs_after_restored_tt_child(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches eager remainder consumption before the legal TT child."""

    board = chess.Board()
    before = board.fen(), tuple(board.move_stack)
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    stores: list[tuple[object, ...]] = []
    isolate_tt(monkeypatch, tt_move, stores)
    tt_finished = False

    def generate(current, *_masks):
        def broken_iterator():
            assert tt_finished
            yield tt_move
            raise RuntimeError("remainder consumption failure")

        return current.is_check(), broken_iterator()

    monkeypatch.setattr(stockconda.cython_chess, "generate_search_moves", generate)
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        nonlocal tt_finished
        if ply:
            tt_finished = True
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    with pytest.raises(RuntimeError, match="remainder consumption failure"):
        engine.pvs_search(board, 1, -100, 100, tt_context=(11, 22))
    assert tt_finished
    assert stores == []
    assert (board.fen(), tuple(board.move_stack)) == before


@pytest.mark.parametrize("outcome", ["cancel", "exception"])
def test_remainder_search_unwinds_and_does_not_store(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
):
    """Catches leaking a remainder push or publishing an unfinished node."""

    board = chess.Board()
    before = board.fen(), tuple(board.move_stack)
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    remainder = chess.Move.from_uci("d2d4")
    stores: list[tuple[object, ...]] = []
    isolate_tt(monkeypatch, tt_move, stores)
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        selected_generator(lambda _board: [tt_move, remainder]),
    )
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            if current.move_stack[-1] == remainder:
                if outcome == "exception":
                    raise RuntimeError("remainder child failure")
                return None, None
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    if outcome == "exception":
        with pytest.raises(RuntimeError, match="remainder child failure"):
            engine.pvs_search(board, 1, -100, 100, tt_context=(11, 22))
    else:
        assert engine.pvs_search(board, 1, -100, 100, tt_context=(11, 22)) == (
            None,
            None,
        )
    assert stores == []
    assert (board.fen(), tuple(board.move_stack)) == before


def test_cancellation_during_remainder_consumption_stops_before_remainder_push(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches consuming the remainder before TT search or pushing after cancellation."""

    board = chess.Board()
    before = board.fen(), tuple(board.move_stack)
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    remainder = chess.Move.from_uci("d2d4")
    stores: list[tuple[object, ...]] = []
    isolate_tt(monkeypatch, tt_move, stores)
    visits: list[chess.Move] = []

    def generate(current, *_masks):
        def cancelling_iterator():
            yield tt_move
            engine.stop_flag = True
            yield remainder

        return current.is_check(), cancelling_iterator()

    monkeypatch.setattr(stockconda.cython_chess, "generate_search_moves", generate)
    engine.stop_flag_check = lambda: engine.stop_flag
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            visits.append(current.move_stack[-1])
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    assert engine.pvs_search(board, 1, -100, 100, tt_context=(11, 22)) == (
        None,
        None,
    )
    assert visits == [tt_move]
    assert stores == []
    assert (board.fen(), tuple(board.move_stack)) == before


def test_tt_hint_is_probed_once_even_when_child_mutates_tt(
    monkeypatch: pytest.MonkeyPatch,
):
    """Catches re-probing a hint after recursive TT replacement."""

    board = chess.Board()
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    replacement = chess.Move.from_uci("g1f3")
    current_hint = [tt_move]
    probes = isolate_tt(monkeypatch, lambda _board: current_hint[0])
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        selected_generator(lambda _board: [tt_move, chess.Move.from_uci("d2d4")]),
    )
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(current, depth, alpha, beta, lmr_depth=3, ply=0, *, tt_context=None):
        if ply:
            current_hint[0] = replacement
            return 0, None
        return real(
            current, depth, alpha, beta, lmr_depth, ply, tt_context=tt_context
        )

    engine.pvs_search = dispatch
    engine.pvs_search(board, 1, 0, 100, tt_context=(11, 22))
    assert probes == [board]
    assert current_hint == [replacement]


def _assert_same_order(expected: tuple[chess.Move, ...], actual: tuple[chess.Move, ...]):
    assert actual == expected


def test_negative_control_dynamic_history_is_killed():
    board = chess.Board()
    engine = bare_engine()
    first = chess.Move.from_uci("g1f3")
    second = chess.Move.from_uci("g1h3")
    engine.history_table[first.from_square][first.to_square][board.turn] = 50
    expected = baseline_order(board, capture_entry_ordering(board, engine, 0), None)
    engine.history_table[first.from_square][first.to_square][board.turn] = 0
    engine.history_table[second.from_square][second.to_square][board.turn] = 100
    dynamic = baseline_order(board, capture_entry_ordering(board, engine, 0), None)
    with pytest.raises(AssertionError):
        _assert_same_order(expected, dynamic)


def test_negative_control_aliased_snapshot_row_is_killed():
    board = chess.Board()
    engine = bare_engine()
    first = chess.Move.from_uci("g1f3")
    second = chess.Move.from_uci("g1h3")
    row_alias = engine.history_table[first.from_square]
    row_alias[first.to_square][board.turn] = 40
    expected = baseline_order(board, capture_entry_ordering(board, engine, 0), None)
    row_alias[first.to_square][board.turn] = 0
    row_alias[second.to_square][board.turn] = 80
    aliased = baseline_order(board, capture_entry_ordering(board, engine, 0), None)
    with pytest.raises(AssertionError):
        _assert_same_order(expected, aliased)


def test_negative_control_missing_capture_history_tiebreak_is_killed():
    board = chess.Board("7k/8/8/3pp3/8/2N2N2/8/K7 w - - 0 1")
    engine = bare_engine()
    first = chess.Move.from_uci("c3d5")
    second = chess.Move.from_uci("f3e5")
    initial = capture_entry_ordering(board, engine, 0)
    capture_order = [move for move in initial.legal_moves if move in (first, second)]
    assert len(capture_order) == 2
    later = capture_order[1]
    engine.history_table[later.from_square][later.to_square][board.turn] = 100
    entry = capture_entry_ordering(board, engine, 0)
    expected = baseline_order(board, entry, None)
    mutant = tuple(capture_order + [move for move in expected if move not in capture_order])
    with pytest.raises(AssertionError):
        _assert_same_order(expected, mutant)


def test_negative_control_duplicate_tt_is_killed():
    board = chess.Board()
    engine = bare_engine()
    tt_move = chess.Move.from_uci("e2e4")
    expected = baseline_order(board, capture_entry_ordering(board, engine, 0), tt_move)
    mutant = (tt_move, *expected)
    with pytest.raises(AssertionError):
        _assert_same_order(expected, mutant)


def test_negative_control_reset_remainder_index_is_killed():
    board = chess.Board()
    tt_move = chess.Move.from_uci("e2e4")
    remainder = chess.Move.from_uci("d2d4")
    reduction_by_index = (3, 1)

    def coordinate(reset_remainder_index: bool):
        trace: list[tuple[chess.Move, int]] = [(tt_move, 4)]
        global_index = 1
        for local_index, move in enumerate((remainder,)):
            index = local_index if reset_remainder_index else global_index
            reduced_depth = 4 - reduction_by_index[index]
            trace.append((move, reduced_depth))
            global_index += 1
        return tuple(trace)

    expected_trace = ((tt_move, 4), (remainder, 3))
    assert coordinate(False) == expected_trace
    with pytest.raises(AssertionError):
        assert coordinate(True) == expected_trace


def test_negative_control_eager_remainder_is_killed():
    events: list[str] = []
    tt_finished = False

    def remainder():
        events.append("remainder-start")
        assert tt_finished, "remainder prepared before TT cutoff"
        yield chess.Move.from_uci("d2d4")

    def coordinate(eager_remainder: bool):
        nonlocal tt_finished
        iterator = remainder()
        if eager_remainder:
            list(iterator)
        events.append("tt-search")
        tt_finished = True
        events.append("tt-cutoff")
        if not eager_remainder:
            return
        list(iterator)

    with pytest.raises(AssertionError, match="before TT cutoff"):
        coordinate(True)
    assert events == ["remainder-start"]


def test_negative_control_restoring_snapshot_into_live_history_is_killed():
    board = chess.Board()
    engine = bare_engine()
    first = chess.Move.from_uci("g1f3")
    second = chess.Move.from_uci("g1h3")
    entry = capture_entry_ordering(board, engine, 0)
    live_updates = {first: 70, second: 30}
    for move, value in live_updates.items():
        engine.history_table[move.from_square][move.to_square][entry.turn] = value

    def restore_snapshot_mutant():
        for move in live_updates:
            engine.history_table[move.from_square][move.to_square][entry.turn] = (
                entry.history[move.from_square][move.to_square]
            )

    restore_snapshot_mutant()
    with pytest.raises(AssertionError):
        assert {
            move: engine.history_table[move.from_square][move.to_square][entry.turn]
            for move in live_updates
        } == live_updates


def test_negative_control_reversed_stable_ties_is_killed():
    board = chess.Board("7k/8/8/3pp3/8/2N2N2/8/K7 w - - 0 1")
    engine = bare_engine()
    captures = tuple(
        move
        for move in board.legal_moves
        if move in (chess.Move.from_uci("c3d5"), chess.Move.from_uci("f3e5"))
    )
    entry = entry_for_moves(board, engine, 0, list(captures))
    expected = baseline_order(board, entry, None)

    def uci_tiebreak_mutant():
        return tuple(sorted(captures, key=lambda move: move.uci()))

    assert expected == captures
    with pytest.raises(AssertionError):
        _assert_same_order(expected, uci_tiebreak_mutant())


def test_negative_control_killer_before_capture_is_killed():
    board = chess.Board("7k/8/8/3p4/8/2N5/8/K7 w - - 0 1")
    engine = bare_engine()
    capture = chess.Move.from_uci("c3d5")
    quiet_killer = chess.Move.from_uci("c3b5")
    engine.killer_moves[0] = [quiet_killer, capture]
    entry = entry_for_moves(board, engine, 0, [quiet_killer, capture])
    expected = baseline_order(board, entry, None)

    def killer_first_priority(move: chess.Move):
        if move == entry.killers[0]:
            return 6000
        if move == entry.killers[1]:
            return 5000
        if board.is_capture(move):
            return 10000
        return 0

    mutant = tuple(
        sorted(entry.legal_moves, key=lambda move: -killer_first_priority(move))
    )
    assert expected == (capture, quiet_killer)
    with pytest.raises(AssertionError):
        _assert_same_order(expected, mutant)


def test_negative_control_iterator_across_board_mutation_is_killed():
    board = chess.Board()
    before = board.fen(), tuple(board.move_stack)
    first = chess.Move.from_uci("e2e4")
    second = chess.Move.from_uci("d2d4")

    def board_reading_iterator():
        yield first
        assert (board.fen(), tuple(board.move_stack)) == before
        yield second

    def suspended_iterator_mutant():
        iterator = board_reading_iterator()
        move = next(iterator)
        board.push(move)
        try:
            return (move, *tuple(iterator))
        finally:
            board.pop()

    with pytest.raises(AssertionError):
        suspended_iterator_mutant()
    assert (board.fen(), tuple(board.move_stack)) == before

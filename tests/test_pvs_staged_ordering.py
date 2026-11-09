"""AP-04B staged PVS ordering contracts.

Expected capture subsequences are derived from python-chess legal moves, not
from candidate ordering helpers.  Behavioral RED tests are added below this
oracle before their corresponding production change.
"""

from __future__ import annotations

import random
from collections import Counter

import chess
import pytest

import chess_engine.stockconda as stockconda
from chess_engine.cython_ext import cython_chess
from chess_engine.stockconda import HistoryTable, UCIEngine
from chess_engine.uci_search_worker import SearchWorker


CAPTURE_FIXTURES = (
    chess.STARTING_FEN,
    "k3r3/8/8/8/8/8/8/4KB2 w - - 0 1",
    "k3r3/8/8/8/1b6/8/8/4K3 w - - 0 1",
    "4k3/4r3/8/8/8/8/4R3/4K3 w - - 0 1",
    "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
    "7k/8/8/3pP3/8/8/8/K7 w - d6 0 1",
    "4k2r/6P1/8/8/8/8/8/4K3 w - - 0 1",
    "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1",
    "7k/5K2/6Q1/8/8/8/8/8 b - - 0 1",
)


def proposed_capture_subsequence(
    board: chess.Board,
) -> tuple[bool, tuple[chess.Move, ...]]:
    """Exercise the proposed production composition without candidate code."""

    in_check, normal = cython_chess.generate_search_moves(
        board,
        chess.BB_ALL,
        board.occupied_co[not board.turn],
    )
    captures = tuple(normal) + tuple(
        cython_chess.generate_legal_ep(board, chess.BB_ALL, chess.BB_ALL)
    )
    return in_check, captures


def assert_capture_subsequence(board: chess.Board) -> None:
    before = board.fen(), tuple(board.move_stack)
    expected = tuple(move for move in board.legal_moves if board.is_capture(move))

    in_check, actual = proposed_capture_subsequence(board)

    assert in_check is board.is_check()
    assert actual == expected
    assert len(actual) == len(set(actual))
    assert (board.fen(), tuple(board.move_stack)) == before


@pytest.mark.parametrize("fen", CAPTURE_FIXTURES)
def test_masked_capture_plus_ep_is_exact_full_legal_capture_subsequence(
    fen: str,
) -> None:
    """Catches missing, duplicated or reordered masked/EP captures."""

    assert_capture_subsequence(chess.Board(fen))


def test_masked_capture_plus_ep_matches_deterministic_legal_playouts() -> None:
    """Catches a capture-subsequence mismatch outside hand-picked positions."""

    rng = random.Random(20260918)
    board = chess.Board()
    checked = 0
    for _ in range(400):
        assert_capture_subsequence(board)
        checked += 1
        legal = tuple(board.legal_moves)
        if not legal:
            board.reset()
            continue
        board.push(legal[rng.randrange(len(legal))])
        if board.ply() >= 100:
            board.reset()

    assert checked == 400


def bare_engine() -> UCIEngine:
    """Construct the real PVS owner without a worker or allocated TT."""

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


def isolate_empty_tt(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[object, ...]]:
    stores: list[tuple[object, ...]] = []
    monkeypatch.setattr(stockconda, "probe_tt_entry", lambda *_a, **_kw: None)
    monkeypatch.setattr(stockconda, "probe_tt_move", lambda *_a, **_kw: (None, None))
    monkeypatch.setattr(
        stockconda,
        "store_tt_entry",
        lambda *args, **_kwargs: stores.append(args),
    )
    return stores


def test_capture_cutoff_does_not_prepare_killers_or_other_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches eager full generation before a capture beta cutoff."""

    board = chess.Board("7k/8/8/3p4/8/2N5/8/K7 w - - 0 1")
    capture = chess.Move.from_uci("c3d5")
    engine = bare_engine()
    stores = isolate_empty_tt(monkeypatch)
    calls: list[int] = []

    def staged_generator(current: chess.Board, _from: int, to_mask: int):
        assert current is board
        calls.append(to_mask)
        if to_mask == current.occupied_co[not current.turn]:
            return current.is_check(), iter((capture,))
        raise AssertionError("Other moves prepared before capture cutoff")

    monkeypatch.setattr(stockconda.cython_chess, "generate_search_moves", staged_generator)
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_legal_ep",
        lambda *_args: iter(()),
    )
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(
        current: chess.Board,
        depth: int,
        alpha: int,
        beta: int,
        lmr_depth: int = 3,
        ply: int = 0,
        *,
        tt_context: tuple[int, int] | None = None,
    ):
        if ply:
            assert current.move_stack[-1] == capture
            return -100, None
        return real(
            current,
            depth,
            alpha,
            beta,
            lmr_depth,
            ply,
            tt_context=tt_context,
        )

    engine.pvs_search = dispatch
    assert engine.pvs_search(board, 1, -200, 50, tt_context=(11, 22)) == (
        100,
        capture,
    )
    assert calls == [board.occupied_co[chess.BLACK]]
    assert stores and stores[-1][4] == capture


def test_killer_cutoff_does_not_prepare_other_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches eager full generation before a quiet killer beta cutoff."""

    board = chess.Board()
    killer = chess.Move.from_uci("e2e4")
    engine = bare_engine()
    engine.killer_moves[0] = [killer, None]
    stores = isolate_empty_tt(monkeypatch)
    calls: list[int] = []

    def staged_generator(current: chess.Board, _from: int, to_mask: int):
        assert current is board
        calls.append(to_mask)
        if to_mask == current.occupied_co[not current.turn]:
            return current.is_check(), iter(())
        raise AssertionError("Other moves prepared before killer cutoff")

    monkeypatch.setattr(stockconda.cython_chess, "generate_search_moves", staged_generator)
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_legal_ep",
        lambda *_args: iter(()),
    )
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(
        current: chess.Board,
        depth: int,
        alpha: int,
        beta: int,
        lmr_depth: int = 3,
        ply: int = 0,
        *,
        tt_context: tuple[int, int] | None = None,
    ):
        if ply:
            assert current.move_stack[-1] == killer
            return -100, None
        return real(
            current,
            depth,
            alpha,
            beta,
            lmr_depth,
            ply,
            tt_context=tt_context,
        )

    engine.pvs_search = dispatch
    assert engine.pvs_search(board, 1, -200, 50, tt_context=(11, 22)) == (
        100,
        killer,
    )
    assert calls == [board.occupied_co[chess.BLACK]]
    assert stores and stores[-1][4] == killer


def test_quiet_only_node_uses_eager_other_path_without_history_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches copying entry history when no early stage can mutate it."""

    board = chess.Board()
    engine = bare_engine()
    isolate_empty_tt(monkeypatch)
    snapshot_calls = 0
    original_snapshot = engine.history_table.snapshot

    def counting_snapshot(turn: chess.Color, occupied: int):
        nonlocal snapshot_calls
        snapshot_calls += 1
        return original_snapshot(turn, occupied)

    monkeypatch.setattr(engine.history_table, "snapshot", counting_snapshot)
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(
        current: chess.Board,
        depth: int,
        alpha: int,
        beta: int,
        lmr_depth: int = 3,
        ply: int = 0,
        *,
        tt_context: tuple[int, int] | None = None,
    ):
        if ply:
            return 0, None
        return real(
            current,
            depth,
            alpha,
            beta,
            lmr_depth,
            ply,
            tt_context=tt_context,
        )

    engine.pvs_search = dispatch
    value, move = engine.pvs_search(board, 1, -100, 100, tt_context=(11, 22))

    assert value == 0
    assert move in chess.Board().legal_moves
    assert snapshot_calls == 0


def test_duplicate_killer_slots_search_each_legal_move_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches emitting the same frozen killer from both slots."""

    board = chess.Board()
    before = board.fen(), tuple(board.move_stack)
    killer = chess.Move.from_uci("e2e4")
    engine = bare_engine()
    engine.killer_moves[0] = [killer, killer]
    isolate_empty_tt(monkeypatch)
    visits: list[chess.Move] = []
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(
        current: chess.Board,
        depth: int,
        alpha: int,
        beta: int,
        lmr_depth: int = 3,
        ply: int = 0,
        *,
        tt_context: tuple[int, int] | None = None,
    ):
        if ply:
            visits.append(current.move_stack[-1])
            return 0, None
        return real(
            current,
            depth,
            alpha,
            beta,
            lmr_depth,
            ply,
            tt_context=tt_context,
        )

    engine.pvs_search = dispatch
    engine.pvs_search(board, 1, -100, 100, tt_context=(11, 22))

    counts = Counter(visits)
    assert set(counts) == set(chess.Board().legal_moves)
    assert set(counts.values()) == {1}
    assert visits[0] == killer
    assert (board.fen(), tuple(board.move_stack)) == before


def test_retained_child_exception_releases_stage_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches capture/killer lists retained by the worker's saved traceback."""

    board = chess.Board("7k/8/8/3p4/8/2N5/8/K7 w - - 0 1")
    before = board.fen(), tuple(board.move_stack)
    capture = chess.Move.from_uci("c3d5")
    engine = bare_engine()
    isolate_empty_tt(monkeypatch)
    real = UCIEngine.pvs_search.__get__(engine)

    def dispatch(
        current: chess.Board,
        depth: int,
        alpha: int,
        beta: int,
        lmr_depth: int = 3,
        ply: int = 0,
        *,
        tt_context: tuple[int, int] | None = None,
    ):
        if ply:
            assert current.move_stack[-1] == capture
            raise RuntimeError("retained staged child failure")
        return real(
            current,
            depth,
            alpha,
            beta,
            lmr_depth,
            ply,
            tt_context=tt_context,
        )

    engine.pvs_search = dispatch
    retained: RuntimeError | None = None
    try:
        engine.pvs_search(board, 1, -100, 100, tt_context=(11, 22))
    except RuntimeError as exc:
        retained = exc

    assert retained is not None
    leaked: list[str] = []
    traceback = retained.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code.co_filename == real.__func__.__code__.co_filename:
            for name in ("captures", "killer_candidates"):
                value = frame.f_locals.get(name)
                if value:
                    leaked.append(name)
        traceback = traceback.tb_next

    assert leaked == []
    assert (board.fen(), tuple(board.move_stack)) == before


def assert_worker_failure_releases_helper_locals(
    operation,
    helper: str,
    expected: dict[str, object],
) -> BaseException:
    """Retain a real worker failure and inspect the named production frame."""

    worker = SearchWorker(lambda _task: operation())
    try:
        task = worker.submit(chess.STARTING_FEN, None, 1, False)
        assert task.completed.wait(2)
        failure = worker.failure
        assert failure is not None
    finally:
        worker.shutdown()

    frames = []
    traceback = failure.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_name == helper:
            frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert len(frames) == 1
    retained = frames[0].f_locals
    assert {name: retained.get(name) for name in expected} == expected
    return failure


def test_worker_retained_capture_duplicate_releases_helper_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches capture moves/lists/sets retained by an invariant traceback."""

    board = chess.Board("7k/8/8/3p4/8/2N5/8/K7 w - - 0 1")
    capture = chess.Move.from_uci("c3d5")
    engine = bare_engine()
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        lambda *_args: (False, iter((capture,))),
    )
    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_legal_ep",
        lambda *_args: iter((capture,)),
    )

    failure = assert_worker_failure_releases_helper_locals(
        lambda: engine._generate_pvs_captures(board, None),
        "_generate_pvs_captures",
        {
            "moves_generator": None,
            "ep_generator": None,
            "moves": [],
            "captures": [],
            "seen": set(),
        },
    )
    assert str(failure) == "capture stage produced a duplicate move"


def test_worker_retained_full_iterator_failure_releases_helper_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the full iterator and its partial decoration in a traceback."""

    board = chess.Board()
    first = chess.Move.from_uci("e2e4")
    engine = bare_engine()

    def broken_iterator():
        yield first
        raise RuntimeError("full iterator failure")

    monkeypatch.setattr(
        stockconda.cython_chess,
        "generate_search_moves",
        lambda *_args: (False, broken_iterator()),
    )
    failure = assert_worker_failure_releases_helper_locals(
        lambda: engine._prepare_pvs_moves(board, 0, None),
        "_prepare_pvs_moves",
        {
            "moves_generator": None,
            "decorated_moves": [],
            "killers": (None, None),
            "history": (),
            "snapshot": None,
        },
    )
    assert str(failure) == "full iterator failure"


@pytest.mark.parametrize(
    ("helper", "operation", "expected"),
    [
        pytest.param(
            "_prepare_pvs_captures",
            lambda engine, board, snapshot, move, seen: engine._prepare_pvs_captures(
                board, (move,), stockconda._OrderingSnapshot((), snapshot.killers)
            ),
            {
                "captures": (),
                "decorated_moves": [],
                "snapshot": None,
                "history": (),
            },
            id="capture-decoration",
        ),
        pytest.param(
            "_prepare_pvs_killers",
            lambda engine, board, snapshot, move, seen: engine._prepare_pvs_killers(
                ((6000, move),), stockconda._OrderingSnapshot((), snapshot.killers)
            ),
            {
                "candidates": (),
                "decorated_moves": [],
                "snapshot": None,
                "history": (),
            },
            id="killer-decoration",
        ),
    ],
)
def test_worker_retained_decoration_failure_releases_helper_payloads(
    helper,
    operation,
    expected,
) -> None:
    """Catches caller sequences and partial output retained during decoration."""

    board = chess.Board()
    move = chess.Move.from_uci("e2e4")
    engine = bare_engine()
    snapshot = engine._ordering_snapshot(board, (move, None))
    seen: set[chess.Move] = set()

    failure = assert_worker_failure_releases_helper_locals(
        lambda: operation(engine, board, snapshot, move, seen), helper, expected
    )
    assert isinstance(failure, IndexError)
    assert snapshot.history
    assert seen == set()


@pytest.mark.parametrize(
    ("helper", "operation", "expected"),
    [
        pytest.param(
            "_prepare_pvs_captures",
            lambda engine, move: engine._prepare_pvs_captures(
                chess.Board(), (move,), None
            ),
            {"captures": (), "decorated_moves": [], "history": (), "snapshot": None},
            id="capture-missing-snapshot",
        ),
        pytest.param(
            "_prepare_pvs_killers",
            lambda engine, move: engine._prepare_pvs_killers(
                ((6000, move),), None
            ),
            {"candidates": (), "decorated_moves": [], "history": (), "snapshot": None},
            id="killer-missing-snapshot",
        ),
    ],
)
def test_worker_retained_missing_snapshot_releases_helper_inputs(
    helper,
    operation,
    expected,
) -> None:
    """Catches caller sequences retained when decoration preconditions fail."""

    engine = bare_engine()
    move = chess.Move.from_uci("e2e4")
    failure = assert_worker_failure_releases_helper_locals(
        lambda: operation(engine, move), helper, expected
    )
    assert isinstance(failure, ValueError)


def test_worker_retained_killer_invariant_releases_helper_payloads() -> None:
    """Catches copied seen/killers and partial candidates in a traceback."""

    board = chess.Board("7k/8/8/3p4/8/2N5/8/K7 w - - 0 1")
    capture = chess.Move.from_uci("c3d5")
    engine = bare_engine()
    seen_input: set[chess.Move] = set()
    failure = assert_worker_failure_releases_helper_locals(
        lambda: engine._generate_pvs_killers(
            board, (capture, None), seen_input
        ),
        "_generate_pvs_killers",
        {
            "candidates": [],
            "emitted": set(),
            "killers": (None, None),
            "seen": set(),
        },
    )
    assert str(failure) == "capture stage omitted a legal killer capture"
    assert seen_input == set()


@pytest.mark.parametrize("failure_site", ["factory", "iterator", "invariant"])
def test_worker_retained_other_failure_releases_helper_payloads(
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
) -> None:
    """Catches staged Other inputs/output for every exception boundary."""

    board = chess.Board("7k/8/8/3p4/8/2N5/8/K7 w - - 0 1")
    capture = chess.Move.from_uci("c3d5")
    quiet = chess.Move.from_uci("c3b5")
    engine = bare_engine()
    snapshot = engine._ordering_snapshot(board, (quiet, None))
    seen_input = {chess.Move.from_uci("a1a2")}

    def generate(*_args):
        if failure_site == "factory":
            raise RuntimeError("other factory failure")

        def moves():
            if failure_site == "iterator":
                yield chess.Move.from_uci("c3e4")
                raise RuntimeError("other iterator failure")
            yield capture

        return False, moves()

    monkeypatch.setattr(
        stockconda.cython_chess, "generate_search_moves", generate
    )
    failure = assert_worker_failure_releases_helper_locals(
        lambda: engine._prepare_pvs_other(
            board, (quiet, None), snapshot, seen_input
        ),
        "_prepare_pvs_other",
        {
            "moves_generator": None,
            "decorated_moves": [],
            "killers": (None, None),
            "snapshot": None,
            "history": (),
            "seen": set(),
        },
    )
    expected_message = (
        "capture stage omitted a legal capture"
        if failure_site == "invariant"
        else f"other {failure_site} failure"
    )
    assert str(failure) == expected_message
    assert snapshot.history
    assert seen_input == {chess.Move.from_uci("a1a2")}

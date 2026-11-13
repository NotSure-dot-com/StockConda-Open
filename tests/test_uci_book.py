"""The UCI Book option must affect the public search path."""

import sys
import pickle
from pathlib import Path

import chess
import pytest

from uci_harness import UciSession


def test_source_and_wheel_book_copies_match():
    root = Path(__file__).resolve().parents[1]
    assert (root / "opening_book.pkl").read_bytes() == (
        root / "src" / "chess_engine" / "opening_book.pkl").read_bytes()


def test_missing_bundled_book_does_not_load_cwd_pickle(tmp_path, monkeypatch):
    from chess_engine import opening_book

    source = tmp_path / "source" / "src" / "chess_engine"
    source.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "opening_book.pkl").write_bytes(
        pickle.dumps({"move_sequences": {(chess.Move.from_uci("e2e4"),): 1}})
    )
    monkeypatch.setattr(opening_book, "__file__", str(source / "opening_book.py"))
    monkeypatch.setattr(opening_book, "_OPENING_BOOK_CACHE", None)
    monkeypatch.chdir(elsewhere)
    assert opening_book.load_opening_book() is None


def test_installed_package_book_is_used_when_source_root_is_absent(tmp_path, monkeypatch):
    from chess_engine import opening_book

    package = tmp_path / "site-packages" / "chess_engine"
    package.mkdir(parents=True)
    (package / "opening_book.pkl").write_bytes(
        pickle.dumps({"move_sequences": {(chess.Move.from_uci("d2d4"),): 1}})
    )
    monkeypatch.setattr(opening_book, "__file__", str(package / "opening_book.py"))
    monkeypatch.setattr(opening_book, "_OPENING_BOOK_CACHE", None)
    monkeypatch.chdir(tmp_path)
    book = opening_book.load_opening_book()
    assert book is not None
    assert chess.Move.from_uci("d2d4") in book.first_moves


def test_frozen_book_comes_from_executable_directory(tmp_path, monkeypatch):
    from chess_engine import opening_book

    distribution = tmp_path / "StockConda"
    distribution.mkdir()
    (distribution / "opening_book.pkl").write_bytes(
        pickle.dumps({"move_sequences": {(chess.Move.from_uci("e2e4"),): 1}})
    )
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "opening_book.pkl").write_bytes(b"invalid")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(distribution / "StockConda.exe"))
    monkeypatch.setattr(
        opening_book,
        "__file__",
        str(distribution / "_internal" / "chess_engine" / "opening_book.py"),
    )
    monkeypatch.setattr(opening_book, "_OPENING_BOOK_CACHE", None)
    monkeypatch.chdir(unrelated)

    book = opening_book.load_opening_book()
    assert book is not None
    assert chess.Move.from_uci("e2e4") in book.first_moves


@pytest.mark.parametrize("foreign_cwd", [False, True])
def test_book_toggle_uses_bundled_move(tmp_path, foreign_cwd):
    command = None
    if foreign_cwd:
        (tmp_path / "opening_book.pkl").write_bytes(
            pickle.dumps({"move_sequences": {}})
        )
        script = (
            f"import os; os.chdir({str(tmp_path)!r}); "
            "from chess_engine.stockconda import UCIEngine; UCIEngine().run()"
        )
        command = [sys.executable, "-u", "-c", script]
    session = UciSession(command)
    try:
        session.fence()
        assert session.wait(lambda line: line == "option name Book type check default false", timeout=5)
        session.ready()

        start = session.send("setoption name Book value true")
        session.wait(lambda line: "Opening book enabled" in line, start, 10)
        session.fence()
        board = chess.Board()
        session.position(board)
        first = session.go(board, "go depth 1")
        book_line = session.wait(lambda line: line.startswith("info string book move "), first, 5)
        result = session.bestmove(first)
        assert result.split()[1] == book_line.split()[-1]
        assert chess.Move.from_uci(result.split()[1]) in board.legal_moves

        session.send("setoption name Book value false")
        session.fence()
        session.position(board)
        second = session.go(board, "go depth 1")
        session.wait(lambda line: line.startswith("info depth 1 "), second, 5)
        session.bestmove(second)
        assert not any(
            channel == "stdout" and line.startswith("info string book move ")
            for _, channel, line in session.events[second:]
        ), session.transcript()

        start = session.send("setoption name Book value true")
        session.wait(lambda line: "Opening book enabled" in line, start, 10)
        session.fence()
        session.position(board)
        third = session.go(board, "go depth 1")
        session.wait(lambda line: line.startswith("info string book move "), third, 5)
        session.bestmove(third)
        session.finish()
    finally:
        print(session.transcript())
        session.close()


@pytest.mark.parametrize("bad_book", ["missing", "corrupt"])
def test_unavailable_book_reports_and_searches(tmp_path, bad_book):
    if bad_book == "missing":
        loader = "lambda: None"
    else:
        damaged = tmp_path / "opening_book.pkl"
        damaged.write_bytes(b"not a pickle")
        loader = (
            "lambda: __import__('chess_engine.opening_book', fromlist=['OpeningBook'])"
            f".OpeningBook.load({str(damaged)!r})"
        )
    script = (
        "import chess_engine.stockconda as module; "
        f"module.load_opening_book = {loader}; module.UCIEngine().run()"
    )
    session = UciSession([sys.executable, "-u", "-c", script])
    try:
        session.fence()
        start = session.send("setoption name Book value true")
        session.wait(lambda line: "Opening book unavailable" in line, start, 5)
        session.fence()
        board = chess.Board()
        session.position(board)
        start = session.go(board, "go depth 1")
        session.wait(lambda line: line.startswith("info depth 1 "), start, 5)
        session.bestmove(start)
        session.finish()
    finally:
        print(session.transcript())
        session.close()


def test_illegal_book_result_uses_search():
    script = """
import chess
import chess_engine.stockconda as module
class BadBook:
    exhausted = False
    def get_move(self, board, temperature=1):
        return chess.Move.from_uci('e2e5')
module.load_opening_book = lambda: BadBook()
module.UCIEngine().run()
"""
    session = UciSession([sys.executable, "-u", "-c", script])
    try:
        session.fence()
        start = session.send("setoption name Book value true")
        session.wait(lambda line: "Opening book enabled" in line, start, 5)
        session.fence()
        board = chess.Board()
        session.position(board)
        start = session.go(board, "go depth 1")
        session.wait(lambda line: "illegal book move; using search" in line, start, 5)
        session.wait(lambda line: line.startswith("info depth 1 "), start, 5)
        session.bestmove(start)
        session.finish()
    finally:
        print(session.transcript())
        session.close()


def test_book_does_not_finish_infinite_search_before_stop():
    session = UciSession()
    try:
        session.fence()
        start = session.send("setoption name Book value true")
        session.wait(lambda line: "Opening book enabled" in line, start, 10)
        session.fence()
        board = chess.Board()
        session.position(board)
        start = session.go(board)
        session.ready()
        assert not any(
            channel == "stdout" and line.startswith("bestmove ")
            for _, channel, line in session.events[start:]
        ), session.transcript()
        session.stop()
        session.finish()
    finally:
        print(session.transcript())
        session.close()


def test_direct_infinite_search_does_not_take_book_move(monkeypatch):
    from chess_engine.stockconda import UCIEngine

    engine = UCIEngine()
    try:
        class Book:
            exhausted = False

            def get_move(self, _board, temperature=1):
                return chess.Move.from_uci("e2e4")

        engine.use_opening_book = True
        engine.opening_book = Book()
        searched = []
        move = chess.Move.from_uci("e2e4")

        def search(_board, depth, _alpha, _beta, *_args, **_kwargs):
            searched.append(depth)
            return 0, move

        monkeypatch.setattr(engine, "pvs_search", search)
        monkeypatch.setattr(engine, "reconstruct_pv", lambda *_args, **_kwargs: [move])
        engine.start_search(None, 1, True)
        assert searched == list(range(1, 65))
    finally:
        engine.handle_quit()


def test_book_does_not_short_circuit_ponder():
    session = UciSession()
    try:
        session.fence()
        start = session.send("setoption name Book value true")
        session.wait(lambda line: "Opening book enabled" in line, start, 10)
        session.fence()
        board = chess.Board()
        session.position(board)
        start = session.go(board, "go ponder depth 1")
        session.wait(lambda line: line.startswith("info depth 1 "), start, 5)
        assert not any(
            channel == "stdout" and line.startswith("info string book move ")
            for _, channel, line in session.events[start:]
        ), session.transcript()
        session.send("ponderhit")
        session.bestmove(start)
        session.finish()
    finally:
        print(session.transcript())
        session.close()


def test_book_lookup_oserror_falls_back_to_search():
    script = """
import chess_engine.stockconda as module
class BrokenBook:
    exhausted = False
    def get_move(self, board, temperature=1):
        raise OSError('read failed')
module.load_opening_book = lambda: BrokenBook()
module.UCIEngine().run()
"""
    session = UciSession([sys.executable, "-u", "-c", script])
    try:
        session.fence()
        start = session.send("setoption name Book value true")
        session.wait(lambda line: "Opening book enabled" in line, start, 5)
        session.fence()
        board = chess.Board()
        session.position(board)
        start = session.go(board, "go depth 1")
        session.wait(lambda line: "book lookup failed" in line, start, 5)
        session.wait(lambda line: line.startswith("info depth 1 "), start, 5)
        session.bestmove(start)
        session.finish()
    finally:
        print(session.transcript())
        session.close()


def test_book_ignores_nonstandard_root_even_for_legal_first_move():
    script = """
import chess
import chess_engine.stockconda as module
from chess_engine.opening_book import OpeningBook
book = OpeningBook({(chess.Move.from_uci('g1f3'),): 1})
module.load_opening_book = lambda: book
module.UCIEngine().run()
"""
    session = UciSession([sys.executable, "-u", "-c", script])
    try:
        session.fence()
        start = session.send("setoption name Book value true")
        session.wait(lambda line: "Opening book enabled" in line, start, 5)
        session.fence()
        board = chess.Board()
        board.push_uci("e2e4")
        board.push_uci("e7e5")
        session.position(board)  # A bare FEN has no standard-root move stack.
        start = session.go(board, "go depth 1")
        session.wait(lambda line: line.startswith("info depth 1 "), start, 5)
        session.bestmove(start)
        assert not any(
            channel == "stdout" and line.startswith("info string book move ")
            for _, channel, line in session.events[start:]
        ), session.transcript()
        session.finish()
    finally:
        print(session.transcript())
        session.close()


def test_new_start_position_reenables_exhausted_book():
    script = """
import chess
import chess_engine.stockconda as module
from chess_engine.opening_book import OpeningBook
book = OpeningBook({(chess.Move.from_uci('e2e4'),): 1})
module.load_opening_book = lambda: book
module.UCIEngine().run()
"""
    session = UciSession([sys.executable, "-u", "-c", script])
    try:
        session.fence()
        start = session.send("setoption name Book value true")
        session.wait(lambda line: "Opening book enabled" in line, start, 5)
        session.fence()
        board = chess.Board()
        board.push_uci("e2e4")
        session.send("position startpos moves e2e4")
        first = session.go(board, "go depth 1")
        session.wait(lambda line: line.startswith("info depth 1 "), first, 5)
        session.bestmove(first)
        start_board = chess.Board()
        session.send("position startpos")
        second = session.go(start_board, "go depth 1")
        session.wait(lambda line: line == "info string book move e2e4", second, 5)
        session.bestmove(second)
        session.finish()
    finally:
        print(session.transcript())
        session.close()


def test_different_opening_line_reenables_exhausted_book():
    script = """
import chess
import chess_engine.stockconda as module
from chess_engine.opening_book import OpeningBook
book = OpeningBook({
    (chess.Move.from_uci('e2e4'), chess.Move.from_uci('e7e5')): 1,
    (chess.Move.from_uci('d2d4'), chess.Move.from_uci('d7d5')): 1,
})
module.load_opening_book = lambda: book
module.UCIEngine().run()
"""
    session = UciSession([sys.executable, "-u", "-c", script])
    try:
        session.fence()
        start = session.send("setoption name Book value true")
        session.wait(lambda line: "Opening book enabled" in line, start, 5)
        session.fence()
        off_book = chess.Board()
        off_book.push_uci("e2e4")
        off_book.push_uci("c7c5")
        session.send("position startpos moves e2e4 c7c5")
        first = session.go(off_book, "go depth 1")
        session.wait(lambda line: line.startswith("info depth 1 "), first, 5)
        session.bestmove(first)

        different = chess.Board()
        different.push_uci("d2d4")
        session.send("position startpos moves d2d4")
        second = session.go(different, "go depth 1")
        session.wait(lambda line: line == "info string book move d7d5", second, 5)
        session.bestmove(second)
        session.finish()
    finally:
        print(session.transcript())
        session.close()

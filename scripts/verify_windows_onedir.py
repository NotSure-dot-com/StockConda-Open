"""UCI process smoke for the actual Windows onedir executable."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time

import chess


class Session:
    def __init__(self, exe: Path, cwd: Path):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        self.process = subprocess.Popen(
            [str(exe)], cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self.lines: queue.Queue[tuple[str, str]] = queue.Queue()
        self.trace: list[str] = []
        self.bestmoves: list[str] = []
        self.readers: list[threading.Thread] = []
        for channel, stream in (("stdout", self.process.stdout), ("stderr", self.process.stderr)):
            assert stream is not None
            reader = threading.Thread(target=self._read, args=(channel, stream), daemon=True)
            self.readers.append(reader)
            reader.start()

    def _read(self, channel: str, stream) -> None:
        for raw in stream:
            self.lines.put((channel, raw.rstrip("\r\n")))
        self.lines.put((channel, "<EOF>"))

    def send(self, command: str) -> None:
        self.trace.append("stdin: " + command)
        assert self.process.stdin is not None
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def until(self, predicate, timeout: float = 10) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                channel, line = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            self.trace.append(f"{channel}: {line}")
            if channel == "stdout" and line.startswith("bestmove "):
                self.bestmoves.append(line)
            if line == "<EOF>":
                if channel == "stdout":
                    raise AssertionError("engine stdout closed early")
                continue
            if channel == "stdout" and predicate(line):
                return line
        raise AssertionError("UCI response timeout")

    def exit(self) -> None:
        self.process.wait(timeout=10)
        for reader in self.readers:
            reader.join(timeout=2)
            if reader.is_alive():
                raise AssertionError("UCI output reader did not finish")
        if self.process.returncode != 0:
            raise AssertionError(f"engine exited {self.process.returncode}")
        while not self.lines.empty():
            channel, line = self.lines.get_nowait()
            self.trace.append(f"{channel}: {line}")
            if channel == "stdout" and line.startswith("bestmove "):
                self.bestmoves.append(line)

    def cleanup(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)


def legal(line: str, board: chess.Board) -> None:
    move = chess.Move.from_uci(line.split()[1])
    assert move in board.legal_moves, (line, board.fen())


def run(exe: Path, transcript: Path) -> None:
    all_trace: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as temporary:
            foreign_cwd = Path(temporary)
            (foreign_cwd / "opening_book.pkl").write_bytes(b"unrelated book")
            session = Session(exe, foreign_cwd)
            try:
                session.send("uci")
                session.until(lambda line: line == "uciok")
                session.send("isready")
                session.until(lambda line: line == "readyok")
                session.send("setoption name Book value true")
                session.until(lambda line: "Opening book enabled" in line)
                session.send("position startpos")
                session.send("go depth 1")
                session.until(lambda line: line.startswith("info string book move "))
                legal(session.until(lambda line: line.startswith("bestmove ")), chess.Board())
                session.send("setoption name Book value false")
                session.send("position startpos")
                session.send("go infinite")
                session.until(lambda line: line.startswith("info depth 1 "))
                session.send("isready")
                session.until(lambda line: line == "readyok")
                session.send("stop")
                legal(session.until(lambda line: line.startswith("bestmove ")), chess.Board())
                session.send("quit")
                session.exit()
                assert len(session.bestmoves) == 2, session.bestmoves
            finally:
                session.cleanup()
                all_trace += session.trace

            eof_session = Session(exe, foreign_cwd)
            try:
                eof_session.send("uci")
                eof_session.until(lambda line: line == "uciok")
                eof_session.send("position startpos")
                eof_session.send("go infinite")
                eof_session.until(lambda line: line.startswith("info depth 1 "))
                assert eof_session.process.stdin is not None
                eof_session.process.stdin.close()
                eof_session.exit()
                assert len(eof_session.bestmoves) <= 1, eof_session.bestmoves
                if eof_session.bestmoves:
                    legal(eof_session.bestmoves[0], chess.Board())
            finally:
                eof_session.cleanup()
                all_trace += eof_session.trace
    finally:
        transcript.write_text("\n".join(all_trace) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("exe", type=Path)
    parser.add_argument("transcript", type=Path)
    args = parser.parse_args()
    run(args.exe.resolve(strict=True), args.transcript)

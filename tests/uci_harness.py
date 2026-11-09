"""Bounded UCI subprocess interaction with an append-only transcript."""
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import chess


class UciSession:
    def __init__(self, command=None):
        root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        # Staged tests can use a separately built, immutable source snapshot.
        source = env.get('STOCKCONDA_TEST_SOURCE', str(root / 'src'))
        env['PYTHONPATH'] = source
        self.events = []
        self.condition = threading.Condition()
        self.eof = set()
        self.roots = []
        self.forced = False
        self.proc = subprocess.Popen(
            command or [sys.executable, '-u', '-m', 'chess_engine.stockconda'],
            cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
            start_new_session=(os.name != 'nt'))
        self.readers = []
        for channel, stream in [('stdout', self.proc.stdout), ('stderr', self.proc.stderr)]:
            reader = threading.Thread(target=self._read, args=(channel, stream), daemon=True)
            self.readers.append(reader)
            reader.start()

    def _read(self, channel, stream):
        try:
            for line in stream:
                with self.condition:
                    self.events.append((time.monotonic(), channel, line.rstrip('\n')))
                    self.condition.notify_all()
        finally:
            with self.condition:
                self.eof.add(channel)
                self.condition.notify_all()

    def transcript(self):
        with self.condition:
            return '\n'.join(f'{stamp:.6f} {channel}: {line}' for stamp, channel, line in self.events)

    def send(self, command):
        with self.condition:
            start = len(self.events)
            self.events.append((time.monotonic(), 'stdin', command))
        assert self.proc.poll() is None, self.transcript()
        self.proc.stdin.write(command + '\n')
        self.proc.stdin.flush()
        return start

    def wait(self, predicate, start=0, timeout=2):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for _, channel, line in self.events[start:]:
                    if channel == 'stdout' and predicate(line):
                        return line
                if 'stdout' in self.eof:
                    raise AssertionError('Unexpected EOF\n' + self.transcript())
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError('Response timeout\n' + self.transcript())
                self.condition.wait(remaining)

    def fence(self):
        start = self.send('uci')
        self.wait(lambda s: s == 'uciok', start, 5)

    def ready(self, timeout=1):
        start = self.send('isready')
        self.wait(lambda s: s == 'readyok', start, timeout)

    def handshake(self):
        self.fence()
        self.send('setoption name Book value false')
        self.fence()  # Consumes all earlier stdout, including setoption's readyok.
        self.ready()

    def position(self, board):
        self.send('position fen ' + board.fen())

    def go(self, board, command='go infinite'):
        self.roots.append(board.copy())
        start = self.send(command)
        if command == 'go infinite' and any(board.legal_moves):
            self.wait(lambda s: s.startswith('info depth 1 '), start, 2)
        return start

    def bestmove(self, start):
        return self.wait(lambda s: s.startswith('bestmove '), start, 2)

    def stop(self):
        return self.bestmove(self.send('stop'))

    def finish(self, eof=False):
        if eof:
            with self.condition:
                self.events.append((time.monotonic(), 'stdin', '<EOF>'))
            self.proc.stdin.close()
        else:
            self.send('quit')
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError('Shutdown timeout\n' + self.transcript()) from exc
        for reader in self.readers:
            reader.join(timeout=2)
        assert all(not reader.is_alive() for reader in self.readers), self.transcript()
        assert self.proc.returncode == 0, self.transcript()
        assert not self.forced
        self.validate_results()

    def validate_results(self):
        lines = [line for _, channel, line in self.events
                 if channel == 'stdout' and line.startswith('bestmove')]
        assert len(lines) == len(self.roots), self.transcript()
        for line, board in zip(lines, self.roots):
            board = board.copy()
            parts = line.split()
            assert len(parts) in (2, 4) and parts[0] == 'bestmove', self.transcript()
            if parts[1] == '0000':
                assert not any(board.legal_moves), self.transcript()
                assert len(parts) == 2, self.transcript()
            else:
                try:
                    move = chess.Move.from_uci(parts[1])
                except ValueError as exc:
                    raise AssertionError(self.transcript()) from exc
                assert move in board.legal_moves, self.transcript()
                if len(parts) == 4:
                    assert parts[2] == 'ponder', self.transcript()
                    board.push(move)
                    assert chess.Move.from_uci(parts[3]) in board.legal_moves, self.transcript()

    def close(self):
        # Cleanup never converts a failed finish() into a successful one.
        if self.proc.poll() is None:
            self.forced = True
            if os.name == 'nt':
                self.proc.kill()
            else:
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=2)
        for reader in self.readers:
            reader.join(timeout=2)
        assert all(not reader.is_alive() for reader in self.readers), self.transcript()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if not stream.closed:
                stream.close()


def engine_command_with_fault(fault):
    """Separate-process output/dispatch mutations; production files stay intact."""
    script = '''
from chess_engine.stockconda import UCIEngine
import time
fault = FAULT
original = UCIEngine._uci_print
def output(self, *args, **kwargs):
    line = ' '.join(map(str, args))
    if fault == 'ready' and line == 'readyok': return
    if line.startswith('bestmove '):
        if fault == 'missing': return
        if fault == 'illegal': args = ('bestmove e2e5',)
        original(self, *args, **kwargs)
        if fault == 'duplicate':
            time.sleep(0.35)
            original(self, *args, **kwargs)
        return
    original(self, *args, **kwargs)
UCIEngine._uci_print = output
if fault == 'quit':
    def ignore_quit(self): time.sleep(30)
    UCIEngine.handle_quit = ignore_quit
UCIEngine().run()
'''.replace('FAULT', repr(fault))
    return [sys.executable, '-u', '-c', script]

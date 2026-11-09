"""Reviewed admission controls; fake peers test harness, mutants test engine cases."""
import sys

import chess
import pytest

from uci_harness import UciSession, engine_command_with_fault


@pytest.mark.parametrize('script,reason', [
    ('import time; time.sleep(10)', 'Response timeout'),
    ('pass', 'Unexpected EOF'),
])
def test_missing_response_and_eof(script, reason):
    engine = UciSession([sys.executable, '-u', '-c', script])
    try:
        with pytest.raises(AssertionError, match=reason):
            engine.wait(lambda line: line == 'readyok', timeout=0.5)
    finally:
        engine.close()
    assert engine.proc.poll() is not None
    assert all(not reader.is_alive() for reader in engine.readers)


@pytest.mark.parametrize('move,duplicate', [('e2e5', False), ('e2e4', True)])
def test_bad_and_delayed_duplicate_result(move, duplicate):
    script = f'''
import sys, time
for line in sys.stdin:
    if line.strip() == 'go':
        print('bestmove {move}', flush=True)
        if {duplicate!r}:
            time.sleep(0.35)
            print('bestmove {move}', flush=True)
    if line.strip() == 'quit': break
'''
    engine = UciSession([sys.executable, '-u', '-c', script])
    try:
        engine.roots.append(chess.Board())
        engine.bestmove(engine.send('go'))
        with pytest.raises(AssertionError):
            engine.finish()
        assert not engine.forced
        assert engine.proc.returncode == 0
        assert all(not reader.is_alive() for reader in engine.readers)
    finally:
        engine.close()


@pytest.mark.parametrize('fault', ['ready', 'missing', 'duplicate', 'illegal', 'quit'])
def test_engine_fault_is_detected(fault):
    engine = UciSession(engine_command_with_fault(fault))
    try:
        engine.fence()  # Import/start must succeed, otherwise this control is invalid.
        if fault == 'ready':
            with pytest.raises(AssertionError, match='Response timeout'):
                engine.ready()
            return
        engine.ready()
        board = chess.Board()
        engine.position(board)
        engine.go(board)
        if fault == 'missing':
            with pytest.raises(AssertionError, match='Response timeout'):
                engine.stop()
        elif fault == 'quit':
            with pytest.raises(AssertionError, match='Shutdown timeout'):
                engine.finish()
        else:
            engine.stop()
            with pytest.raises(AssertionError):
                engine.finish()
            assert engine.proc.returncode == 0  # Failure must concern results, not a crash.
            assert all(not r.is_alive() for r in engine.readers)
    finally:
        print(engine.transcript())
        engine.close()

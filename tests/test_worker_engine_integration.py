"""Engine integration probes; fault injection replaces search, not lifecycle."""
import threading

import chess
import pytest

from chess_engine.stockconda import UCIEngine


def test_engine_reuses_worker_for_100_searches(capsys):
    engine = UCIEngine()
    threads = []
    def search(*args):
        threads.append(threading.current_thread())
        return chess.Move.from_uci('e2e4'), 0
    engine.execute_search_task = search
    try:
        for _ in range(100):
            engine.handle_go(['depth', '1'])
            engine.handle_stop()
        assert len(set(threads)) == 1
        assert len([s for s in capsys.readouterr().out.splitlines() if s.startswith('bestmove ')]) == 100
    finally:
        engine.handle_quit()
    assert all(not t.is_alive() for t in threads)


@pytest.mark.parametrize('bad', ['raise', 'illegal'])
def test_fallback_and_next_search_after_error(bad, capsys):
    engine = UCIEngine()
    def search(*args):
        if bad == 'raise':
            raise ValueError('injected search failure')
        return chess.Move.from_uci('e2e5'), 0
    engine.execute_search_task = search
    try:
        engine.handle_go(['depth', '1'])
        engine.handle_stop()
        engine.execute_search_task = lambda *args: (chess.Move.from_uci('d2d4'), 0)
        engine.handle_go(['depth', '1'])
        engine.handle_stop()
        lines = [s for s in capsys.readouterr().out.splitlines() if s.startswith('bestmove ')]
        assert len(lines) == 2
        assert chess.Move.from_uci(lines[0].split()[1]) in chess.Board().legal_moves
        assert lines[1].split()[1] == 'd2d4'
    finally:
        engine.handle_quit()


def test_native_cleanup_follows_real_worker_exit(monkeypatch):
    import chess_engine.stockconda as module
    engine = UCIEngine()
    entered, release, exiting = (threading.Event() for _ in range(3))
    threads, observations = [], []
    original_free = module.free_cython_tt
    def free():
        observations.append(not threads[0].is_alive())
        original_free()
    def search(*args):
        threads.append(threading.current_thread())
        entered.set()
        assert release.wait(3)
        return chess.Move.from_uci('e2e4'), 0
    engine.execute_search_task = search
    monkeypatch.setattr(module, 'free_cython_tt', free)
    engine.handle_go(['infinite'])
    assert entered.wait(2)
    def quit_engine():
        exiting.set()
        engine.handle_quit()
    closer = threading.Thread(target=quit_engine)
    closer.start()
    try:
        assert exiting.wait(2)
        assert observations == []
        release.set()
        closer.join(3)
        assert not closer.is_alive()
        engine.handle_quit()
        assert observations == [True]
    finally:
        release.set()
        closer.join(3)
        engine.handle_quit()


@pytest.mark.parametrize('mutation', ['position', 'ucinewgame', 'setoption'])
def test_mutation_waits_for_search_completion(mutation):
    engine = UCIEngine()
    entered, release = threading.Event(), threading.Event()
    def search(*args):
        entered.set()
        assert release.wait(3)
        return chess.Move.from_uci('e2e4'), 0
    engine.execute_search_task = search
    engine.board.push_uci('e2e4')
    before = engine.board.fen(), engine.tt_size_bytes
    engine.handle_go(['infinite'])
    assert entered.wait(2)
    actions = {
        'position': lambda: engine.handle_position(['startpos']),
        'ucinewgame': engine.handle_ucinewgame,
        'setoption': lambda: engine.handle_setoption(['name', 'Hash', 'value', '32']),
    }
    changer = threading.Thread(target=actions[mutation])
    changer.start()
    try:
        assert engine._active_cancel.wait(2)
        assert (engine.board.fen(), engine.tt_size_bytes) == before
        release.set()
        changer.join(3)
        assert not changer.is_alive()
        if mutation == 'setoption':
            assert engine.tt_size_bytes == 32 * 1024 * 1024
        else:
            assert engine.board == chess.Board()
    finally:
        release.set()
        changer.join(3)
        engine.handle_quit()


def test_cancelled_iteration_keeps_last_completed_move(capsys):
    engine = UCIEngine()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def pvs(board, depth, *args, **kwargs):
        calls.append(depth)
        if depth == 1:
            return 10, chess.Move.from_uci('e2e4')
        entered.set()
        assert release.wait(3)
        return 500, chess.Move.from_uci('d2d4')
    engine.pvs_search = pvs
    stopper = threading.Thread(target=engine.handle_stop)
    try:
        engine.handle_go(['infinite'])
        assert entered.wait(2)
        stopper.start()
        assert engine._active_cancel.wait(2)
        release.set()
        stopper.join(3)
        assert not stopper.is_alive()
        output = capsys.readouterr().out.splitlines()
        assert [s.split()[1] for s in output if s.startswith('bestmove ')] == ['e2e4']
        assert not any(s.startswith('info depth 2 ') for s in output)
        assert calls == [1, 2]
    finally:
        release.set()
        if stopper.ident is not None:
            stopper.join(3)
        engine.handle_quit()


def test_task_cancellation_reaches_recursive_time_check():
    engine = UCIEngine()
    entered, release = threading.Event(), threading.Event()
    observations = []
    def search(*args):
        entered.set()
        assert release.wait(3)
        observations.append(engine.stop_flag_check())
        return chess.Move.from_uci('e2e4'), 0
    engine.execute_search_task = search
    stopper = threading.Thread(target=engine.handle_stop)
    try:
        engine.handle_go(['infinite'])
        assert entered.wait(2)
        stopper.start()
        assert engine._active_cancel.wait(2)
        release.set()
        stopper.join(3)
        assert observations == [True]
    finally:
        release.set()
        if stopper.ident is not None:
            stopper.join(3)
        engine.handle_quit()


def test_terminal_output_failure_closes_admission(monkeypatch):
    engine = UCIEngine()
    outputs = []
    original = engine._uci_print
    def output(*args, **kwargs):
        if str(args[0]).startswith('bestmove '):
            outputs.append(args[0])
            raise BrokenPipeError('injected broken output')
        original(*args, **kwargs)
    monkeypatch.setattr(engine, '_uci_print', output)
    engine.execute_search_task = lambda *args: (chess.Move.from_uci('e2e4'), 0)
    try:
        engine.handle_go(['depth', '1'])
        engine.handle_stop()
        with pytest.raises(RuntimeError):
            engine.handle_go(['depth', '1'])
        assert len(outputs) == 1
    finally:
        engine.handle_quit()


def test_info_depth_output_failure_closes_admission(monkeypatch):
    engine = UCIEngine()
    original = engine._uci_print
    attempted = threading.Event()
    outputs = []
    failure = BrokenPipeError('injected broken info output')

    def output(*args, **kwargs):
        line = str(args[0])
        outputs.append(line)
        if line.startswith('info depth '):
            attempted.set()
            raise failure
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, '_uci_print', output)

    try:
        # Keep the real search and wait for its output fault before stop or a
        # replacement go can cancel the iteration that must reach that fault.
        engine.handle_go(['depth', '1'])
        assert attempted.wait(2), 'search never reached info depth output'
        engine.handle_stop()
        assert engine._search_worker.failure is failure
        with pytest.raises(RuntimeError) as rejected:
            engine.handle_go(['depth', '1'])
        assert rejected.value.__cause__ is failure
        assert sum(line.startswith('info depth ') for line in outputs) == 1
        assert not any(line.startswith('bestmove ') for line in outputs)
        assert not any(line.startswith('info string search error') for line in outputs)
    finally:
        engine.handle_quit()

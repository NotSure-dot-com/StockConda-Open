"""Clock-source regressions: real searches, only the external clocks controlled."""
import re

import pytest

import chess_engine.stockconda as module


class Clock:
    def __init__(self):
        self.wall = 100_000.0
        self.elapsed = 100.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.elapsed


@pytest.fixture
def engine_clock(monkeypatch):
    engine = module.UCIEngine()
    engine.use_opening_book = False
    clock = Clock()
    monkeypatch.setattr(module, 'time', clock)
    try:
        yield engine, clock
    finally:
        engine.handle_quit()


@pytest.mark.parametrize('entry', ['execute_search_task', 'start_search'])
def test_elapsed_output_survives_clock_rollback(engine_clock, capsys, entry):
    """A wall-clock subtraction or stale direct-search origin must fail."""
    engine, clock = engine_clock
    real_search = engine.pvs_search
    nesting = 0
    def search(*args, **kwargs):
        nonlocal nesting
        nesting += 1
        try:
            result = real_search(*args, **kwargs)
        finally:
            nesting -= 1
        if nesting == 0:
            clock.elapsed += .125
            clock.wall -= 60
        return result
    engine.pvs_search = search
    getattr(engine, entry)(None, 1, False)
    lines = [line for line in capsys.readouterr().out.splitlines()
             if line.startswith('info depth 1 ')]
    assert len(lines) == 1
    assert re.search(r'\btime 125\b', lines[0]), lines


@pytest.mark.parametrize('wall_jump', [-600, 600])
def test_budget_expiry_ignores_wall_clock_jump(engine_clock, wall_jump):
    """Mixed clocks, premature forward expiry and delayed rollback expiry fail."""
    engine, clock = engine_clock
    engine.execute_search_task(1.0, 1, False)
    clock.wall += wall_jump
    clock.elapsed += .5
    assert not any(engine.stop_flag_check() for _ in range(2048))
    clock.elapsed += .5
    assert any(engine.stop_flag_check() for _ in range(2048))


@pytest.mark.parametrize('entry', ['execute_search_task', 'start_search'])
def test_search_and_deadline_do_not_require_calendar_clock(engine_clock, capsys, entry):
    """A calendar-clock read anywhere in search timing must be unnecessary."""
    engine, clock = engine_clock
    def unavailable():
        raise AssertionError('calendar clock is unavailable for duration measurement')
    clock.time = unavailable
    getattr(engine, entry)(1.0, 1, False)
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith('info depth 1 ') for line in lines), lines
    assert not any('error' in line.lower() for line in lines), lines
    clock.elapsed += 1.0
    assert any(engine.stop_flag_check() for _ in range(2048))

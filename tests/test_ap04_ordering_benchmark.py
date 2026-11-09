"""Prospective AP04 thresholds: wrong precedence or sample arithmetic must fail."""
from importlib import util
from pathlib import Path
from typing import Any

import pytest


def runner() -> Any:
    path = Path(__file__).parents[1] / "scripts/ap04_ordering_benchmark.py"
    assert path.exists(), "AP04 decision runner has not been implemented"
    spec = util.spec_from_file_location("ap04_benchmark", path)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("aggregate", "fixtures", "valid", "expected"), [
    ([.90] * 10, [[.90] * 10] * 12, False, "FAIL"),
    ([.80, 1.20] * 5, [[.90] * 10] * 12, False, "FAIL"),
    ([.80, 1.20] * 5, [[1.1] * 10] * 12, True, "HOLD"),
    ([.90] * 10, [[.80, 1.20] * 5] * 12, True, "HOLD"),
    ([1.001] * 10, [[.95] * 10] * 12, True, "FAIL"),
    ([.90] * 10, [[1.031] * 10] + [[.9] * 10] * 11, True, "FAIL"),
    ([.971] * 10, [[.98] * 10] * 12, True, "HOLD"),
    ([1.] * 10, [[1.] * 10] * 12, True, "HOLD"),
    ([.97] * 10, [[1.03] * 10] + [[.9] * 10] * 11, True, "PASS"),
    ([.95] * 9, [[.95] * 10] * 12, True, "FAIL"),
    ([.95] * 10, [[.95] * 10] * 11, True, "FAIL"),
    ([float("nan")] * 10, [[.95] * 10] * 12, True, "FAIL"),
    ([.95] * 10, [[0.] * 10] * 12, True, "FAIL"),
])
def test_exclusive_decision_precedence(aggregate: list[float], fixtures: list[list[float]],
                                       valid: bool, expected: str) -> None:
    assert runner().decision(aggregate, fixtures, valid=valid)["status"] == expected


def test_relative_mad_is_unscaled() -> None:
    assert runner().relative_mad([.96, 1.04] * 5) == pytest.approx(.04)


def test_weighted_aggregate_uses_time_sums_and_requires_all_repetitions() -> None:
    rows = [{"block": block, "fixture": fixture, "engine": engine,
             "repetition": rep, "elapsed_ns": duration}
            for block in range(10) for fixture in range(12)
            for engine in ("baseline", "candidate") for rep in range(5)
            for duration in [100 if engine == "baseline" else 95]]
    aggregate, fixtures = runner().ratios(rows)
    assert aggregate == [.95] * 10
    assert fixtures == [[.95] * 10] * 12
    with pytest.raises(ValueError):
        runner().ratios(rows[:-1])
    with pytest.raises(ValueError):
        runner().ratios(rows + [rows[0]])


def test_aggregate_weights_longer_fixtures_by_their_search_time() -> None:
    rows = [{"block": block, "fixture": fixture, "engine": engine,
             "repetition": rep,
             "elapsed_ns": (1000 if engine == "baseline" else 500) if fixture == 0 else 100}
            for block in range(10) for fixture in range(12)
            for engine in ("baseline", "candidate") for rep in range(5)]
    aggregate, fixtures = runner().ratios(rows)
    # One long search plus eleven short searches: 1600/2100, not the
    # unweighted average of the twelve fixture ratios (11.5/12).
    assert aggregate == pytest.approx([16 / 21] * 10)
    assert fixtures == [[.5] * 10] + [[1.] * 10] * 11


def test_existing_output_is_rejected_without_modifying_it(tmp_path: Path) -> None:
    sentinel = tmp_path / "old.json"
    sentinel.write_text("retained")
    with pytest.raises(FileExistsError):
        runner().new_output(tmp_path)
    assert sentinel.read_text() == "retained"


def test_history_state_normalizes_both_storage_layouts() -> None:
    module = runner()
    legacy = [[[source + target + turn for turn in range(2)]
               for target in range(64)] for source in range(64)]

    class Planes:
        def plane(self, turn: int) -> list[list[int]]:
            return [[legacy[source][target][turn] for target in range(64)]
                    for source in range(64)]

    assert module.history_state(legacy) == legacy
    assert module.history_state(Planes()) == legacy

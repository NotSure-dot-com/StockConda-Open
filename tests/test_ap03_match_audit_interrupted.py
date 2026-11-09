from __future__ import annotations

from importlib import util
from pathlib import Path
import sys
from typing import Any

import pytest


NAMES = ("candidate", "baseline")


def auditor() -> Any:
    path = Path(__file__).parents[1] / "scripts/ap03_match_audit.py"
    sys.path.insert(0, str(path.parent))
    spec = util.spec_from_file_location("ap03_match_audit_interrupted", path)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.NAMES = list(NAMES)
    return module


def engine_line(thread: int, name: str, direction: str, text: str) -> str:
    return f"[Engine] <{thread}> {name} {direction} {text}\n"


def truncated_trace(pending_searches: int = 1) -> list[str]:
    lines: list[str] = []
    for index, name in enumerate(NAMES):
        lines.extend([
            engine_line(0, name, "<---", "position startpos"),
            engine_line(0, name, "<---", "go wtime 1000 btime 1000 winc 100 binc 100"),
        ])
        if index >= pending_searches:
            lines.append(engine_line(0, name, "--->", "bestmove e2e4"))
    return lines


def test_interrupted_trace_allows_one_pending_search_per_match_thread() -> None:
    result = auditor().audit_trace(
        truncated_trace(), concurrency=1, expected_increment=100,
        allow_interrupted=True,
    )

    assert result["pending_searches"] == 1
    assert result["shutdown_complete"] is False


def test_interrupted_trace_rejects_more_pending_searches_than_concurrency() -> None:
    with pytest.raises(AssertionError, match="too many pending searches"):
        auditor().audit_trace(
            truncated_trace(pending_searches=2), concurrency=1,
            expected_increment=100, allow_interrupted=True,
        )


def test_interrupted_trace_rejects_two_pending_engines_on_one_match_thread() -> None:
    lines: list[str] = []
    for thread in range(2):
        for name in NAMES:
            lines.extend([
                engine_line(thread, name, "<---", "position startpos"),
                engine_line(
                    thread, name, "<---",
                    "go wtime 1000 btime 1000 winc 100 binc 100",
                ),
            ])
            if thread == 1:
                lines.append(engine_line(thread, name, "--->", "bestmove e2e4"))

    with pytest.raises(AssertionError, match="match thread"):
        auditor().audit_trace(
            lines, concurrency=2, expected_increment=100, allow_interrupted=True,
        )


def test_complete_trace_still_rejects_a_missing_result() -> None:
    with pytest.raises(AssertionError, match="missing result"):
        auditor().audit_trace(
            truncated_trace(), concurrency=1, expected_increment=100,
        )

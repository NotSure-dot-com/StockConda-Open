from __future__ import annotations

import chess
import pytest

from chess_engine.stockconda import HistoryTable


def test_history_table_exposes_color_planes_and_legacy_indexing() -> None:
    table = HistoryTable()

    table.set_value(chess.WHITE, chess.E2, chess.E4, 17)
    table[chess.E7][chess.E5][chess.BLACK] = 23

    assert table[chess.E2][chess.E4][chess.WHITE] == 17
    assert table.plane(chess.BLACK)[chess.E7][chess.E5] == 23
    assert table[(chess.E2, chess.E4, chess.WHITE)] == 17


def test_snapshot_copies_only_occupied_source_rows_without_aliases() -> None:
    table = HistoryTable()
    table.set_value(chess.WHITE, chess.E2, chess.E4, 31)
    table.set_value(chess.WHITE, chess.G1, chess.F3, 19)
    occupied = chess.BB_E2 | chess.BB_G1

    snapshot = table.snapshot(chess.WHITE, occupied)
    table[chess.E2][chess.E4][chess.WHITE] = 99

    assert snapshot[chess.E2][chess.E4] == 31
    assert snapshot[chess.G1][chess.F3] == 19
    assert snapshot[chess.A1] == ()


def test_public_plane_is_read_only_and_cannot_stale_snapshot_cache() -> None:
    table = HistoryTable()
    table.set_value(chess.WHITE, chess.E2, chess.E4, 10)
    occupied = chess.BB_E2
    before = table.snapshot(chess.WHITE, occupied)

    with pytest.raises(TypeError):
        table.plane(chess.WHITE)[chess.E2][chess.E4] = 99  # type: ignore[index]

    table.set_value(chess.WHITE, chess.E2, chess.E4, 17)
    after = table.snapshot(chess.WHITE, occupied)

    assert before[chess.E2][chess.E4] == 10
    assert after[chess.E2][chess.E4] == 17


def test_history_update_invalidates_future_snapshot_without_changing_old_snapshot() -> None:
    table = HistoryTable()
    table[chess.E2][chess.E4][chess.WHITE] = 10
    occupied = chess.BB_E2
    before = table.snapshot(chess.WHITE, occupied)

    table.add(chess.WHITE, chess.E2, chess.E4, 7)
    after = table.snapshot(chess.WHITE, occupied)

    assert before[chess.E2][chess.E4] == 10
    assert after[chess.E2][chess.E4] == 17


def test_decay_clear_items_and_legacy_import_preserve_values() -> None:
    legacy = [[[0 for _ in range(2)] for _ in range(64)] for _ in range(64)]
    legacy[chess.B1][chess.C3][chess.WHITE] = 9
    legacy[chess.B8][chess.C6][chess.BLACK] = 6
    table = HistoryTable.from_legacy(legacy)

    table.decay()

    assert table[chess.B1][chess.C3][chess.WHITE] == 4
    assert table[chess.B8][chess.C6][chess.BLACK] == 3
    assert dict(table.items()) == {
        (chess.B1, chess.C3, chess.WHITE): 4,
        (chess.B8, chess.C6, chess.BLACK): 3,
    }
    assert len(table) == 2

    table.clear()
    assert len(table) == 0

"""Audit concurrent Fastchess output by match thread AND engine, then paired PGNs."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import json
import math
from pathlib import Path
import re
from decimal import Decimal
from typing import Any, Literal, TypedDict

import chess
import chess.pgn
from ap03_uci_audit import audit

NAMES = ['AP03_PersistentWorker', 'AP03_Baseline_9acfd60']


class TraceReport(TypedDict):
    output_lines: int
    searches: int
    process_pairs: int
    normal_exits: dict[str, int]
    exit_pids: list[str]
    pending_searches: int
    shutdown_complete: bool


class EloDecision(TypedDict):
    elo: float
    half_width: float
    lower: float
    upper: float
    status: Literal['PASS', 'FAIL', 'HOLD']


class MatchResult(TypedDict):
    status: Literal['PASS', 'FAIL', 'HOLD']
    games: int
    candidate_wld: list[int]
    trace: TraceReport
    strength_verdict: Literal['NOT_ESTABLISHED'] | EloDecision


class MatchReport(MatchResult, total=False):
    sha256: dict[str, str]


def audit_trace(
    lines: Iterable[str], concurrency: int, expected_increment: int | None = None,
    *, allow_interrupted: bool = False,
) -> TraceReport:
    boards: dict[tuple[str, str], chess.Board] = {}
    pending: dict[tuple[str, str], chess.Board] = {}
    counts: dict[tuple[str, str], list[int]] = {}
    quits, exits = dict.fromkeys(NAMES, 0), dict.fromkeys(NAMES, 0)
    closing: set[tuple[str, str]] = set()
    exit_pids: set[str] = set()
    exit_record: str | None = None
    output_count = 0
    names = '|'.join(re.escape(name) for name in NAMES)
    pattern = re.compile(r'^\[Engine\].*?<\s*(\d+)>\s+(<stderr>\s+)?'
                         rf'({names})\s+(--->|<---)\s+(.*)$')
    for line in lines:
        terminated = re.search(r'<\s*(\d+)> fastchess --- Process with pid: (\d+) terminated with status: (\d+)', line)
        if terminated:
            thread, pid, status = terminated.groups()
            assert status == '0' and pid not in exit_pids and exit_record is None
            exit_pids.add(pid)
            exit_record = thread
        assert not re.match(r'^\[(?:WARN|ERR|ERROR|FATAL)\s*\]', line), line
        match = pattern.search(line.rstrip('\n'))
        if not match:
            assert not line.startswith('[Engine]'), 'unparsed engine trace: ' + line
            continue
        thread, stderr, name, direction, text = match.groups()
        key = thread, name
        if stderr:
            assert direction == '--->' and text == 'Process exited normally with status 0', text
            assert key in closing and exit_record == thread
            closing.remove(key)
            exit_record = None
            exits[name] += 1
            continue
        if direction == '<---':
            if text == 'quit':
                assert key not in closing
                closing.add(key)
                quits[name] += 1
            elif text.startswith('position '):
                parts = text.split()
                board = chess.Board() if parts[1] == 'startpos' else chess.Board(' '.join(parts[2:8]))
                if 'moves' in parts:
                    for move in parts[parts.index('moves') + 1:]:
                        board.push_uci(move)
                boards[key] = board
            elif text.startswith('go '):
                if expected_increment is not None:
                    parts = text.split()
                    assert int(parts[parts.index('winc') + 1]) == expected_increment
                    assert int(parts[parts.index('binc') + 1]) == expected_increment
                assert key not in pending, 'overlapping search'
                pending[key] = boards[key].copy()
                counts.setdefault(key, [0, 0])[0] += 1
        else:
            audit([('stdout', text)])
            output_count += 1
            if text.startswith('bestmove '):
                assert key in pending, 'duplicate/unsolicited result'
                board = pending.pop(key)
                parts = text.split()
                if parts[1] == '0000':
                    assert not any(board.legal_moves)
                else:
                    board.push_uci(parts[1])
                    if len(parts) == 4:
                        assert parts[2] == 'ponder'
                        board.push_uci(parts[3])
                counts[key][1] += 1
    assert len(counts) == concurrency * 2
    if allow_interrupted:
        assert len(pending) <= concurrency, 'too many pending searches'
        assert len({thread for thread, _name in pending}) == len(pending), \
            'more than one pending engine on a match thread'
        assert all(go > 0 and go - best in (0, 1) for go, best in counts.values()), \
            'inconsistent interrupted search counts'
        assert not closing and exit_record is None, 'partial shutdown in interrupted trace'
        assert not exit_pids, 'unexpected process exits in interrupted trace'
        assert quits == exits == dict.fromkeys(NAMES, 0), \
            'unexpected shutdown in interrupted trace'
    else:
        assert not pending, 'missing result'
        assert all(go == best > 0 for go, best in counts.values())
        assert not closing and exit_record is None
        assert len(exit_pids) == concurrency * 2
        assert quits == exits == dict.fromkeys(NAMES, concurrency)
    return dict(output_lines=output_count, searches=sum(c[0] for c in counts.values()),
                process_pairs=concurrency, normal_exits=exits, exit_pids=sorted(exit_pids),
                pending_searches=len(pending), shutdown_complete=not allow_interrupted)


def final_summary(
    console: str, expected_games: int, results: list[int], penta: list[int],
) -> tuple[float, float]:
    block = console.rsplit('Results of ', 1)[-1]
    assert block.startswith(NAMES[0] + ' vs ' + NAMES[1] + ' (')
    assert 'Finished match' in block
    totals = re.search(r'^Games: (\d+), Wins: (\d+), Losses: (\d+), Draws: (\d+),', block, re.M)
    assert totals and list(map(int, totals.groups())) == [expected_games] + results
    pairs = re.search(r'^Ptnml\(0-2\): \[([\d, ]+)\]', block, re.M)
    assert pairs and list(map(int, pairs[1].split(','))) == penta
    number = r'(?:-?[\d.]+|nan)'
    estimate = re.search(rf'^Elo: ({number}) \+/- ({number}),', block, re.M)
    assert estimate, 'missing final Elo estimate'
    return float(estimate[1]), float(estimate[2])


def elo_decision(estimate: float, half_width: float) -> EloDecision:
    assert math.isfinite(estimate) and math.isfinite(half_width) and half_width >= 0
    se = half_width / 1.959964
    lower, upper = estimate - 1.644854 * se, estimate + 1.644854 * se
    return dict(elo=estimate, half_width=half_width, lower=lower, upper=upper,
                status='PASS' if lower > -20 else 'FAIL' if upper < -20 else 'HOLD')


def validate_time_config(saved: Mapping[str, Any], tc: str) -> None:
    base, increment = (Decimal(value) * 1000 for value in tc.split('+'))
    assert len(saved['engines']) == 2
    for engine in saved['engines']:
        assert engine['limit']['tc'] == dict(time=int(base), increment=int(increment),
                                           fixed_time=0, moves=0, timemargin=0)


def audit_match(
    root: Path, expected_games: int, tc: str, concurrency: int, elo: bool = False,
) -> MatchReport:
    run = json.loads((root / 'run.json').read_text())
    assert run['exit_code'] == 0
    with (root / 'protocol.log').open() as stream:
        trace = audit_trace(stream, concurrency, int(Decimal(tc.split('+')[1]) * 1000))
    openings: list[list[chess.Move]] = []
    with Path('/reference/books/8moves_v3.pgn').open() as stream:
        for _ in range(expected_games // 2):
            game = chess.pgn.read_game(stream)
            assert game is not None and not game.errors
            openings.append(list(game.mainline_moves()))
    seen: set[tuple[int, str]] = set()
    pair_scores: dict[int, list[int]] = {}
    results = [0, 0, 0]  # candidate wins, losses, draws
    with (root / 'games.pgn').open() as stream:
        while (game := chess.pgn.read_game(stream)) is not None:
            assert not game.errors
            pair = int(game.headers['Round']) - 1
            assert 0 <= pair < expected_games // 2
            white, black = game.headers['White'], game.headers['Black']
            assert {white, black} == set(NAMES)
            key = pair, white
            assert key not in seen
            seen.add(key)
            assert game.headers['Termination'] in {'normal', 'adjudication'}
            assert game.headers['TimeControl'] == tc
            result = game.headers['Result']
            assert result in {'1-0', '0-1', '1/2-1/2'}
            if result == '1/2-1/2':
                results[2] += 1
                score = 1
            elif (result == '1-0') == (white == NAMES[0]):
                results[0] += 1
                score = 2
            else:
                results[1] += 1
                score = 0
            pair_scores.setdefault(pair, []).append(score)
            moves = list(game.mainline_moves())
            assert moves[:len(openings[pair])] == openings[pair]
            board = game.board()
            for move in moves:
                assert move in board.legal_moves
                board.push(move)
    assert len(seen) == expected_games
    console = (root / 'console.log').read_text()
    finished = re.findall(r'^Finished game (\d+)', console, re.M)
    assert len(finished) == expected_games and set(map(int, finished)) == set(range(1, expected_games + 1))
    assert 'Finished match' in console
    saved = json.loads((root / 'autosave.json').read_text())
    validate_time_config(saved, tc)
    assert saved['rounds'] * saved['games'] == expected_games
    assert saved['concurrency'] == concurrency and saved['seed'] == 90210
    assert not saved['sprt']['enabled'] and not saved['recover']
    stats = saved['stats'][NAMES[0] + ' vs ' + NAMES[1]]
    assert [stats[k] for k in ['wins', 'losses', 'draws']] == results
    penta = [0] * 5
    for scores in pair_scores.values():
        assert len(scores) == 2
        penta[sum(scores)] += 1
    assert penta == [stats['penta_LL'], stats['penta_LD'],
                     stats['penta_WL'] + stats['penta_DD'], stats['penta_WD'], stats['penta_WW']]
    estimate = final_summary(console, expected_games, results, penta)
    report: MatchReport = dict(status='PASS', games=expected_games, candidate_wld=results, trace=trace,
                               strength_verdict='NOT_ESTABLISHED')
    if elo:
        assert expected_games == 2000
        decision = elo_decision(*estimate)
        report['status'] = decision['status']
        report['strength_verdict'] = decision
    report['sha256'] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                        for name in ['run.json', 'protocol.log', 'games.pgn', 'console.log', 'autosave.json']}
    if 'input_sha256' in run:
        assert hashlib.sha256((root / 'input.json').read_bytes()).hexdigest() == run['input_sha256']
        report['sha256']['input.json'] = run['input_sha256']
    (root / 'audit.json').write_text(json.dumps(report, indent=2) + '\n')
    return report

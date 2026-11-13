"""Audit complete positive-case UCI transcripts, including nonterminal output."""
from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path
import re


def audit(events: Iterable[tuple[str, str]]) -> None:
    seen = 0
    for channel, line in events:
        if channel == 'stderr':
            raise AssertionError('Unexpected stderr: ' + line)
        if channel != 'stdout':
            continue
        seen += 1
        # Identified legacy prologue/epilogue, not UCI protocol messages.
        if line in {'StockConda v0.2.3.5.dev2 by Real', 'bye'}:
            continue
        assert not re.search(r'error|traceback|illegal|unavailable|exception', line, re.I), line
        assert re.fullmatch(
            r'uciok|readyok|id (?:name|author) .+|'
            r'option name .+ type (?:spin|check|combo|button|string)(?: .*)?|'
            r'info string .*|'
            r'info depth \d+ score cp -?\d+ time \d+ pv [a-h][1-8][a-h][1-8][qrbn]?(?: [a-h][1-8][a-h][1-8][qrbn]?)*|'
            r'bestmove (?:0000|[a-h][1-8][a-h][1-8][qrbn]?)(?: ponder [a-h][1-8][a-h][1-8][qrbn]?)?',
            line), 'Malformed UCI line: ' + line
    assert seen, 'No stdout transcript found'


def self_test() -> None:
    audit([('stdout', 'uciok'), ('stdout', 'bestmove e2e4')])
    for bad in [('stderr', 'Traceback'), ('stdout', 'info bogus'),
                ('stdout', 'info string search error: test'), ('stdout', 'bestmove nonsense'),
                ('stdout', 'info depth 1 score cp 0 time 1 pv e2e4 nonsense')]:
        try:
            audit([bad, ('stdout', 'bestmove e2e4')])
        except AssertionError:
            continue
        raise AssertionError('Fault escaped audit: ' + repr(bad))
    print('Audit control: valid transcript accepted; five malformed/error cases rejected')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('transcript', type=Path, nargs='?')
    args = parser.parse_args()
    self_test()
    if args.transcript:
        events = re.findall(r'^\d+\.\d+ (stdout|stderr): (.*)$',
                            args.transcript.read_text(), re.MULTILINE)
        audit(events)
        print('Complete positive-case transcript audit PASS:', len(events), 'output lines')

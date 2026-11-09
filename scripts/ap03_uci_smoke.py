"""Additional frozen smoke commands not in the admitted process test module."""
import argparse
import json
import os
from pathlib import Path
import sys
import hashlib

import chess


parser = argparse.ArgumentParser()
parser.add_argument('--identity', type=Path, required=True)
parser.add_argument('--parity', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
identity = json.loads(args.identity.read_text())
parity = json.loads(args.parity.read_text())
assert identity['FG-ENV'] == identity['FG-TYPE'] == parity['status'] == 'PASS'
assert hashlib.sha256(args.identity.read_bytes()).hexdigest() == parity['identity_sha256']
root = Path(identity['snapshots']['candidate']['root'])
assert parity['sources']['candidate'] == str(root / 'src')
for relative, expected in identity['snapshots']['candidate']['source'].items():
    if Path(relative).suffix in {'.py', '.pyi', '.pyx', '.pxd', '.h'}:
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected
for relative, expected in identity['snapshots']['candidate']['extensions'].items():
    assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected
sys.path.insert(0, str(root / 'tests'))
from uci_harness import UciSession
from ap03_uci_audit import audit

os.environ['STOCKCONDA_TEST_SOURCE'] = str(root / 'src')
session = UciSession()
try:
    session.handshake()
    previous = chess.Board()
    previous.push_uci('e2e4')
    session.position(previous)
    session.ready()
    session.send('position startpos')
    board = chess.Board()
    marker = session.go(board, 'go depth 1')
    session.bestmove(marker)
    board.push_uci('e2e4')
    session.position(board)
    marker = session.go(board, 'go movetime 25')
    session.bestmove(marker)
    session.finish()
    audit((channel, line) for _, channel, line in session.events)
finally:
    try:
        args.output.write_text(session.transcript(), encoding='utf-8')
    finally:
        session.close()

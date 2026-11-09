"""Full four-game PGN and UCI trace audit; explicitly no strength verdict."""
import hashlib
import json
from pathlib import Path
import re
import sys

import chess
import chess.pgn

sys.path.insert(0, '/build')
from ap03_uci_audit import audit, self_test

root = Path('/results/ap03_preflight1')
run = json.loads((root / 'run.json').read_text())
assert run['exit_code'] == 0 and run['status'] == 'NEEDS_AUDIT'
self_test()
names = ['AP03_PersistentWorker', 'AP03_Baseline_9acfd60']
boards = {name: chess.Board() for name in names}
pending = {}
quitted = set()
normal_exits = set()
counts = {name: {'go': 0, 'bestmove': 0} for name in names}
outputs = []
trace = (root / 'protocol.log').read_text()
assert not re.search(r'^\[(?:WARN|ERR|ERROR|FATAL)\s*\]', trace, re.M)
for line in trace.splitlines():
    match = re.search(r'\b(AP03_PersistentWorker|AP03_Baseline_9acfd60)\s+(--->|<---)\s+(.*)$', line)
    if not match:
        continue
    name, direction, text = match.groups()
    if '<stderr>' in line:
        # Fastchess itself formats this string (verified in its binary), after
        # the trace records process exit0. No other stderr is accepted.
        assert direction == '--->' and text == 'Process exited normally with status 0'
        assert name in quitted and name not in normal_exits
        normal_exits.add(name)
        continue
    if direction == '<---':
        if text == 'quit':
            quitted.add(name)
        elif text.startswith('position '):
            parts = text.split()
            board = chess.Board() if parts[1] == 'startpos' else chess.Board(' '.join(parts[2:8]))
            if 'moves' in parts:
                for move in parts[parts.index('moves')+1:]:
                    board.push_uci(move)
            boards[name] = board
        elif text.startswith('go '):
            assert name not in pending, 'overlapping go without terminal result'
            pending[name] = boards[name].copy()
            counts[name]['go'] += 1
    else:
        outputs.append(('stdout', text))
        if text.startswith('bestmove '):
            assert name in pending, 'duplicate or unsolicited bestmove'
            board = pending.pop(name)
            parts = text.split()
            if parts[1] == '0000':
                assert not any(board.legal_moves)
            else:
                board.push_uci(parts[1])
                if len(parts) == 4:
                    assert parts[2] == 'ponder'
                    board.push_uci(parts[3])
            counts[name]['bestmove'] += 1
assert not pending, 'missing terminal result'
assert normal_exits == set(names)
assert all(c['go'] == c['bestmove'] > 0 for c in counts.values())
audit(outputs)
with Path('/reference/books/8moves_v3.pgn').open() as source:
    openings = [list(chess.pgn.read_game(source).mainline_moves()) for _ in range(2)]
games = []
with (root / 'games.pgn').open() as stream:
    while (game := chess.pgn.read_game(stream)) is not None:
        assert not game.errors
        index = len(games)
        assert index < 4
        assert game.headers['White'] == names[index % 2]
        assert game.headers['Black'] == names[1-index % 2]
        # The frozen resign/draw adjudication is a regular result, not a crash.
        assert game.headers['Termination'] in {'normal', 'adjudication'}
        assert game.headers['TimeControl'] == '10+0.1'
        assert game.headers['Result'] in {'1-0', '0-1', '1/2-1/2'}
        moves = list(game.mainline_moves())
        assert moves[:len(openings[index//2])] == openings[index//2]
        board = game.board()
        for move in moves:
            assert move in board.legal_moves
            board.push(move)
        games.append(dict(white=game.headers['White'], black=game.headers['Black'],
                          result=game.headers['Result'], plies=len(moves)))
assert len(games) == 4
console = (root / 'console.log').read_text()
assert len(re.findall(r'^Finished game \d+', console, re.M)) == 4
report = dict(status='PASS', gate='FG-FC-PRE', strength_verdict='NOT_ESTABLISHED',
              games=games, output_lines=len(outputs), terminal_counts=counts,
              sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in [root/'run.json', root/'games.pgn', root/'protocol.log', root/'console.log']})
(root / 'audit.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))

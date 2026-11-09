"""Frozen AP03 depth-5 parity; selection and execution are separate stages."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import chess
import chess.pgn


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select(book, output):
    assert digest(book) == '5835239f88cc2c7511b177c32392a69f3ede21819cf0616f80a7f907cd21d17e'
    fens = []
    with book.open(encoding='utf-8') as stream:
        while len(fens) < 50:
            game = chess.pgn.read_game(stream)
            assert game is not None, 'fewer than 50 eligible roots'
            assert not game.errors, game.errors
            board = game.end().board()
            if not board.is_game_over() and board.fen() not in fens:
                fens.append(board.fen())
    with output.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write('\n'.join(fens) + '\n')
    print('50 roots:', digest(output), flush=True)


def run(args):
    identity = json.loads(args.identity.read_text())
    assert identity['FG-ENV'] == identity['FG-TYPE'] == 'PASS'
    assert args.baseline.resolve() != args.candidate.resolve()
    assert identity['snapshots']['baseline']['commit'] != identity['snapshots']['candidate']['commit']
    for label, root in [('baseline', args.baseline), ('candidate', args.candidate)]:
        snapshot = identity['snapshots'][label]
        assert root.resolve() == Path(snapshot['root']).resolve()
        for relative, expected in snapshot['source'].items():
            if Path(relative).suffix in {'.py', '.pyi', '.pyx', '.pxd', '.h'}:
                assert digest(root / relative) == expected, relative
        for relative, expected in snapshot['extensions'].items():
            assert digest(root / relative) == expected, relative
        assert digest(root / 'ap03-launch.sh') == snapshot['launcher_sha256']
    sys.path.insert(0, str(args.candidate / 'tests'))
    from uci_harness import UciSession
    harness_path = Path(sys.modules['uci_harness'].__file__).resolve()
    assert harness_path == (args.candidate / 'tests/uci_harness.py').resolve()
    fens = args.fens.read_text().splitlines()
    assert len(fens) == len(set(fens)) == 50
    assert digest(args.fens) == args.fens_sha
    args.output.mkdir(exist_ok=False)
    report = {'identity_sha256': digest(args.identity),
              'harness_sha256': digest(harness_path),
              'fens_sha256': digest(args.fens), 'runner_sha256': digest(Path(__file__)),
              'roots': fens, 'sources': {}, 'runs': {}, 'status': 'RUNNING'}

    def save():
        (args.output / 'parity.json').write_text(json.dumps(report, indent=2) + '\n')

    def start(source):
        os.environ['STOCKCONDA_TEST_SOURCE'] = str(source)
        session = UciSession()
        try:
            session.handshake()
            session.send('setoption name Hash value 16')
            session.fence()
            session.ready()
            return session
        except BaseException:
            session.close()
            raise

    def search(session, fen):
        board = chess.Board(fen)
        session.send('ucinewgame')
        session.ready()
        session.position(board)
        marker = session.go(board, 'go depth 5')
        # Fixed-depth completion is not a stop-latency measurement.
        result = session.wait(lambda line: line.startswith('bestmove '), marker, 120)
        lines = [line for _, channel, line in session.events[marker:]
                 if channel == 'stdout' and line.startswith('info depth ')]
        assert lines, session.transcript()
        info = lines[-1].split()
        assert info[3:5] == ['score', 'cp'], lines[-1]
        assert int(info[2]) == 5, lines[-1]
        return {'move': result.split()[1], 'depth': int(info[2]), 'score': int(info[5])}

    try:
        for mode in ['fresh', 'sequential']:
            for label, root in [('baseline', args.baseline), ('candidate', args.candidate)]:
                source = root / 'src'
                report['sources'][label] = str(source)
                key = mode + '_' + label
                report['runs'][key] = []
                session = None
                try:
                    for index, fen in enumerate(fens):
                        if session is None:
                            session = start(source)
                        report['runs'][key].append(search(session, fen))
                        if mode == 'fresh':
                            try:
                                session.finish()
                            finally:
                                try:
                                    (args.output / f'{key}_{index:02}.log').write_text(session.transcript())
                                finally:
                                    session.close()
                                    session = None
                        save()
                        print(key, index + 1, '/ 50', flush=True)
                    if session is not None:
                        session.finish()
                finally:
                    if session is not None:
                        try:
                            (args.output / f'{key}.log').write_text(session.transcript())
                        finally:
                            session.close()
            if report['runs'][mode + '_baseline'] != report['runs'][mode + '_candidate']:
                report['status'] = 'HOLD'
                return
        report['status'] = 'PASS'
    except BaseException as exc:
        report['status'] = 'FAIL'
        report['error'] = repr(exc)
        raise
    finally:
        save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='stage', required=True)
    selection = sub.add_parser('select')
    selection.add_argument('--book', type=Path, required=True)
    selection.add_argument('--output', type=Path, required=True)
    execution = sub.add_parser('run')
    execution.add_argument('--identity', type=Path, required=True)
    execution.add_argument('--baseline', type=Path, required=True)
    execution.add_argument('--candidate', type=Path, required=True)
    execution.add_argument('--fens', type=Path, required=True)
    execution.add_argument('--fens-sha', required=True)
    execution.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.stage == 'select':
        select(args.book, args.output)
    else:
        run(args)
        raise SystemExit(0 if json.loads((args.output / 'parity.json').read_text())['status'] == 'PASS' else 3)

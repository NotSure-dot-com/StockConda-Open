"""Frozen rev2 dispatch/stop latency with continuous, CPU-pinned readers."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import signal


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def quantiles(values):
    ordered = sorted(values)
    assert ordered
    return {name: ordered[math.ceil(p * len(ordered)) - 1]
            for name, p in [('median', .5), ('p95', .95), ('p99', .99)]}


def decision(base, candidate):
    return ('PASS' if candidate['bestmove']['median'] <= .9 * base['bestmove']['median']
            and candidate['bestmove']['p95'] <= 1.05 * base['bestmove']['p95']
            and candidate['stop']['p95'] <= 1.05 * base['stop']['p95'] else 'HOLD')


def validate_timestamps(row):
    assert row['go_ns'] <= row['info_ns'] <= row['bestmove_ns']
    assert row['bestmove_ns'] - row['go_ns'] <= 5_000_000_000
    if row['mode'] == 'stop':
        assert row['info_ns'] <= row['stop_ns'] <= row['bestmove_ns']


def main(args):
    import chess
    identity = json.loads(args.identity.read_text())
    assert identity['FG-ENV'] == identity['FG-TYPE'] == 'PASS'
    parity = json.loads(args.parity.read_text())
    assert parity['status'] == 'PASS' and parity['identity_sha256'] == sha(args.identity)
    roots = {label: Path(entry['root']) for label, entry in identity['snapshots'].items()}
    assert roots['baseline'].resolve() != roots['candidate'].resolve()
    for label, root in roots.items():
        for relative, expected in identity['snapshots'][label]['source'].items():
            if Path(relative).suffix in {'.py', '.pyi', '.pyx', '.pxd', '.h'}:
                assert sha(root / relative) == expected, relative
        for relative, expected in identity['snapshots'][label]['extensions'].items():
            assert sha(root / relative) == expected, relative
    sys.path.insert(0, str(roots['candidate'] / 'tests'))
    from uci_harness import UciSession
    from ap03_uci_audit import audit
    assert Path(sys.modules['uci_harness'].__file__).resolve() == (roots['candidate'] / 'tests/uci_harness.py').resolve()

    class TimedSession(UciSession):
        def __init__(self, root):
            self.receipts = {}
            self.reader_affinities = {}
            os.environ['STOCKCONDA_TEST_SOURCE'] = str(root / 'src')
            try:
                super().__init__(['taskset', '-c', '0', sys.executable, '-u', '-m', 'chess_engine.stockconda'])
            except BaseException:
                if hasattr(self, 'proc'):
                    if self.proc.poll() is None:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    self.proc.wait(timeout=2)
                    for reader in getattr(self, 'readers', []):
                        if reader.ident is not None:
                            reader.join(timeout=2)
                    for stream in [self.proc.stdin, self.proc.stdout, self.proc.stderr]:
                        if stream is not None:
                            stream.close()
                raise

        def _read(self, channel, stream):
            os.sched_setaffinity(0, {1})
            self.reader_affinities[channel] = sorted(os.sched_getaffinity(0))
            try:
                for line in stream:
                    stamp = time.perf_counter_ns()
                    with self.condition:
                        index = len(self.events)
                        self.receipts[index] = stamp
                        self.events.append((stamp / 1e9, channel, line.rstrip('\n')))
                        self.condition.notify_all()
            finally:
                with self.condition:
                    self.eof.add(channel)
                    self.condition.notify_all()

        def timed_send(self, command):
            assert self.proc.poll() is None
            with self.condition:
                marker = len(self.events)
                self.events.append((time.perf_counter_ns() / 1e9, 'stdin', command))
            stamp = time.perf_counter_ns()
            self.proc.stdin.write(command + '\n')
            self.proc.stdin.flush()
            return marker, stamp

        def received(self, marker, prefix):
            self.wait(lambda line: line.startswith(prefix), marker, 5)
            with self.condition:
                return next(self.receipts[i] for i in range(marker, len(self.events))
                            if self.events[i][1] == 'stdout' and self.events[i][2].startswith(prefix))

    args.output.mkdir(exist_ok=False)
    os.sched_setaffinity(0, {1})
    order = ['baseline', 'candidate', 'candidate', 'baseline', 'baseline',
             'candidate', 'candidate', 'baseline', 'baseline', 'candidate']
    report = {'status': 'RUNNING', 'identity_sha256': sha(args.identity),
              'runner_sha256': sha(Path(__file__)), 'harness_sha256': sha(Path(sys.modules['uci_harness'].__file__)),
              'audit_sha256': sha(Path(sys.modules['ap03_uci_audit'].__file__)),
              'order': order, 'warmups_per_block': 200, 'samples_per_block': 1000,
              'engine_cpu': 0, 'reader_cpu': 1, 'driver_cpu': 1,
              'options': {'Book': False, 'Hash': 16, 'MaxDepth': 64,
                          'Ponder': 'off (no UCI Ponder option or pondering implementation)'},
              'blocks': []}
    def save():
        (args.output / 'latency.json').write_text(json.dumps(report, indent=2) + '\n')
    save()  # Freeze hashes/environment/options before any candidate measurements.
    pooled = {label: {key: [] for key in ['info_depth1', 'bestmove', 'stop']} for label in roots}
    board = chess.Board()
    try:
        for mode in ['depth1', 'stop']:
            for block, label in enumerate(order):
                session = TimedSession(roots[label])
                rows = []
                prefix = mode + '_' + str(block) + '_' + label
                try:
                    session.handshake()
                    session.send('setoption name Hash value 16')
                    session.send('setoption name MaxDepth value 64')
                    session.fence()
                    session.ready()
                    assert os.sched_getaffinity(session.proc.pid) == {0}
                    assert session.reader_affinities == {'stdout': [1], 'stderr': [1]}
                    assert not any('option name Ponder ' in event[2] for event in session.events)
                    with (args.output / (prefix + '.jsonl')).open('x') as raw:
                        for request in range(1200):
                            session.send('ucinewgame')
                            session.send('position startpos')
                            session.ready()
                            session.roots.append(board.copy())
                            marker, t0 = session.timed_send('go depth 1' if mode == 'depth1' else 'go infinite')
                            ti = session.received(marker, 'info depth 1 ')
                            if mode == 'stop':
                                _, ts = session.timed_send('stop')
                            tb = session.received(marker, 'bestmove ')
                            row = {'mode': mode, 'block': block, 'label': label, 'request': request,
                                   'warmup': request < 200, 'go_ns': t0, 'info_ns': ti}
                            if mode == 'stop':
                                assert tb >= ts, 'bestmove received before stop'
                                row.update(stop_ns=ts, bestmove_ns=tb, stop=tb - ts)
                                assert 0 <= row['stop'] <= 5_000_000_000
                            else:
                                row.update(bestmove_ns=tb, info_depth1=ti - t0, bestmove=tb - t0)
                                assert 0 <= ti - t0 <= tb - t0 <= 5_000_000_000
                            validate_timestamps(row)
                            raw.write(json.dumps(row) + '\n')
                            rows.append(row)
                    session.finish()
                    audit((channel, line) for _, channel, line in session.events)
                    keys = ['stop'] if mode == 'stop' else ['info_depth1', 'bestmove']
                    metrics = {}
                    for key in keys:
                        values = [row[key] for row in rows if not row['warmup']]
                        assert len(values) == 1000
                        metrics[key] = quantiles(values)
                        pooled[label][key].extend(values)
                    report['blocks'].append({'mode': mode, 'block': block, 'label': label,
                                             'reader_affinities': session.reader_affinities, 'metrics_ns': metrics})
                    save()
                    print(prefix, 'complete', flush=True)
                finally:
                    try:
                        (args.output / (prefix + '.log')).write_text(session.transcript())
                    finally:
                        session.close()
        assert all(len(values) == 5000 for label in pooled.values() for values in label.values())
        report['pooled_ns'] = {label: {key: quantiles(values) for key, values in groups.items()}
                               for label, groups in pooled.items()}
        report['status'] = decision(report['pooled_ns']['baseline'], report['pooled_ns']['candidate'])
    except BaseException as exc:
        report.update(status='FAIL', error=repr(exc))
        raise
    finally:
        save()
    return 0 if report['status'] == 'PASS' else 3


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--identity', type=Path, required=True)
    parser.add_argument('--parity', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    raise SystemExit(main(parser.parse_args()))

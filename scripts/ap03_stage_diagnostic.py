"""Fixed BCCB depth1 stage diagnostic, not a formal latency gate."""
import hashlib
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sys

import chess


def quantile(values):
    ordered = sorted(values)
    return {name: ordered[math.ceil(p * len(ordered)) - 1]
            for name, p in [('median', .5), ('p95', .95)]}


root = Path(sys.argv[1])
root.mkdir(exist_ok=False)
identity_path = Path('/results/ap03_gate_identity_types_acceptance1.json')
identity = json.loads(identity_path.read_text())
roots = {label: Path(entry['root']) for label, entry in identity['snapshots'].items()}
for label, snapshot in roots.items():
    for relative, expected in identity['snapshots'][label]['source'].items():
        if Path(relative).suffix in {'.py', '.pyi', '.pyx', '.pxd', '.h'}:
            assert hashlib.sha256((snapshot / relative).read_bytes()).hexdigest() == expected
    for relative, expected in identity['snapshots'][label]['extensions'].items():
        assert hashlib.sha256((snapshot / relative).read_bytes()).hexdigest() == expected
sys.path.insert(0, str(roots['candidate'] / 'tests'))
from uci_harness import UciSession

os.sched_setaffinity(0, {1})
summary = dict(purpose='instrumented diagnosis only; no acceptance decision', blocks=[])
for block, label in enumerate(['baseline', 'candidate', 'candidate', 'baseline']):
    os.environ['STOCKCONDA_TEST_SOURCE'] = str(roots[label] / 'src')
    output = root / f'{block}_{label}.json'
    with closing(UciSession(['taskset', '-c', '0', sys.executable, '-u',
                             '/build/ap03_stage_probe.py', str(output)])) as session:
        session.handshake()
        session.send('setoption name Hash value 16')
        session.send('setoption name MaxDepth value 64')
        session.fence()
        session.ready()
        try:
            for request in range(1200):
                session.send('ucinewgame')
                session.send('position startpos')
                session.ready()
                marker = session.go(chess.Board(), 'go depth 1')
                session.bestmove(marker)
            session.finish()
        finally:
            (root / f'{block}_{label}.log').write_text(session.transcript())
    data = json.loads(output.read_text())
    assert len(data) == 1200
    stages = {'input_to_worker_wall': ('go_enter', 'worker_enter', 0),
              'worker_wall': ('worker_enter', 'worker_return', 0),
              'worker_cpu': ('worker_enter', 'worker_return', 1),
              'search_wall': ('search_enter', 'search_return', 0),
              'search_cpu': ('search_enter', 'search_return', 1),
              'prepare_output_cpu': ('search_return', 'output_enter', 1),
              'output_write_wall': ('output_enter', 'output_return', 0),
              'output_write_cpu': ('output_enter', 'output_return', 1)}
    metrics = {}
    for name, (begin, end, clock) in stages.items():
        values = [r[end][clock] - r[begin][clock] for r in data[200:]]
        assert min(values) >= 0
        metrics[name] = quantile(values)
    invalid_time_lines = [line for _, channel, line in session.events
                          if channel == 'stdout' and ' time -' in line]
    summary['blocks'].append(dict(block=block, label=label, metrics_ns=metrics,
                                  negative_time_lines=invalid_time_lines))
    (root / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(block, label, metrics, flush=True)

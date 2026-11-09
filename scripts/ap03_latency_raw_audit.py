"""Independent raw-data audit; no benchmark helper imports or engine execution."""
import hashlib
import json
from pathlib import Path
import sys


root = Path(sys.argv[1])
report = json.loads((root / 'latency.json').read_text())
assert len(report['blocks']) == 20
collected = {label: {metric: [] for metric in ['info_depth1', 'bestmove', 'stop']}
             for label in ['baseline', 'candidate']}
hashes = {}
for mode in ['depth1', 'stop']:
    for block, label in enumerate(['baseline', 'candidate', 'candidate', 'baseline', 'baseline',
                                   'candidate', 'candidate', 'baseline', 'baseline', 'candidate']):
        prefix = f'{mode}_{block}_{label}'
        path = root / (prefix + '.jsonl')
        data = path.read_bytes()
        hashes[path.name] = hashlib.sha256(data).hexdigest()
        rows = [json.loads(line) for line in data.splitlines()]
        assert len(rows) == 1200
        for index, row in enumerate(rows):
            assert (row['mode'], row['block'], row['label'], row['request'], row['warmup']) == (
                mode, block, label, index, index < 200)
            assert row['go_ns'] <= row['info_ns'] <= row['bestmove_ns']
            assert row['bestmove_ns'] - row['go_ns'] <= 5_000_000_000
            expected = ({'info_depth1': row['info_ns'] - row['go_ns'],
                         'bestmove': row['bestmove_ns'] - row['go_ns']} if mode == 'depth1'
                        else {'stop': row['bestmove_ns'] - row['stop_ns']})
            if mode == 'stop':
                assert row['info_ns'] <= row['stop_ns'] <= row['bestmove_ns']
            for metric, value in expected.items():
                assert row[metric] == value
                if index >= 200:
                    collected[label][metric].append(value)
        log = root / (prefix + '.log')
        hashes[log.name] = hashlib.sha256(log.read_bytes()).hexdigest()
        assert sum(' stdout: bestmove ' in line for line in log.read_text().splitlines()) == 1200

metrics = {}
for label, groups in collected.items():
    metrics[label] = {}
    for metric, values in groups.items():
        assert len(values) == 5000
        ordered = sorted(values)
        metrics[label][metric] = dict(median=ordered[2499], p95=ordered[4749], p99=ordered[4949])
assert metrics == report['pooled_ns']
base, candidate = metrics['baseline'], metrics['candidate']
checks = dict(median=candidate['bestmove']['median'] * 10 <= base['bestmove']['median'] * 9,
              bestmove_p95=candidate['bestmove']['p95'] * 100 <= base['bestmove']['p95'] * 105,
              stop_p95=candidate['stop']['p95'] * 100 <= base['stop']['p95'] * 105)
verdict = 'PASS' if all(checks.values()) else 'HOLD'
assert verdict == report['status']
audit = dict(raw_integrity='PASS', samples=24000, measured_requests=20000,
             warmup_requests=4000, checks=checks, numerical_verdict=verdict,
             metrics_ns=metrics, sha256=hashes)
(root / 'raw_audit.json').write_text(json.dumps(audit, indent=2) + '\n')
print(json.dumps({key: value for key, value in audit.items() if key != 'sha256'}, indent=2))

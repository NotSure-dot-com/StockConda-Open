"""Read-only descriptive analysis; never emits an acceptance verdict."""
from datetime import datetime
import json
import math
from pathlib import Path
import sys


def quantiles(values):
    ordered = sorted(values)
    return {name: ordered[math.ceil(p * len(ordered)) - 1]
            for name, p in [('median', .5), ('p95', .95), ('max', 1)]}


root = Path(sys.argv[1])
linux = [json.loads(line) for line in (root / 'linux.jsonl').read_text().splitlines()]
host = [json.loads(line) for line in Path(sys.argv[2]).read_text().splitlines()]
offsets = [r['wall_ns'] - r['mono_ns'] for r in linux]
offset = sorted(offsets)[len(offsets)//2]
host_rows = []
for row in host:
    assert 'error' not in row, row
    row['wall_ns'] = int(datetime.fromisoformat(row['utc']).timestamp() * 1e9)
    host_rows.append(row)

report = {'purpose': 'diagnostic only; incomplete protocol-valid comparison',
          'clock_offset_range_ns': max(offsets) - min(offsets),
          'linux_samples': len(linux), 'host_samples': len(host), 'blocks': []}
pooled = {name: [] for name in ['baseline', 'candidate']}
for path in sorted((root / 'measurement').glob('depth1_*.jsonl')):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    measured = [r for r in rows if not r['warmup']]
    label = rows[0]['label']
    pooled[label].extend(r['bestmove'] for r in measured)
    start, end = rows[0]['go_ns'], rows[-1]['bestmove_ns']
    loads = [r for r in host_rows if start + offset <= r['wall_ns'] <= end + offset]
    cpu = [next(c['PercentProcessorTime'] for c in r['cpu'] if c['Name'] == '_Total') for r in loads]
    report['blocks'].append(dict(file=path.name, requests=len(rows), measured=len(measured),
                                bestmove_ns=quantiles([r['bestmove'] for r in measured]),
                                info_to_best_ns=quantiles([r['bestmove_ns'] - r['info_ns'] for r in measured]),
                                host_samples=len(loads), host_cpu_percent=cpu))
report['descriptive_pooled_ns'] = {name: quantiles(values) for name, values in pooled.items()}
report['host_cpu_percent'] = quantiles([next(c['PercentProcessorTime'] for c in r['cpu'] if c['Name'] == '_Total') for r in host_rows])
report['battery_observed'] = list({(b['BatteryStatus'], b['EstimatedChargeRemaining']) for r in host for b in r['battery']})
report['pressure_samples'] = [r['pressure'].splitlines()[0] for r in linux]
report['clock_offset_deltas_ns'] = [b-a for a, b in zip(offsets, offsets[1:])]
if len(sys.argv) > 3:
    with Path(sys.argv[3]).open('x') as destination:
        destination.write(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))

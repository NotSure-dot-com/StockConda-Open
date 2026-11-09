"""Synthetic controls for frozen quantiles and acceptance boundaries; no engines."""
from copy import deepcopy
from ap03_latency import quantiles, decision, validate_timestamps


assert quantiles(list(range(100, 0, -1))) == {'median': 50, 'p95': 95, 'p99': 99}
assert quantiles([1000, 1, 2]) == {'median': 2, 'p95': 1000, 'p99': 1000}
baseline = {'bestmove': {'median': 100, 'p95': 100}, 'stop': {'p95': 100}}
boundary = {'bestmove': {'median': 90, 'p95': 105}, 'stop': {'p95': 105}}
assert decision(baseline, boundary) == 'PASS'
for key, metric in [('bestmove', 'median'), ('bestmove', 'p95'), ('stop', 'p95')]:
    broken = deepcopy(boundary)
    broken[key][metric] += 1
    assert decision(baseline, broken) == 'HOLD', (key, metric)
print('Quantiles: 2 controls PASS; exact boundary PASS; 3 individual threshold violations HOLD')
valid = dict(mode='stop', go_ns=0, info_ns=100, stop_ns=120, bestmove_ns=200)
validate_timestamps(valid)
for invalid in [dict(valid, info_ns=4_900_000_000, stop_ns=4_900_000_001, bestmove_ns=5_100_000_000),
                dict(valid, bestmove_ns=119), dict(valid, info_ns=-1)]:
    try:
        validate_timestamps(invalid)
    except AssertionError:
        continue
    raise AssertionError('Invalid timing accepted')
print('Timestamp ordering/whole-request deadline: valid accepted; 3 invalid controls rejected')

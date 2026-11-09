"""A one-millisecond UCI/config deviation must fail even with a plausible PGN."""
from pathlib import Path
from ap03_match_audit import audit_trace, validate_time_config
import json

root = Path('/results/ap03_preflight1')
trace = (root / 'protocol.log').read_text()
audit_trace(trace.splitlines(), 1, expected_increment=100)
bad = trace.replace('winc 100 binc 100', 'winc 99 binc 100', 1)
assert bad != trace
try:
    audit_trace(bad.splitlines(), 1, expected_increment=100)
except AssertionError:
    pass
else:
    raise AssertionError('wrong UCI increment accepted')
saved = json.loads((root / 'autosave.json').read_text())
validate_time_config(saved, '10+0.1')
saved['engines'][1]['limit']['tc']['increment'] = 99
try:
    validate_time_config(saved, '10+0.1')
except AssertionError:
    pass
else:
    raise AssertionError('wrong saved increment accepted')
print('PASS: UCI and saved-config millisecond mutations rejected')

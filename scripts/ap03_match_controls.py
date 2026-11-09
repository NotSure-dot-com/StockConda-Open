"""Before-launch controls: concurrent trace attribution and frozen Elo decisions."""
import json
from pathlib import Path
import re
import shutil
import tempfile

from ap03_match_audit import audit_trace, audit_match, elo_decision, final_summary

source = Path('/results/ap03_preflight1')
trace = (source/'protocol.log').read_text()
assert audit_trace(trace.splitlines(), 1)['searches'] == 392
clone = re.sub(r'<\s*(\d+)>', lambda m: '<' + str(int(m[1]) + 1000000000000) + '>', trace)
clone = re.sub(r'Process with pid: (\d+)', lambda m: 'Process with pid: ' + str(int(m[1]) + 1000000), clone)
assert audit_trace((trace + '\n' + clone).splitlines(), 2)['searches'] == 784
exit_line = next(line for line in trace.splitlines() if 'Process exited normally with status 0' in line)
clone_exit = re.sub(r'<\s*(\d+)>', lambda m: '<' + str(int(m[1]) + 1000000000000) + '>', exit_line)
try:
    audit_trace((trace + '\n' + clone.replace(clone_exit, exit_line)).splitlines(), 2)
except AssertionError:
    pass
else:
    raise AssertionError('cross-worker shutdown mutation escaped')
console = (source/'console.log').read_text()
for bad in [console.replace('Games: 4,', 'Games: 2,'),
            console.replace('Wins: 0,', 'Wins: 1,'),
            console.replace('[0, 1, 1, 0, 0]', '[0, 0, 2, 0, 0]'),
            console + '\nResults of AP03_PersistentWorker vs AP03_Baseline_9acfd60 (x):\nGames: 4, Wins: 0, Losses: 1, Draws: 3,\nPtnml(0-2): [0, 1, 1, 0, 0]\nFinished match']:
    try:
        final_summary(bad, 4, [0, 1, 3], [0, 1, 1, 0, 0])
    except AssertionError:
        continue
    raise AssertionError('final summary mutation escaped')
best = next(line for line in trace.splitlines() if '---> bestmove ' in line)
for bad in [trace.replace(best, '', 1), trace.replace(best, best+'\n'+best, 1),
            trace.replace(best, re.sub(r'bestmove .*', 'bestmove e2e5', best), 1),
            trace.replace('Process exited normally with status 0', 'fatal engine exception', 1)]:
    try:
        audit_trace(bad.splitlines(), 1)
    except (AssertionError, ValueError):
        continue
    raise AssertionError('trace mutation escaped')
with tempfile.TemporaryDirectory(prefix='ap03_match_controls_') as folder:
    root = Path(folder)
    for name in ['run.json', 'protocol.log', 'games.pgn', 'console.log', 'autosave.json']:
        shutil.copyfile(source/name, root/name)
    assert audit_match(root, 4, '10+0.1', 1)['status'] == 'PASS'
    original = (root/'games.pgn').read_text()
    for bad in [original.replace('[Round "1"]', '[Round "2"]', 1),
                original.replace('[Termination "normal"]', '[Termination "time forfeit"]', 1)]:
        (root/'games.pgn').write_text(bad)
        try:
            audit_match(root, 4, '10+0.1', 1)
        except AssertionError:
            continue
        raise AssertionError('PGN mutation escaped')
assert elo_decision(0, 10)['status'] == 'PASS'
assert elo_decision(-40, 10)['status'] == 'FAIL'
assert elo_decision(-20, 10)['status'] == 'HOLD'
assert elo_decision(-20, 0)['status'] == 'HOLD'
print('PASS: real preflight, two-worker trace, 5 trace faults, 4 final-summary faults, 2 PGN faults, 4 Elo controls')

"""Identity-bound four-game technical preflight; no Elo interpretation."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


identity_path = Path('/results/ap03_gate_identity_types_acceptance2.json')
identity = json.loads(identity_path.read_text())
assert sha(identity_path) == 'e895bcd6b1d6fecd6e1fe059a9dba10b9c82811ffe7175224077dde84983019a'
assert identity['FG-ENV'] == identity['FG-TYPE'] == 'PASS'
roots = {}
for label, snapshot in identity['snapshots'].items():
    root = Path(snapshot['root'])
    roots[label] = root
    for relative, expected in snapshot['source'].items():
        if Path(relative).suffix in {'.py', '.pyi', '.pyx', '.pxd', '.h'}:
            assert sha(root / relative) == expected, relative
    for relative, expected in snapshot['extensions'].items():
        assert sha(root / relative) == expected, relative
    assert sha(root / 'ap03-launch.sh') == snapshot['launcher_sha256']
book = Path('/reference/books/8moves_v3.pgn')
fastchess = Path('/reference/tools/fastchess/fastchess')
assert sha(book) == identity['book_sha256']
assert sha(fastchess) == identity['fastchess_sha256']
output = Path('/results/ap03_preflight1')
output.mkdir(exist_ok=False)
command = [str(fastchess),
           '-engine', 'cmd=' + str(roots['candidate'] / 'ap03-launch.sh'), 'name=AP03_PersistentWorker',
           '-engine', 'cmd=' + str(roots['baseline'] / 'ap03-launch.sh'), 'name=AP03_Baseline_9acfd60',
           '-each', 'proto=uci', 'tc=10+0.1', 'option.Book=false', 'option.Hash=16',
           '-openings', 'file=' + str(book), 'format=pgn', 'order=sequential',
           '-srand', '90210', '-rounds', '2', '-repeat', '-concurrency', '1',
           '-use-affinity', '0-21',
           '-resign', 'movecount=3', 'score=600',
           '-draw', 'movenumber=34', 'movecount=8', 'score=20',
           '-report', 'penta=true', '-autosaveinterval', '20',
           '-config', 'outname=' + str(output / 'autosave.json'),
           '-pgnout', 'file=' + str(output / 'games.pgn'), 'append=false',
           '-log', 'file=' + str(output / 'protocol.log'), 'level=trace', 'engine=true',
           'append=false', 'realtime=true', '-strict']
report = dict(status='RUNNING', purpose='technical preflight only', command=command,
              identity_sha256=sha(identity_path), runner_sha256=sha(Path(__file__)),
              timeout_seconds=300, concurrency=1)
(output / 'run.json').write_text(json.dumps(report, indent=2))
with (output / 'console.log').open('x') as console:
    proc = subprocess.Popen(command, cwd=output, stdout=console, stderr=subprocess.STDOUT,
                            start_new_session=True)
    try:
        code = proc.wait(timeout=300)
        report.update(status='NEEDS_AUDIT' if code == 0 else 'FAIL', exit_code=code)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        report.update(status='FAIL', reason='outer timeout; forced cleanup')
(output / 'run.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2), flush=True)
raise SystemExit(0 if report['status'] == 'NEEDS_AUDIT' else 1)

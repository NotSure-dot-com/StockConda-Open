"""Real Fastchess boundary regression: exact milliseconds, no engine modifications."""
import json
from pathlib import Path
import re
import subprocess
import tempfile
import os
import signal
import ap03_match_pipeline as pipeline

with tempfile.TemporaryDirectory(prefix='ap03_tc_') as folder:
    root = Path(folder)
    for tc, expected in [('120+1.2', 1200), ('10+0.1', 100)]:
        output = root / str(expected)
        output.mkdir()
        if hasattr(pipeline, 'exact_match_command'):
            command = pipeline.exact_match_command(output, 2, tc, 1)
        else:
            command = json.loads(Path('/results/ap03_preflight1/run.json').read_text())['command']
            command = [p.replace('/results/ap03_preflight1/', str(output) + '/') for p in command]
            command[command.index('-rounds') + 1] = '1'
            command[command.index('tc=10+0.1')] = 'tc=' + tc
        command += ['-maxmoves', '1']
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=5)
            raise
        assert proc.returncode == 0, stdout + stderr
        saved = json.loads((output / 'autosave.json').read_text())
        actual = [e['limit']['tc']['increment'] for e in saved['engines']]
        print(tc, 'increments:', actual, flush=True)
        assert actual == [expected, expected], (tc, actual, expected)
        trace = (output / 'protocol.log').read_text()
        incs = re.findall(r'<--- go .*?winc (\d+) binc (\d+)', trace)
        assert incs and all(tuple(map(int, pair)) == (expected, expected) for pair in incs)
        assert f'[TimeControl "{tc}"]' in (output / 'games.pgn').read_text()
print('PASS: exact load/Elo increments in autosave, UCI and PGN')

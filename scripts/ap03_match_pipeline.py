"""Authorized load -> audited PASS -> fixed 2000-game match. No automatic retry."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from decimal import Decimal

from ap03_match_audit import audit_match

RESULTS = Path('/results')
IDENTITY = RESULTS / 'ap03_gate_identity_types_acceptance2.json'
EXPECTED = 'e895bcd6b1d6fecd6e1fe059a9dba10b9c82811ffe7175224077dde84983019a'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_identity():
    assert sha(IDENTITY) == EXPECTED
    identity = json.loads(IDENTITY.read_text())
    for snapshot in identity['snapshots'].values():
        root = Path(snapshot['root'])
        for relative, digest in snapshot['source'].items():
            if Path(relative).suffix in {'.py', '.pyi', '.pyx', '.pxd', '.h'}:
                assert sha(root / relative) == digest, relative
        for relative, digest in snapshot['extensions'].items():
            assert sha(root / relative) == digest, relative
        assert sha(root / 'ap03-launch.sh') == snapshot['launcher_sha256']
    assert sha(Path('/reference/books/8moves_v3.pgn')) == identity['book_sha256']
    assert sha(Path('/reference/tools/fastchess/fastchess')) == identity['fastchess_sha256']


def exact_match_command(output, games, tc, concurrency):
    preflight = RESULTS / 'ap03_preflight1/run.json'
    assert sha(preflight) == '87b8a33af998986f6903674db371c173b60b6e3c9fa77a8dce1d48c95a65756d'
    command = json.loads(preflight.read_text())['command']
    command = [part.replace('/results/ap03_preflight1/', str(output) + '/') for part in command]
    command[command.index('-rounds') + 1] = str(games // 2)
    command[command.index('-concurrency') + 1] = str(concurrency)
    template = RESULTS / 'ap03_preflight1/autosave.json'
    assert sha(template) == '447cdd835a5bee1a79b92146ea88a47f0c5fecc690818f0419c418a8edebfb53'
    config = json.loads(template.read_text())
    config['stats'] = {}
    base, increment = (Decimal(value) * 1000 for value in tc.split('+'))
    assert base == int(base) and increment == int(increment)
    for engine in config['engines']:
        engine['limit']['tc'].update(time=int(base), increment=int(increment))
    config_path = output / 'input.json'
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    # Load exact integer milliseconds first; all remaining frozen CLI options
    # override tournament paths/settings. Do not send decimal tc through -each.
    return [command[0], '-config', 'file=' + str(config_path), 'stats=false'] + command[command.index('-openings'):]


def run_stage(stage, games, tc, timeout):
    verify_identity()
    output = RESULTS / ('ap03_' + stage + '2')
    output.mkdir(exist_ok=False)
    command = exact_match_command(output, games, tc, 11)
    report = dict(status='RUNNING', stage=stage, games=games, tc=tc, concurrency=11,
                  command=command, identity_sha256=EXPECTED, started_epoch=time.time(),
                  runner_sha256=sha(Path(__file__)),
                  auditor_sha256=sha(Path(__file__).with_name('ap03_match_audit.py')),
                  input_sha256=sha(output / 'input.json'),
                  outer_limit_seconds=timeout)
    def save():
        temp = output / 'run.tmp'
        temp.write_text(json.dumps(report, indent=2))
        temp.replace(output / 'run.json')
    save()
    with (output / 'console.log').open('x') as console:
        proc = subprocess.Popen(command, cwd=output, stdout=console, stderr=subprocess.STDOUT,
                                start_new_session=True)
        report['pid'] = proc.pid
        save()
        try:
            code = proc.wait(timeout=timeout)
            report.update(exit_code=code, status='NEEDS_AUDIT' if code == 0 else 'FAIL')
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
            report.update(status='FAIL', reason='outer timeout; forced cleanup')
        finally:
            report['ended_epoch'] = time.time()
            save()
    if report['status'] != 'NEEDS_AUDIT':
        return 'FAIL'
    try:
        result = audit_match(output, games, tc, 11, elo=(stage == 'elo'))
    except Exception as exc:
        report.update(status='FAIL', reason='audit: ' + repr(exc))
        save()
        return 'FAIL'
    # audit.json is the final verdict; keep its hashed run.json input immutable.
    return result['status']


if __name__ == '__main__':
    assert sha(RESULTS/'ap03_preflight1/audit.json') == '84eddc310e3b4d551d7ec13aad0647f71ab883ab796f50ca6add998fe56dfa85'
    assert json.loads((RESULTS/'ap03_preflight1/audit.json').read_text())['status'] == 'PASS'
    print('Starting load gate; Elo cannot start before audited PASS.', flush=True)
    load = run_stage('load', 20, '120+1.2', 3600)
    print('Load gate:', load, flush=True)
    if load != 'PASS':
        raise SystemExit(1)
    print('Starting fixed 2000-game non-inferiority gate.', flush=True)
    elo = run_stage('elo', 2000, '10+0.1', 43200)
    print('Elo gate:', elo, flush=True)
    raise SystemExit(0 if elo == 'PASS' else 3 if elo == 'HOLD' else 1)

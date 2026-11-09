"""One execution of the unchanged frozen runner, with separate Linux telemetry."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--identity', type=Path, required=True)
    parser.add_argument('--parity', type=Path, required=True)
    parser.add_argument('--purpose', choices=['diagnostic', 'acceptance'], required=True)
    args = parser.parse_args()
    root = args.output
    root.mkdir(exist_ok=False)
    os.sched_setaffinity(0, {2})
    runner = Path('/build/ap03_latency.py')
    expected = '7f0046525fb296f58cdff3770833bc91cc2bb640f8eecf9f2117f48429feb297'
    assert hashlib.sha256(runner.read_bytes()).hexdigest() == expected
    metadata = dict(purpose=args.purpose,
                    identity=str(args.identity), parity=str(args.parity),
                    identity_sha256=hashlib.sha256(args.identity.read_bytes()).hexdigest(),
                    parity_sha256=hashlib.sha256(args.parity.read_bytes()).hexdigest(),
                    runner_sha256=expected, telemetry_cpu=2,
                    start_wall_ns=time.time_ns(), start_mono_ns=time.perf_counter_ns())
    (root / 'diagnostic.json').write_text(json.dumps(metadata, indent=2))
    with (root / 'console.log').open('x') as console, (root / 'linux.jsonl').open('x') as raw:
        command = ['timeout', '--kill-after=5', '900', sys.executable, str(runner),
                   '--identity', str(args.identity),
                   '--parity', str(args.parity),
                   '--output', str(root / 'measurement')]
        proc = subprocess.Popen(command, stdout=console, stderr=subprocess.STDOUT)
        try:
            while True:
                row = dict(wall_ns=time.time_ns(), mono_ns=time.perf_counter_ns())
                for name, source in [('stat', '/proc/stat'), ('pressure', '/proc/pressure/cpu'),
                                     ('loadavg', '/proc/loadavg')]:
                    row[name] = Path(source).read_text()
                raw.write(json.dumps(row) + '\n')
                raw.flush()
                try:
                    result = proc.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
    metadata.update(exit_code=result, end_wall_ns=time.time_ns(), end_mono_ns=time.perf_counter_ns())
    (root / 'diagnostic.json').write_text(json.dumps(metadata, indent=2))
    (root / 'done').touch()
    print(json.dumps(metadata, indent=2), flush=True)
    return result


if __name__ == '__main__':
    raise SystemExit(main())

"""Reproducible external identity/build/type gates; never runs legacy tests.

Run inside the AP03 container. Writes only under /build and /results.
This runner deliberately stops at FG-TYPE; later gates require an explicit PASS.
"""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import argparse


BASE = '9acfd605d8a5edb99d98089a49304c06ed01f1fb'
CANDIDATE = 'd50fb4c2e8d01fb32ad5b1b88c557789c3098a2a'
GATE = 'f6aa6e71b85f5fea0b0570b51893da5c88169593'
RESULTS = Path('/results')
BOOK = Path('/reference/books/8moves_v3.pgn')
BOOK_SHA = '5835239f88cc2c7511b177c32392a69f3ede21819cf0616f80a7f907cd21d17e'
report = {'gate_revision': 2, 'gate_commit': GATE, 'snapshots': {}, 'commands': []}
RUN_ID = ''
ARCHIVE = RESULTS / 'ap03_candidate_source.tar'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(name, args, cwd, env=None, timeout=180):
    name += RUN_ID
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                            text=True, timeout=timeout)
    log = RESULTS / (name + '.log')
    log.write_text(result.stdout + result.stderr, encoding='utf-8')
    report['commands'].append({'name': name, 'args': args, 'cwd': str(cwd),
                               'exit': result.returncode, 'log_sha256': sha(log)})
    print(name, 'exit', result.returncode, flush=True)
    return result.returncode


def snapshot(label, commit, archive):
    root = Path('/build') / ('gate_' + label + '_' + commit[:7] + RUN_ID)
    root.mkdir(exist_ok=False)
    with tarfile.open(archive) as source:
        assert source.pax_headers['comment'] == commit
        source.extractall(root, filter='data')
    # Capture pristine source identity before any generated build artifacts.
    files = {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob('*')) if p.is_file()}
    entry = {'commit': commit, 'archive_sha256': sha(archive), 'source': files,
             'source_state': 'clean exact Git archive before build', 'root': str(root)}
    report['snapshots'][label] = entry
    if command('ap03_gate_build_' + label,
               [sys.executable, 'setup.py', 'build_ext', '--inplace', '--force'], root):
        raise RuntimeError(label + ' build failed')
    launcher = root / 'ap03-launch.sh'
    launcher.write_text('#!/bin/sh\nexport PYTHONPATH=' + str(root / 'src') +
                        '\nexec ' + sys.executable + ' -u -m chess_engine.stockconda\n',
                        encoding='utf-8')
    launcher.chmod(0o755)
    entry['launcher_sha256'] = sha(launcher)
    entry['extensions'] = {str(p.relative_to(root)): sha(p)
                           for p in sorted((root / 'src').rglob('*.so'))}
    assert len(entry['extensions']) >= 3
    # Build-generated C/C++/HTML is allowed; Python and Cython source must not change.
    for relative, digest in files.items():
        if Path(relative).suffix in {'.py', '.pyx', '.pxd', '.h'}:
            assert sha(root / relative) == digest, relative
    return root


def main():
    assert sha(BOOK) == BOOK_SHA
    report['book_sha256'] = sha(BOOK)
    report['python'] = sys.version
    report['versions'] = {p: importlib.metadata.version(p)
                          for p in ['chess', 'Cython', 'mypy', 'pytest', 'numpy']}
    assert report['versions']['mypy'] == '1.15.0'
    report['affinity'] = sorted(os.sched_getaffinity(0))
    report['concurrency'] = 1
    report['compiler'] = subprocess.check_output(['gcc', '--version'], text=True)
    report['cpu'] = subprocess.check_output(['lscpu'], text=True)
    report['fastchess'] = subprocess.check_output(
        ['/reference/tools/fastchess/fastchess', '--version'], text=True)
    report['fastchess_sha256'] = sha('/reference/tools/fastchess/fastchess')
    report['runner_sha256'] = sha(__file__)
    report['container_image'] = 'sha256:417cbf33f87b5378849df37983552cd1f8bc8b62fe1ceabe004de816a55dff21'
    baseline = snapshot('baseline', BASE, RESULTS / 'baseline_9acfd60.tar')
    candidate = snapshot('candidate', CANDIDATE, ARCHIVE)
    report['FG-ENV'] = 'PASS'
    # Exact frozen commands, separate empty caches, same executable and dependencies.
    codes = []
    for label, root in [('baseline', baseline), ('candidate', candidate)]:
        env = dict(os.environ, MYPY_CACHE_DIR=str(root / '.gate_mypy'))
        codes.append(command('ap03_gate_type_' + label,
                             [sys.executable, '-m', 'mypy', '--check-untyped-defs',
                              '--ignore-missing-imports', 'src/chess_engine/stockconda.py'], root, env))
    strict = command('ap03_gate_type_controller',
                     [sys.executable, '-m', 'mypy', '--strict', '--ignore-missing-imports',
                      'src/chess_engine/uci_search_worker.py'], candidate,
                     dict(os.environ, MYPY_CACHE_DIR=str(candidate / '.strict_mypy')))
    syntax = command('ap03_gate_syntax',
                     [sys.executable, '-m', 'compileall', '-q', 'src', 'tests'], candidate)
    report['FG-TYPE'] = ('FAIL' if strict or syntax else
                         'PASS' if codes[1] == 0 else 'REVIEW_BASELINE_DIAGNOSTICS')
    report['later_gates'] = 'NOT_RUN'
    report['candidate_test_source'] = str(candidate / 'src')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', default=CANDIDATE)
    parser.add_argument('--archive', type=Path, default=ARCHIVE)
    parser.add_argument('--run-id', default='')
    options = parser.parse_args()
    if options.run_id and not options.run_id.replace('_', '').isalnum():
        parser.error('run-id must contain only letters, digits or underscores')
    CANDIDATE, ARCHIVE = options.candidate, options.archive
    RUN_ID = '_' + options.run_id if options.run_id else ''
    try:
        main()
    finally:
        (RESULTS / ('ap03_gate_identity_types' + RUN_ID + '.json')).write_text(
            json.dumps(report, indent=2) + '\n', encoding='utf-8')

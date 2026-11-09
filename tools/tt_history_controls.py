"""Run compiled TT-history mutation controls in isolated source copies.

The controller supplies an immutable staged source tree and a new external
result directory.  Every command receives a unique environment tag so only
that command and descendants which inherited the tag are cleaned up.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
from typing import Literal
import uuid
import xml.etree.ElementTree as ET


Edit = tuple[str, str, str, int]
Expected = Literal['assertion-rejected', 'survives']

IGNORED_DIRECTORIES = {
    '.git', '.mypy_cache', '.pytest_cache', '__pycache__', 'build',
}
IGNORED_SUFFIXES = {'.o', '.pyc'}


@dataclass(frozen=True)
class Control:
    name: str
    edits: list[Edit]
    target: str
    expected: Expected = 'assertion-rejected'


NATIVE = 'src/chess_engine/cython_ext/transposition.pyx'

CONTROLS = (
    Control(
        'history-compare-removed',
        [(NATIVE,
          'if entry_ptr.history_context != <uint64_t>history_context: return None',
          'if False: return None', 1)],
        'tests/test_tt_repetition_context.py::test_matching_history_values_and_move_hints[17-0]',
    ),
    Control(
        'history-functions-return-zero',
        [(NATIVE,
          'return mix_history(position_key ^ <uint64_t>0x243f6a8885a308d3)',
          'return 0', 1),
         (NATIVE,
          'return mix_history(parent_history ^ child_key ^ <uint64_t>0x9e3779b97f4a7c15)',
          'return 0', 1)],
        'tests/test_search_history_context.py::test_bootstrap_is_ordered_and_preserves_the_board[False-False]',
    ),
    Control(
        'reduced-child-uses-parent-context',
        [('src/chess_engine/stockconda.py',
          """                    child_value, _ = self.pvs_search(
                        board, reduced_depth, -alpha - 1, -alpha, lmr_depth,
                        ply + 1, tt_context=child_context)""",
          """                    child_value, _ = self.pvs_search(
                        board, reduced_depth, -alpha - 1, -alpha, lmr_depth,
                        ply + 1, tt_context=tt_context)""",
          1)],
        'tests/test_search_history_context.py::test_sibling_and_research_contexts_match_independent_history',
    ),
    Control(
        'full-research-uses-parent-context',
        [('src/chess_engine/stockconda.py',
          """                        child_value, _ = self.pvs_search(
                            board, depth - 1, -beta, -alpha, lmr_depth,
                            ply + 1, tt_context=child_context)""",
          """                        child_value, _ = self.pvs_search(
                            board, depth - 1, -beta, -alpha, lmr_depth,
                            ply + 1, tt_context=tt_context)""",
          1)],
        'tests/test_search_history_context.py::test_sibling_and_research_contexts_match_independent_history',
    ),
    Control(
        'every-history-step-zeroes',
        [(NATIVE, '    if zeroing:\n', '    if True:\n', 1)],
        'tests/test_search_history_context.py::test_repeated_occurrences_do_not_cancel_and_king_move_does_not_reset',
    ),
    Control(
        'lowerbound-history-compare-bypassed',
        [(NATIVE,
          'if entry_ptr.history_context != <uint64_t>history_context: return None',
          ('if entry_ptr.node_type == NODE_TYPE_EXACT and '
           'entry_ptr.history_context != <uint64_t>history_context: return None'),
          1)],
        'tests/test_tt_repetition_context.py::test_history_mismatch_cannot_narrow_child_window[LOWERBOUND-5]',
    ),
    Control(
        'upperbound-history-compare-bypassed',
        [(NATIVE,
          'if entry_ptr.history_context != <uint64_t>history_context: return None',
          ('if entry_ptr.node_type == NODE_TYPE_EXACT and '
           'entry_ptr.history_context != <uint64_t>history_context: return None'),
          1)],
        'tests/test_tt_repetition_context.py::test_history_mismatch_cannot_narrow_child_window[UPPERBOUND--5]',
    ),
    Control(
        'none-query-coerced-to-zero',
        [(NATIVE,
          'if not valid_history_context(history_context): return None',
          'if history_context is None: history_context = 0', 1)],
        'tests/test_tt_repetition_context.py::test_invalid_history_is_move_only[probe-None]',
    ),
    Control(
        'wrapper-rejects-all-probes',
        [(NATIVE,
          """    return probe_tt_entry_cy(board_hash, <uint8_t>depth, current_ply,
                             board.halfmove_clock, history_context)""",
          '    return None', 1)],
        'tests/test_tt_repetition_context.py::test_ancestor_values_stay_with_their_history[False-3]',
    ),
    Control(
        'halfmove-compare-removed',
        [(NATIVE,
          'if entry_ptr.halfmove_context != context: return None',
          'if False: return None', 1)],
        'tests/test_tt_halfmove_context.py::test_raw_clock_guard_is_independent_of_history[0]',
    ),
    Control(
        'worker-search-restores-bare-fen',
        [('src/chess_engine/stockconda.py',
          """        board = (self.board.copy() if board_fen is None
                 else _board_from_fen_moves(board_fen, moves))""",
          """        board = (self.board.copy() if board_fen is None
                 else chess.Board(board_fen))""",
          1)],
        'tests/test_repetition_history.py::test_uci_history_preserves_threefold_escape[cold-3-black]',
    ),
    Control(
        'worker-validation-restores-bare-fen',
        [('src/chess_engine/stockconda.py',
          '        root = _board_from_fen_moves(board_fen, moves)',
          '        root = chess.Board(board_fen)',
          1)],
        'tests/test_repetition_history.py::test_uci_history_preserves_threefold_escape[cold-3-black]',
    ),
    Control(
        'bool-store-cannot-alias-one',
        [(NATIVE,
          'return type(history) is int and 0 <= history < 18446744073709551616',
          'return isinstance(history, int) and 0 <= history < 18446744073709551616',
          1)],
        'tests/test_tt_repetition_context.py::test_invalid_history_is_move_only[store-True]',
    ),
    Control(
        'bool-probe-cannot-alias-one',
        [(NATIVE,
          'return type(history) is int and 0 <= history < 18446744073709551616',
          'return isinstance(history, int) and 0 <= history < 18446744073709551616',
          1)],
        'tests/test_tt_repetition_context.py::test_invalid_history_is_move_only[probe-True]',
    ),
)


def _included_files(root: Path, *, include_extensions: bool):
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRECTORIES for part in relative.parts):
            continue
        if path.suffix in IGNORED_SUFFIXES:
            continue
        if not include_extensions and path.suffix in {'.pyd', '.so'}:
            continue
        yield path


def _manifest(root: Path, *, include_extensions: bool = False) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(_included_files(root, include_extensions=include_extensions))
    }


def _tagged_processes(token: str, variable: str) -> list[int]:
    """Return only processes inheriting this runner's unguessable command tag."""
    proc_root = Path('/proc')
    if not proc_root.is_dir():
        return []
    marker = f'{variable}={token}'.encode()
    found = []
    for process in proc_root.iterdir():
        if not process.name.isdigit() or int(process.name) <= 1:
            continue
        try:
            environment = (process / 'environ').read_bytes().split(b'\0')
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if marker in environment:
            found.append(int(process.name))
    return sorted(found)


def _signal_tagged(token: str, variable: str, sig: signal.Signals) -> list[int]:
    signalled = []
    for pid in _tagged_processes(token, variable):
        # Recheck immediately before signalling in case the numeric PID was reused.
        if pid not in _tagged_processes(token, variable):
            continue
        try:
            os.kill(pid, sig)
            signalled.append(pid)
        except ProcessLookupError:
            pass
    return signalled


def _cleanup_tagged(token: str, variable: str) -> dict[str, list[int]]:
    initial = _tagged_processes(token, variable)
    terminated = _signal_tagged(token, variable, signal.SIGTERM) if initial else []
    deadline = time.monotonic() + 1.0
    while _tagged_processes(token, variable) and time.monotonic() < deadline:
        time.sleep(0.02)
    kill_signal = getattr(signal, 'SIGKILL', signal.SIGTERM)
    killed = _signal_tagged(token, variable, kill_signal)
    deadline = time.monotonic() + 1.0
    while _tagged_processes(token, variable) and time.monotonic() < deadline:
        time.sleep(0.02)
    return {
        'initial': initial,
        'terminated': terminated,
        'killed': killed,
        'remaining': _tagged_processes(token, variable),
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def _run_command(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    deadline: int,
    output: Path,
) -> dict[str, object]:
    token = uuid.uuid4().hex
    marker_variable = 'STOCKCONDA_CONTROL_COMMAND_ID'
    tagged_env = {**env, marker_variable: token}
    started = time.monotonic()
    record: dict[str, object] = {
        'command': command,
        'deadline_seconds': deadline,
        'run_id': token,
        'status': 'RUNNING',
    }
    log = ''
    process: subprocess.Popen[str] | None = None
    timed_out = False
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=tagged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            log, _ = process.communicate(timeout=deadline)
        except subprocess.TimeoutExpired:
            timed_out = True
            cleanup = _cleanup_tagged(token, marker_variable)
            if process.poll() is None:
                process.kill()
            try:
                log, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                log = 'Command output unavailable after timeout cleanup.\n'
            record['timeout_cleanup'] = cleanup
    except BaseException as exc:
        record['launch_or_capture_failure'] = repr(exc)
    finally:
        cleanup = _cleanup_tagged(token, marker_variable)
        (output / f'{name}.log').write_text(log, encoding='utf-8')
        record.update(
            elapsed_seconds=time.monotonic() - started,
            exit_code=None if process is None else process.returncode,
            timed_out=timed_out,
            cleanup=cleanup,
        )
        if timed_out:
            record['status'] = 'TIMEOUT_INFRASTRUCTURE_FAILURE'
        elif process is None:
            record['status'] = 'LAUNCH_INFRASTRUCTURE_FAILURE'
        elif 'launch_or_capture_failure' in record:
            record['status'] = 'CAPTURE_INFRASTRUCTURE_FAILURE'
        elif cleanup['initial']:
            record['status'] = 'DESCENDANT_CLEANUP_INFRASTRUCTURE_FAILURE'
        else:
            record['status'] = 'COMPLETED'
    return record


def _assert_target_failure(xml_path: Path, target: str, log: str) -> None:
    root = ET.parse(xml_path).getroot()
    cases = root.findall('.//testcase')
    errors = root.findall('.//error')
    failures = root.findall('.//failure')
    expected_name = target.rsplit('::', 1)[1]
    observed_names = [case.get('name') for case in cases]
    assert not errors, ('pytest produced collection/runtime errors', errors)
    assert len(cases) == 1 and observed_names == [expected_name], (
        'JUnit did not identify the intended single target', target, observed_names)
    assert len(failures) == 1, ('expected exactly one failed test body', log)
    failure_text = failures[0].text or ''
    assert 'AssertionError' in failure_text, ('target did not fail by assertion', log)


def _run_control(
    source: Path,
    output: Path,
    edits: list[Edit],
    target: str,
    expected: Expected,
) -> None:
    source = source.resolve(strict=True)
    output = output.resolve(strict=False)
    if source == output or source in output.parents:
        raise ValueError('output must be outside the source tree')
    output.mkdir(parents=True, exist_ok=False)
    build = output / 'source'
    result: dict[str, object] = {
        'status': 'RUNNING',
        'expected': expected,
        'input_source': str(source),
        'source': str(build),
        'target': target,
        'edits': edits,
        'python': platform.python_version(),
        'platform': platform.platform(),
        'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'commands': {},
    }

    def save() -> None:
        _write_json(output / 'result.json', result)

    save()
    try:
        result['input_manifest'] = _manifest(source)
        shutil.copytree(
            source,
            build,
            ignore=shutil.ignore_patterns(
                '.git', 'build', '__pycache__', '.pytest_cache', '.mypy_cache',
                '*.so', '*.o', '*.pyd', '*.pyc'),
        )
        result['copied_manifest'] = _manifest(build)
        assert result['input_manifest'] == result['copied_manifest'], (
            'isolated copy differs from staged input before mutation')

        changed = {}
        for relative, old, new, count in edits:
            path = build / relative
            text = path.read_text(encoding='utf-8')
            actual = text.count(old)
            assert actual == count, (relative, 'mutation anchor drift', count, actual)
            path.write_text(text.replace(old, new), encoding='utf-8')
            changed[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        result['changed_hashes'] = changed
        result['mutated_manifest'] = _manifest(build)

        environment = {
            **os.environ,
            'PYTHONPATH': str(build / 'src'),
            'STOCKCONDA_TEST_SOURCE': str(build / 'src'),
            'PYTHONUNBUFFERED': '1',
        }
        result['environment'] = {
            'PYTHONPATH': environment['PYTHONPATH'],
            'STOCKCONDA_TEST_SOURCE': environment['STOCKCONDA_TEST_SOURCE'],
        }
        commands = result['commands']
        assert isinstance(commands, dict)
        commands['build'] = _run_command(
            name='build',
            command=[sys.executable, 'setup.py', 'build_ext', '--inplace', '--force'],
            cwd=build,
            env=environment,
            deadline=180,
            output=output,
        )
        save()
        build_record = commands['build']
        assert isinstance(build_record, dict)
        assert build_record['status'] == 'COMPLETED', 'build command infrastructure failed'
        assert not build_record['timed_out'], 'build timed out; mutant was not evaluated'
        assert not build_record['cleanup']['initial'], (
            'build left tagged descendants which required forced cleanup')
        assert not build_record['cleanup']['remaining'], 'build descendants survived cleanup'
        assert build_record['exit_code'] == 0, 'build failed; mutant was not evaluated'
        result['built_manifest'] = _manifest(build, include_extensions=True)
        save()

        xml_path = output / 'pytest.xml'
        commands['pytest'] = _run_command(
            name='pytest',
            command=[
                sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                f'--junitxml={xml_path}', target,
            ],
            cwd=build,
            env=environment,
            deadline=90,
            output=output,
        )
        test_record = commands['pytest']
        assert isinstance(test_record, dict)
        save()
        assert test_record['status'] == 'COMPLETED', 'test command infrastructure failed'
        assert not test_record['timed_out'], 'test timed out; timeout is not a mutant kill'
        assert not test_record['cleanup']['initial'], (
            'test left tagged descendants which required forced cleanup')
        assert not test_record['cleanup']['remaining'], 'test descendants survived cleanup'
        log = (output / 'pytest.log').read_text(encoding='utf-8')
        if expected == 'assertion-rejected':
            assert test_record['exit_code'] == 1, log
            _assert_target_failure(xml_path, target, log)
            result['status'] = 'MUTANT_REJECTED_BY_ASSERTION'
        else:
            assert test_record['exit_code'] == 0, log
            root = ET.parse(xml_path).getroot()
            cases = root.findall('.//testcase')
            expected_name = target.rsplit('::', 1)[1]
            assert len(cases) == 1 and cases[0].get('name') == expected_name, (
                'JUnit did not identify the intended surviving target')
            assert not root.findall('.//failure') and not root.findall('.//error'), log
            result['status'] = 'SURVIVING_MUTANT_CONFIRMED'

        result['after_manifest'] = _manifest(build, include_extensions=True)
        assert result['built_manifest'] == result['after_manifest'], (
            'test changed the compiled source snapshot')
    except BaseException as exc:
        result['status_before_failure'] = result['status']
        result['status'] = 'INFRASTRUCTURE_OR_CONTROL_FAILURE'
        result['failure'] = repr(exc)
        if build.exists():
            result['after_manifest'] = _manifest(build, include_extensions=True)
        raise
    finally:
        save()


def run_mutant(
    source: Path,
    output: Path,
    edits: list[Edit],
    target: str,
) -> None:
    """Require one exact test node to reject a compiled mutant by assertion."""
    _run_control(source, output, edits, target, 'assertion-rejected')


def _run_case_worker(source: Path, output: Path, control: Control) -> None:
    _run_control(source, output, control.edits, control.target, control.expected)


def _outer_record(output: Path, update: dict[str, object]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    path = output / 'result.json'
    if path.exists():
        result = json.loads(path.read_text(encoding='utf-8'))
    else:
        result = {}
    result['outer'] = update
    if update.get('status') != 'COMPLETED':
        result['status_before_outer_failure'] = result.get('status')
        result['status'] = 'OUTER_INFRASTRUCTURE_FAILURE'
    _write_json(path, result)


def _run_case_outer(source: Path, output: Path, control: Control) -> None:
    token = uuid.uuid4().hex
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        '--source', str(source),
        '--output', str(output),
        '--case', control.name,
        '--worker',
    ]
    marker_variable = 'STOCKCONDA_CONTROL_OUTER_ID'
    environment = {**os.environ, marker_variable: token}
    started = time.monotonic()
    process: subprocess.Popen[str] | None = None
    timed_out = False
    launch_failure = None
    timeout_cleanup = None
    post_kill_wait_failure = None
    try:
        process = subprocess.Popen(command, env=environment, start_new_session=True)
        process.wait(timeout=300)
    except subprocess.TimeoutExpired:
        timed_out = True
        timeout_cleanup = _cleanup_tagged(token, marker_variable)
        if process is not None and process.poll() is None:
            process.kill()
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                post_kill_wait_failure = repr(exc)
    except BaseException as exc:
        launch_failure = repr(exc)
    finally:
        cleanup = _cleanup_tagged(token, marker_variable)
    status = ('OUTER_POST_KILL_WAIT_INFRASTRUCTURE_FAILURE'
              if post_kill_wait_failure
              else 'OUTER_TIMEOUT_INFRASTRUCTURE_FAILURE' if timed_out
              else 'OUTER_LAUNCH_INFRASTRUCTURE_FAILURE' if launch_failure
              else 'OUTER_DESCENDANT_CLEANUP_INFRASTRUCTURE_FAILURE'
              if cleanup['initial']
              else 'COMPLETED' if process is not None and process.returncode == 0
              else 'OUTER_CONTROL_FAILURE')
    record = {
        'command': command,
        'deadline_seconds': 300,
        'elapsed_seconds': time.monotonic() - started,
        'exit_code': None if process is None else process.returncode,
        'timed_out': timed_out,
        'launch_or_wait_failure': launch_failure,
        'post_kill_wait_failure': post_kill_wait_failure,
        'timeout_cleanup': timeout_cleanup,
        'cleanup': cleanup,
        'status': status,
    }
    _outer_record(output, record)
    assert status == 'COMPLETED', record


def _cleanup_self_test_scripts() -> tuple[str, str]:
    grandchild = """import os
import time
keys = ('STOCKCONDA_CONTROL_OUTER_ID', 'STOCKCONDA_CONTROL_COMMAND_ID')
print('grandchild', *(os.environ[key] for key in keys), flush=True)
time.sleep(60)
"""
    child = f"""import os
import subprocess
import sys
import time
keys = ('STOCKCONDA_CONTROL_OUTER_ID', 'STOCKCONDA_CONTROL_COMMAND_ID')
print('parent', *(os.environ[key] for key in keys), flush=True)
subprocess.Popen([sys.executable, '-c', {grandchild!r}])
time.sleep(60)
"""
    compile(grandchild, '<cleanup-grandchild>', 'exec')
    compile(child, '<cleanup-parent>', 'exec')
    return child, grandchild


def _self_test_cleanup(output: Path) -> None:
    """Exercise timeout cleanup with distinct tags inherited by a grandchild."""
    output = output.resolve(strict=False)
    output.mkdir(parents=True, exist_ok=False)
    outer_token = uuid.uuid4().hex
    child, _ = _cleanup_self_test_scripts()
    record = _run_command(
        name='cleanup-self-test',
        command=[sys.executable, '-c', child],
        cwd=Path.cwd(),
        env={**os.environ, 'STOCKCONDA_CONTROL_OUTER_ID': outer_token},
        deadline=1,
        output=output,
    )
    log = (output / 'cleanup-self-test.log').read_text(encoding='utf-8')
    command_token = str(record['run_id'])
    expected = f'{outer_token} {command_token}'
    assert record['status'] == 'TIMEOUT_INFRASTRUCTURE_FAILURE', record
    assert record['timed_out'], record
    timeout_cleanup = record.get('timeout_cleanup')
    assert isinstance(timeout_cleanup, dict), record
    assert len(timeout_cleanup['initial']) >= 2, record
    assert not timeout_cleanup['remaining'], record
    assert not record['cleanup']['remaining'], record
    assert f'parent {expected}' in log and f'grandchild {expected}' in log, log
    _write_json(output / 'self-test.json', {
        'status': 'PASS',
        'outer_token': outer_token,
        'command': record,
    })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', choices=[control.name for control in CONTROLS])
    parser.add_argument('--self-test-cleanup', action='store_true')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test_cleanup:
        assert args.case is None and not args.worker
        _self_test_cleanup(args.output)
        return
    selected = [control for control in CONTROLS
                if args.case is None or control.name == args.case]
    if args.worker:
        assert len(selected) == 1, 'internal worker requires exactly one case'
        _run_case_worker(args.source, args.output, selected[0])
        return
    args.output.resolve(strict=False).mkdir(parents=True, exist_ok=False)
    for control in selected:
        _run_case_outer(args.source, args.output / control.name, control)


if __name__ == '__main__':
    main()

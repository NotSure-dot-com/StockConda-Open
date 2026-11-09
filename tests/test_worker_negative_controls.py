"""Each mutant runs the real requirement test in a separate bounded process."""
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('target,old,new,test', [
    ('uci_search_worker', 'self._execute(task)',
     'child = Thread(target=self._execute, args=(task,)); child.start(); child.join()',
     'test_uci_search_worker.py::test_one_worker_for_100_tasks'),
    ('uci_search_worker', 'task.completed.wait()', 'task.completed.set()',
     'test_uci_search_worker.py::test_replacement_waits_for_publication_and_does_not_overlap'),
    ('uci_search_worker', 'cancelled: Event = field(default_factory=Event, compare=False)',
     'cancelled: Event = field(default=Event(), compare=False)',
     'test_uci_search_worker.py::test_task_payload_and_events_are_generation_local'),
    ('uci_search_worker', 'self._failure = exc', 'self._failure = None',
     'test_uci_search_worker.py::test_fatal_callback_failure_completes_and_closes_admission'),
    ('uci_search_worker', 'self._execute(task)',
     'task.cancelled.clear(); self._execute(task)',
     'test_uci_search_worker.py::test_cancellation_before_dequeue_is_not_reset'),
    ('stockconda', 'if value is None or self._cancel_requested():', 'if False:',
     'test_worker_engine_integration.py::test_cancelled_iteration_keeps_last_completed_move'),
    ('stockconda', 'if self._cancel_requested():', 'if self.stop_flag:',
     'test_worker_engine_integration.py::test_task_cancellation_reaches_recursive_time_check'),
    ('stockconda', 'best_move = next(iter(root.legal_moves), None)',
     'best_move = chess.Move.from_uci("e2e5")',
     'test_worker_engine_integration.py::test_fallback_and_next_search_after_error[illegal]'),
    ('stockconda', 'self._search_worker.shutdown()\n        if self.use_transposition_table',
     'free_cython_tt()\n        self._search_worker.shutdown()\n        if self.use_transposition_table',
     'test_worker_engine_integration.py::test_native_cleanup_follows_real_worker_exit'),
] + [
    ('stockconda',
     'def handle_' + method + signature + '\n        if not self._stop_active_search():',
     'def handle_' + method + signature + '\n        if False:',
     'test_worker_engine_integration.py::test_mutation_waits_for_search_completion[' + method + ']')
    for method, signature in [
        ('position', '(self, params: list[str]) -> None:'),
        ('ucinewgame', '(self) -> None:'),
        ('setoption', '(self, parts: list[str]) -> None:'),
    ]
])
def test_worker_mutant_is_rejected(target, old, new, test):
    script = '''
import importlib
import inspect
import pytest
module = importlib.import_module('chess_engine.' + TARGET)
source = inspect.getsource(module)
assert source.count(OLD) == 1, 'mutation anchor must be unique'
exec(compile(source.replace(OLD, NEW), module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/' + TEST]))
'''
    for name, value in [('TARGET', target), ('OLD', old), ('NEW', new), ('TEST', test)]:
        script = script.replace(name, repr(value))
    result = subprocess.run([sys.executable, '-u', '-c', script],
                            cwd=Path(__file__).resolve().parents[1],
                            env=os.environ.copy(), capture_output=True, text=True,
                            timeout=15)
    print(result.stdout, result.stderr)
    assert result.returncode == 1, 'mutant must fail by assertion, not crash or timeout'
    assert '1 failed' in result.stdout and 'AssertionError' in result.stdout

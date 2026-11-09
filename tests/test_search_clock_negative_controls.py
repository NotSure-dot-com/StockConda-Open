"""Every duration-clock site must reject a return to calendar time."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('site', range(6))
def test_calendar_clock_mutant_is_rejected(site):
    script = '''
import inspect
import re
import pytest
import chess_engine.stockconda as module
source = inspect.getsource(module)
old = 'time.monotonic()'
sites = list(re.finditer(re.escape(old), source))
assert len(sites) == 6, 'clock mutation sites must be reviewed if timing changes'
offset = sites[SITE].start()
source = source[:offset] + 'time.time()' + source[offset + len(old):]
exec(compile(source, module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/test_search_clock.py']))
'''.replace('SITE', str(site))
    result = subprocess.run([sys.executable, '-u', '-c', script],
                            cwd=Path(__file__).resolve().parents[1],
                            env=os.environ.copy(), capture_output=True, text=True,
                            timeout=15)
    print(result.stdout, result.stderr)
    assert result.returncode == 1, 'must fail by assertion, not crash or timeout'
    assert 'failed' in result.stdout and 'AssertionError' in result.stdout

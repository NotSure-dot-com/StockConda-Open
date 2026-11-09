"""Diagnostic runtime wrappers only; unchanged engine files and search logic."""
import json
from pathlib import Path
import sys
import time

from chess_engine.stockconda import UCIEngine


records = []
original_go = UCIEngine.handle_go
original_search = UCIEngine.execute_search_task
original_report = UCIEngine._run_search_and_report_bestmove
original_print = UCIEngine._uci_print


def stamp():
    return (time.perf_counter_ns(), time.thread_time_ns())


def handle_go(self, *args, **kwargs):
    record = {'go_enter': stamp()}
    self._diagnostic_record = record
    try:
        return original_go(self, *args, **kwargs)
    finally:
        record['go_return'] = stamp()


def execute_search(self, *args, **kwargs):
    record = self._diagnostic_record
    record['search_enter'] = stamp()
    try:
        return original_search(self, *args, **kwargs)
    finally:
        record['search_return'] = stamp()


def report(self, *args, **kwargs):
    record = self._diagnostic_record
    record['worker_enter'] = stamp()
    try:
        return original_report(self, *args, **kwargs)
    finally:
        record['worker_return'] = stamp()
        records.append(record)


def output(self, *args, **kwargs):
    value = args[0] if args else ''
    if isinstance(value, str) and value.startswith('bestmove '):
        record = self._diagnostic_record
        record['output_enter'] = stamp()
        try:
            return original_print(self, *args, **kwargs)
        finally:
            record['output_return'] = stamp()
    return original_print(self, *args, **kwargs)


UCIEngine.handle_go = handle_go
UCIEngine.execute_search_task = execute_search
UCIEngine._run_search_and_report_bestmove = report
UCIEngine._uci_print = output
engine = UCIEngine()
try:
    engine.run()
finally:
    Path(sys.argv[1]).write_text(json.dumps(records) + '\n')

"""Lifecycle requirements: real worker, event-controlled jobs, bounded waits."""
import importlib
import threading

import pytest


def controller(callback):
    try:
        module = importlib.import_module('chess_engine.uci_search_worker')
    except ModuleNotFoundError:
        pytest.fail('AP03 persistent worker is not implemented')
    return module.SearchWorker(callback)


def test_one_worker_for_100_tasks():
    threads, outputs = [], []
    def run(task):
        threads.append(threading.current_thread())
        outputs.append((task.task_id, task.fen))
    worker = controller(run)
    try:
        for n in range(100):
            task = worker.submit(str(n), None, 1, False)
            assert task.completed.wait(2)
        assert len(set(threads)) == 1
        assert outputs == [(n + 1, str(n)) for n in range(100)]
    finally:
        worker.shutdown()
    assert not threads[0].is_alive()


def test_replacement_waits_for_publication_and_does_not_overlap():
    started, release, replacing, second_started = (threading.Event() for _ in range(4))
    log, tasks, accepted_after_completion = [], [], []
    def run(task):
        tasks.append(task)
        log.append(('start', task.task_id))
        if task.task_id == 1:
            started.set()
            assert task.cancelled.wait(2)
            assert release.wait(2)
        else:
            second_started.set()
            assert not task.cancelled.is_set()
        log.append(('publish', task.task_id))
    worker = controller(run)
    first = worker.submit('first', None, 1, False)
    def replace():
        replacing.set()
        worker.submit('second', None, 1, False)
        accepted_after_completion.append(first.completed.is_set())
    thread = threading.Thread(target=replace)
    try:
        assert started.wait(2)
        thread.start()
        assert replacing.wait(2)
        assert first.cancelled.wait(2)
        assert not first.completed.is_set()
        assert not second_started.is_set()
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert accepted_after_completion == [True]
        assert second_started.wait(2)
        worker.cancel_and_wait()
        assert log == [('start', 1), ('publish', 1), ('start', 2), ('publish', 2)]
        assert tasks[0].cancelled is not tasks[1].cancelled
    finally:
        release.set()
        thread.join(2)
        worker.shutdown()


def test_shutdown_waits_for_job_and_rejects_more_work():
    started, release, finished = (threading.Event() for _ in range(3))
    def run(task):
        started.set()
        task.cancelled.wait(2)
        release.wait(2)
    worker = controller(run)
    task = worker.submit('root', None, 1, False)
    thread = threading.Thread(target=lambda: (worker.shutdown(), finished.set()))
    try:
        assert started.wait(2)
        thread.start()
        assert task.cancelled.wait(2)
        assert not finished.is_set()
        release.set()
        thread.join(2)
        assert finished.is_set()
        assert task.completed.is_set()
        worker.shutdown()
        with pytest.raises(RuntimeError):
            worker.submit('late', None, 1, False)
    finally:
        release.set()
        thread.join(2)
        worker.shutdown()


def test_fatal_callback_failure_completes_and_closes_admission():
    def run(task):
        raise OSError('output unavailable')
    worker = controller(run)
    try:
        task = worker.submit('root', None, 1, False)
        assert task.completed.wait(2)
        assert worker.failure is not None
        with pytest.raises(RuntimeError):
            worker.submit('later', None, 1, False)
    finally:
        worker.shutdown()


def test_idle_stop_and_shutdown_are_idempotent():
    worker = controller(lambda task: None)
    worker.cancel_and_wait()
    worker.shutdown()
    worker.shutdown()


def test_cancellation_before_dequeue_is_not_reset(monkeypatch):
    # Delay only the queue boundary; retain a real blocking queue and worker.
    from queue import Queue
    module = importlib.import_module('chess_engine.uci_search_worker')
    entered, release = threading.Event(), threading.Event()
    observed = []
    class GatedQueue(Queue):
        def get(self, *args, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().get(*args, **kwargs)
    monkeypatch.setattr(module, 'Queue', GatedQueue)
    worker = controller(lambda task: observed.append(task.cancelled.is_set()))
    stopper = threading.Thread(target=worker.cancel_and_wait)
    try:
        assert entered.wait(2)
        task = worker.submit('root', None, 1, False)
        stopper.start()
        assert task.cancelled.wait(2)
        assert not task.completed.is_set()
        release.set()
        stopper.join(3)
        assert not stopper.is_alive()
        assert task.completed.is_set()
        assert observed == [True]
    finally:
        release.set()
        if stopper.ident is not None:
            stopper.join(3)
        worker.shutdown()


def test_task_payload_and_events_are_generation_local():
    from dataclasses import FrozenInstanceError
    worker = controller(lambda task: None)
    try:
        first = worker.submit('first', 0.5, 4, False)
        assert first.completed.wait(2)
        second = worker.submit('second', None, None, True)
        assert second.completed.wait(2)
        with pytest.raises(FrozenInstanceError):
            first.fen = 'changed'
        assert (first.fen, first.time_limit, first.depth_limit, first.infinite) == ('first', 0.5, 4, False)
        assert first.cancelled is not second.cancelled
        assert first.completed is not second.completed
        first.cancelled.set()
        assert not second.cancelled.is_set()
    finally:
        worker.shutdown()

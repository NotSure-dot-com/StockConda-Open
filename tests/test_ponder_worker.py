"""Task-local ponder transition and wake-up races."""

import threading

from chess_engine.uci_search_worker import SearchWorker


def test_hit_releases_same_task_once() -> None:
    entered = threading.Event()
    observations = []

    def run(task):
        entered.set()
        task.wait_for_release()
        observations.append((task.task_id, task.hit_at, task.cancelled.is_set()))

    worker = SearchWorker(run)
    try:
        task = worker.submit("root", 0.1, 1, False, ponder=True)
        assert entered.wait(2)
        assert not task.completed.is_set()
        assert worker.ponderhit()
        hit_at = task.hit_at
        assert hit_at is not None
        assert not worker.ponderhit()
        assert task.completed.wait(2)
        assert observations == [(task.task_id, hit_at, False)]
        assert not worker.ponderhit()
    finally:
        worker.shutdown()


def test_stop_releases_finished_ponder_without_hit() -> None:
    entered = threading.Event()

    def run(task):
        entered.set()
        task.wait_for_release()

    worker = SearchWorker(run)
    try:
        task = worker.submit("root", None, 1, False, ponder=True)
        assert entered.wait(2)
        worker.cancel_and_wait()
        assert task.completed.is_set()
        assert task.cancelled.is_set()
        assert task.hit_at is None
    finally:
        worker.shutdown()


def test_hit_after_stop_is_not_reused_by_next_task() -> None:
    seen = []

    def run(task):
        task.wait_for_release()
        seen.append((task.task_id, task.hit_at))

    worker = SearchWorker(run)
    try:
        first = worker.submit("first", None, 1, False, ponder=True)
        worker.cancel_and_wait()
        assert not worker.ponderhit()
        second = worker.submit("second", None, 1, False, ponder=True)
        assert second.hit_at is None
        assert worker.ponderhit()
        assert second.completed.wait(2)
        assert seen == [(first.task_id, None), (second.task_id, second.hit_at)]
    finally:
        worker.shutdown()


def test_hit_before_worker_dequeue_is_preserved(monkeypatch) -> None:
    from queue import Queue
    import chess_engine.uci_search_worker as module

    entered = threading.Event()
    release = threading.Event()
    observed = []

    class GatedQueue(Queue):
        def get(self, *args, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().get(*args, **kwargs)

    monkeypatch.setattr(module, "Queue", GatedQueue)
    worker = module.SearchWorker(lambda task: observed.append(task.hit_at))
    try:
        assert entered.wait(2)
        task = worker.submit("root", 0.1, 1, False, ponder=True)
        assert worker.ponderhit()
        hit_at = task.hit_at
        assert hit_at is not None
        release.set()
        assert task.completed.wait(2)
        assert observed == [hit_at]
    finally:
        release.set()
        worker.shutdown()


def test_replacement_wakes_waiting_ponder() -> None:
    entered = threading.Event()
    seen = []

    def run(task):
        if task.ponder:
            entered.set()
        task.wait_for_release()
        seen.append((task.task_id, task.cancelled.is_set()))

    worker = SearchWorker(run)
    try:
        first = worker.submit("first", None, 1, False, ponder=True)
        assert entered.wait(2)
        second = worker.submit("second", None, 1, False)
        assert first.completed.is_set()
        assert second.completed.wait(2)
        assert seen == [(first.task_id, True), (second.task_id, False)]
    finally:
        worker.shutdown()


def test_hit_stop_race_has_one_terminal_callback() -> None:
    seen = []
    worker = SearchWorker(lambda task: (task.wait_for_release(), seen.append(task.task_id)))
    try:
        for n in range(20):
            task = worker.submit(str(n), None, 1, False, ponder=True)
            gate = threading.Barrier(3)
            hitter = threading.Thread(target=lambda: (gate.wait(), worker.ponderhit()))
            stopper = threading.Thread(target=lambda: (gate.wait(), worker.cancel_and_wait()))
            hitter.start()
            stopper.start()
            gate.wait()
            hitter.join(2)
            stopper.join(2)
            assert not hitter.is_alive() and not stopper.is_alive()
            assert task.completed.is_set()
        assert seen == list(range(1, 21))
    finally:
        worker.shutdown()

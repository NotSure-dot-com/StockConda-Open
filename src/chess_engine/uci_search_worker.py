"""One serialized UCI worker; completion includes result publication."""
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Queue
from threading import Event, Lock, Thread, current_thread


@dataclass(frozen=True)
class SearchTask:
    """Immutable starting FEN plus moves; events belong to this generation only."""

    task_id: int
    fen: str
    time_limit: float | None
    depth_limit: int | None
    infinite: bool
    moves: tuple[str, ...] = field(default=(), kw_only=True)
    cancelled: Event = field(default_factory=Event, compare=False)
    completed: Event = field(default_factory=Event, compare=False)


class SearchWorker:
    """Callback owns search and exactly-once output; callback failure is fatal.

    Public operations serialize through _commands. Worker state uses _state;
    callback execution, thread joins and Event waits never hold _state.
    Callbacks must not invoke public controller operations (self-wait).
    """

    def __init__(self, execute: Callable[[SearchTask], None]) -> None:
        self._execute = execute
        self._queue: Queue[SearchTask | None] = Queue(maxsize=1)
        self._commands = Lock()
        self._state = Lock()
        self._current: SearchTask | None = None
        self._closed = False
        self._failure: BaseException | None = None
        self._next_id = 0
        self._thread = Thread(target=self._run, name='stockconda-search', daemon=True)
        self._thread.start()

    @property
    def failure(self) -> BaseException | None:
        with self._state:
            return self._failure

    def _check_caller(self) -> None:
        if current_thread() is self._thread:
            raise RuntimeError('Worker callback cannot wait on itself')

    def _cancel(self) -> None:
        with self._state:
            task = self._current
            if task is not None:
                task.cancelled.set()
        if task is not None:
            task.completed.wait()

    def submit(self, fen: str, time_limit: float | None,
               depth_limit: int | None, infinite: bool, *,
               moves: tuple[str, ...] = ()) -> SearchTask:
        self._check_caller()
        with self._commands:
            self._cancel()
            with self._state:
                if self._closed:
                    raise RuntimeError('Search worker is closed') from self._failure
                self._next_id += 1
                task = SearchTask(self._next_id, fen, time_limit, depth_limit, infinite,
                                  moves=moves)
                self._current = task
            self._queue.put(task)
            return task

    def cancel_and_wait(self) -> None:
        self._check_caller()
        with self._commands:
            self._cancel()

    def shutdown(self) -> None:
        self._check_caller()
        with self._commands:
            with self._state:
                self._closed = True
            self._cancel()
            if self._thread.is_alive():
                self._queue.put(None)
                self._thread.join()

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                return
            try:
                self._execute(task)
            except BaseException as exc:
                with self._state:
                    self._failure = exc
                    self._closed = True
            finally:
                with self._state:
                    if self._current is task:
                        self._current = None
                    task.completed.set()
            if self.failure is not None:
                return

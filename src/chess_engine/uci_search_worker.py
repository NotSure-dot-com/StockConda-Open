"""One serialized UCI worker; completion includes result publication."""
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Queue
from threading import Event, Lock, Thread, current_thread
import time


class PonderControl:
    """One task's hit timestamp and publication barrier."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.release = Event()
        self.hit_at: float | None = None


@dataclass(frozen=True)
class SearchTask:
    """Immutable starting FEN plus moves; events belong to this generation only."""

    task_id: int
    fen: str
    time_limit: float | None
    depth_limit: int | None
    infinite: bool
    moves: tuple[str, ...] = field(default=(), kw_only=True)
    ponder: bool = field(default=False, kw_only=True)
    control: PonderControl = field(default_factory=PonderControl, compare=False)
    cancelled: Event = field(default_factory=Event, compare=False)
    completed: Event = field(default_factory=Event, compare=False)

    def __post_init__(self) -> None:
        if not self.ponder:
            self.control.release.set()

    @property
    def hit_at(self) -> float | None:
        with self.control.lock:
            return self.control.hit_at

    def mark_hit(self) -> bool:
        if not self.ponder:
            return False
        with self.control.lock:
            if (self.control.hit_at is not None or self.cancelled.is_set()
                    or self.completed.is_set()):
                return False
            self.control.hit_at = time.monotonic()
            self.control.release.set()
            return True

    def wait_for_release(self) -> None:
        self.control.release.wait()


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
                task.control.release.set()
        if task is not None:
            task.completed.wait()

    def submit(self, fen: str, time_limit: float | None,
               depth_limit: int | None, infinite: bool, *,
               moves: tuple[str, ...] = (), ponder: bool = False) -> SearchTask:
        self._check_caller()
        with self._commands:
            self._cancel()
            with self._state:
                if self._closed:
                    raise RuntimeError('Search worker is closed') from self._failure
                self._next_id += 1
                task = SearchTask(self._next_id, fen, time_limit, depth_limit, infinite,
                                  moves=moves, ponder=ponder)
                self._current = task
            self._queue.put(task)
            return task

    def ponderhit(self) -> bool:
        """Release the current ponder task without replacing its search."""
        self._check_caller()
        with self._commands:
            with self._state:
                task = self._current
            return task.mark_hit() if task is not None else False

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

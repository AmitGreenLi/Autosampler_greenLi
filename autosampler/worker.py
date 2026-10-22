from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable
from autosampler.errors import OperationAborted


@dataclass
class WorkerEvent:
    kind: str
    name: str
    value: Any = None


class MachineWorker:
    """One predictable worker owns normal blocking machine commands."""

    def __init__(self):
        self.commands: queue.Queue = queue.Queue()
        self.events: queue.Queue[WorkerEvent] = queue.Queue()
        self._stop = threading.Event()
        self._accepting = True
        self._queue_lock = threading.RLock()
        self._generation = 0
        self._pending = 0
        self._active = False
        self._thread = threading.Thread(target=self._run, name="autosampler-machine", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def submit(self, name: str, function: Callable, *args, **kwargs) -> None:
        with self._queue_lock:
            if not self._accepting:
                raise RuntimeError("Machine worker is shutting down and cannot accept commands.")
            self._pending += 1
            self.commands.put((self._generation, name, function, args, kwargs))

    @property
    def busy(self) -> bool:
        with self._queue_lock:
            return self._active or self._pending > 0

    def submit_if_idle(self, name: str, function: Callable, *args, **kwargs) -> bool:
        """Reserve the worker immediately, including the gap before command start."""
        with self._queue_lock:
            if self.busy:
                return False
            self.submit(name, function, *args, **kwargs)
            return True

    def cancel_pending(self, reason="Pending command cancelled by Abort.") -> None:
        with self._queue_lock:
            self._generation += 1
            while True:
                try:
                    item = self.commands.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    self._pending -= 1
                    self.events.put(WorkerEvent("cancelled", item[1], OperationAborted(reason)))

    def stop(self, cancel_pending: bool = True, timeout_s: float = 10.0) -> bool:
        self._accepting = False
        self._stop.set()
        if cancel_pending:
            self.cancel_pending("Application is shutting down.")
        self.commands.put(None)
        self._thread.join(timeout=timeout_s)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self.commands.get()
            if item is None:
                break
            generation, name, function, args, kwargs = item
            with self._queue_lock:
                self._pending -= 1
                if generation != self._generation:
                    self.events.put(WorkerEvent("cancelled", name, OperationAborted("Pending command cancelled.")))
                    continue
                self._active = True
            self.events.put(WorkerEvent("started", name))
            try:
                result = function(*args, **kwargs)
                self.events.put(WorkerEvent("completed", name, result))
            except Exception as exc:
                self.cancel_pending("Pending command cancelled after a machine command failed.")
                self.events.put(WorkerEvent("error", name, exc))
            finally:
                with self._queue_lock:
                    self._active = False

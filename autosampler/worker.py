from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable


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
        self._thread = threading.Thread(target=self._run, name="autosampler-machine", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def submit(self, name: str, function: Callable, *args, **kwargs) -> None:
        if not self._accepting:
            raise RuntimeError("Machine worker is shutting down and cannot accept commands.")
        self.commands.put((name, function, args, kwargs))

    def stop(self, cancel_pending: bool = True, timeout_s: float = 10.0) -> bool:
        self._accepting = False
        self._stop.set()
        if cancel_pending:
            while True:
                try:
                    self.commands.get_nowait()
                except queue.Empty:
                    break
        self.commands.put(None)
        self._thread.join(timeout=timeout_s)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self.commands.get()
            if item is None:
                break
            name, function, args, kwargs = item
            self.events.put(WorkerEvent("started", name))
            try:
                result = function(*args, **kwargs)
                self.events.put(WorkerEvent("completed", name, result))
            except Exception as exc:
                self.events.put(WorkerEvent("error", name, exc))

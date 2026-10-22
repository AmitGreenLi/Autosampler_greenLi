import time
import threading

from autosampler.errors import RecoveryRequired

from autosampler.worker import MachineWorker


def test_worker_reports_result_without_gui_thread_dependency():
    worker = MachineWorker()
    worker.start()
    worker.submit("add", lambda a, b: a + b, 2, 3)
    started = worker.events.get(timeout=1)
    completed = worker.events.get(timeout=1)
    worker.stop()
    assert started.kind == "started"
    assert completed.kind == "completed" and completed.value == 5


def test_idle_submission_reserves_worker_before_thread_starts():
    worker = MachineWorker()
    assert worker.submit_if_idle("home", lambda: None)
    assert not worker.submit_if_idle("unlock", lambda: None)
    assert worker.busy
    worker.start()
    worker.events.get(timeout=1)
    assert worker.events.get(timeout=1).kind == "completed"
    assert worker.stop()


def test_abort_cancels_queued_home_without_restarting_motion():
    worker = MachineWorker()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def active():
        entered.set()
        assert release.wait(2)
    worker.start()
    try:
        worker.submit("active", active)
        assert entered.wait(1)
        worker.submit("home", lambda: calls.append("home"))
        worker.submit("unlock", lambda: calls.append("unlock"))
        worker.cancel_pending()
        release.set()
    finally:
        release.set()
        assert worker.stop(cancel_pending=False)
    assert calls == []
    events = list(worker.events.queue)
    assert {(e.name, e.kind) for e in events if e.kind == "cancelled"} == {("home", "cancelled"), ("unlock", "cancelled")}


def test_failure_cancels_commands_queued_behind_it():
    worker = MachineWorker()
    calls = []
    def fail():
        raise RecoveryRequired("Missing homing ACK")
    worker.submit("home all", fail)
    worker.submit("home again", lambda: calls.append("home"))
    worker.submit("unlock", lambda: calls.append("unlock"))
    worker.start()
    events = [worker.events.get(timeout=1) for _ in range(4)]
    assert worker.stop()
    assert calls == []
    assert [(e.name, e.kind) for e in events] == [
        ("home all", "started"), ("home again", "cancelled"), ("unlock", "cancelled"), ("home all", "error")]

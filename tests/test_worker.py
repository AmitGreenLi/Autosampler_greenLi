import time

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

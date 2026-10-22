import pytest

from autosampler.config import SerialConfig
from autosampler.controllers.grbl import SerialGrblController
from autosampler.errors import SerialTimeout


class ScriptedSerial:
    def __init__(self, responses):
        self.is_open = True
        self.responses = iter(responses)
        self.writes = []
        self.input_resets = 0

    def reset_input_buffer(self):
        self.input_resets += 1

    def write(self, data):
        self.writes.append(data)

    def readline(self):
        return next(self.responses, b"")

    def close(self):
        self.is_open = False


def controller_with_responses(*responses):
    controller = SerialGrblController(SerialConfig("COM-test", 115200))
    controller._serial = ScriptedSerial(responses)
    return controller


def test_homing_waits_through_serial_read_intervals_for_final_ok():
    controller = controller_with_responses(b"", b"", b"ok\r\n")
    controller.home("X")
    assert controller._serial.writes == [b"$HX\n"]


def test_homing_deadline_is_enforced_even_with_continuous_status_lines(monkeypatch):
    from autosampler.errors import RecoveryRequired
    controller = controller_with_responses(b"<Home|MPos:0,0,0>\n", b"[MSG:busy]\n", b"ok\n")
    ticks = iter([0, 0, 1, 301])
    monkeypatch.setattr("autosampler.controllers.grbl.time.monotonic", lambda: next(ticks))
    with pytest.raises(RecoveryRequired, match="Timed out"):
        controller.home("A")
    assert controller.recovery_required
    assert controller._serial.writes[-1:] == [b"!"]
    assert b"\x18" not in controller._serial.writes


def test_ordinary_command_retains_short_serial_timeout():
    controller = controller_with_responses(b"")
    with pytest.raises(SerialTimeout):
        controller.unlock()


def test_read_settings_collects_multiline_dollar_report():
    controller = controller_with_responses(
        b"$112=500.000\r\n", b"$122=20.000\r\n",
        b"[MSG:settings]\r\n", b"ok\r\n",
    )
    assert controller.read_settings() == {112: 500.0, 122: 20.0}
    assert controller._serial.writes == [b"$$\n"]


def test_motion_wait_requires_idle_not_command_ok(monkeypatch, tmp_path):
    controller = controller_with_responses(b"ok\n", b"<Run|MPos:0,0,0>\n", b"<Idle|MPos:10,0,0>\n")
    controller._homing_recovery_file = tmp_path / "none.json"
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    controller.wait_for_motion_complete()
    assert controller._serial.writes == [b"?", b"?"]


def test_position_wait_rejects_early_idle_and_run_even_at_target(monkeypatch, tmp_path):
    controller = controller_with_responses(
        b"<Idle|WPos:747,0,-125,0,0>\n",
        b"<Run|WPos:853,0,-125,0,0>\n",
        b"<Idle|WPos:853,0,-125,0,0>\n")
    controller._homing_recovery_file = tmp_path / "none.json"
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    controller.wait_for_axis_position("X", 853)
    assert controller._serial.writes == [b"?", b"?", b"?"]
    assert controller._serial.input_resets == 3


def test_position_wait_converts_machine_position_with_work_offset(tmp_path):
    controller = controller_with_responses(b"<Idle|MPos:953,0,-100,0,0|WCO:100,0,25,0,0>\n")
    controller._homing_recovery_file = tmp_path / "none.json"
    controller.wait_for_axis_position("X", 853)


@pytest.mark.parametrize("report", [b"<Idle|WPos:840,0,-125,0,0>\n", b"<Idle|MPos:843,0,-125,0,0>\n"])
def test_position_wait_never_accepts_wrong_or_unknown_work_target(tmp_path, report):
    controller = controller_with_responses(report)
    controller._homing_recovery_file = tmp_path / "none.json"
    with pytest.raises(SerialTimeout, match="No Idle confirmation"):
        controller.wait_for_axis_position("X", 843, timeout_s=0)


def test_position_wait_stops_on_abort_after_status(tmp_path):
    from autosampler.errors import OperationAborted
    controller = controller_with_responses(b"<Idle|WPos:843,0,-125,0,0>\n")
    controller._homing_recovery_file = tmp_path / "none.json"
    checks = iter([False, True])
    with pytest.raises(OperationAborted):
        controller.wait_for_axis_position("X", 843, cancelled=lambda: next(checks))


@pytest.mark.parametrize("report", [b"<Hold:0|MPos:0,0,0>\n", b"<Door:0>\n", b"ALARM:1\n", b"Grbl restarted\n"])
def test_motion_wait_refuses_interrupted_controller_states(tmp_path, report):
    from autosampler.errors import RecoveryRequired

    controller = controller_with_responses(report)
    controller._homing_recovery_file = tmp_path / "none.json"
    with pytest.raises(RecoveryRequired):
        controller.wait_for_motion_complete()
    assert controller._serial.writes == [b"?"]


def test_motion_wait_is_bounded_and_checks_cancel_after_status(tmp_path):
    from autosampler.errors import OperationAborted

    controller = controller_with_responses(b"<Run>\n")
    controller._homing_recovery_file = tmp_path / "none.json"
    with pytest.raises(SerialTimeout):
        controller.wait_for_motion_complete(timeout_s=0)
    controller = controller_with_responses(b"<Idle>\n")
    controller._homing_recovery_file = tmp_path / "none.json"
    checks = iter([False, True])
    with pytest.raises(OperationAborted):
        controller.wait_for_motion_complete(cancelled=lambda: next(checks))


def test_fault_diagnostics_ignore_delayed_ok_without_reset_or_unlock():
    controller = controller_with_responses(b"ok\n", b"<Idle|MPos:1,0,2,3,4>\n")
    controller._needs_reconnect = True
    assert controller.status() == "<Idle|MPos:1,0,2,3,4>"
    assert controller._serial.writes == [b"?"]
    assert controller.connected and controller.recovery_required


def test_fault_hold_does_not_reset_or_close_port():
    controller = controller_with_responses()
    controller.hold_for_fault()
    assert controller._serial.writes == [b"!"]
    assert controller.connected and controller.recovery_required


def test_explicit_abort_still_resets_after_nonresetting_fault():
    controller = controller_with_responses()
    controller.hold_for_fault()
    controller.abort()
    assert controller._serial.writes == [b"!", b"!", b"\x18"]
    assert controller.recovery_required


@pytest.mark.parametrize("command", ["  G1 X2 F10  ", "$$", "$100=1"])
def test_manual_command_preserves_text_and_reports_all_lines(command):
    controller = controller_with_responses(b"$100=1\r\n", b"[MSG:ready]\r\n", b"ok\r\n")
    responses = []
    assert controller.send_command(command, responses.append) == "ok"
    assert controller._serial.writes == [(command + "\n").encode("ascii")]
    assert responses == ["$100=1", "[MSG:ready]", "ok"]


def test_manual_command_reports_normal_lines_and_raises_controller_error():
    from autosampler.errors import ControllerError

    controller = controller_with_responses(b"[MSG:invalid]\r\n", b"error:20\r\n")
    responses = []
    with pytest.raises(ControllerError, match="error:20"):
        controller.send_command("G1", responses.append)
    assert responses == ["[MSG:invalid]"]


@pytest.mark.parametrize("failure", [None, b"error:20\r\n", b"ALARM:1\r\n", b""])
def test_manual_serial_logging_has_one_owner(configured_machine, caplog, failure):
    import logging

    from autosampler.errors import ControllerError

    caplog.set_level(logging.INFO)
    caplog.clear()
    controller = controller_with_responses(
        b"$100=1\r\n", failure if failure is not None else b"ok\r\n"
    )
    configured_machine.grbl = controller
    if failure is None:
        configured_machine.send_manual_command("$$")
        final_message = "Manual GRBL < ok"
    else:
        with pytest.raises((ControllerError, SerialTimeout)) as error:
            configured_machine.send_manual_command("$$")
        final_message = f"Manual command: {type(error.value).__name__}: {error.value}"
    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["Manual GRBL > $$", "Manual GRBL < $100=1", final_message]


def test_abort_does_not_wait_for_serial_response_lock():
    import threading

    controller = controller_with_responses()
    started, release = threading.Event(), threading.Event()

    def read():
        started.set()
        assert release.wait(2)
        return b"ok\r\n"

    controller._serial.readline = read
    cancelled = []
    def command():
        from autosampler.errors import OperationAborted
        try:
            controller.send_command("G1")
        except OperationAborted as exc:
            cancelled.append(exc)
    command_thread = threading.Thread(target=command)
    command_thread.start()
    assert started.wait(2)
    abort_thread = threading.Thread(target=controller.abort)
    abort_thread.start()
    try:
        abort_thread.join(0.5)
        assert not abort_thread.is_alive()
        assert controller._serial.writes == [b"G1\n", b"!", b"\x18"]
    finally:
        release.set()
        command_thread.join(2)
        abort_thread.join(2)
    assert len(cancelled) == 1

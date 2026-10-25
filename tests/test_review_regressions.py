"""Verified software findings from the 81d2be8 external review (no hardware)."""
import threading
import time
from unittest.mock import Mock

import pytest

from autosampler.config import SerialConfig
from autosampler.controllers.grbl import SerialGrblController
from autosampler.errors import (ConnectionFailure, ControllerAlarm, CorrectionFailure,
                                RecoveryRequired, SerialTimeout, ValidationError)
from autosampler.models import OperationState, PipetteId, ScaleReading
from test_grbl_controller import ScriptedSerial


def test_timeout_latches_stream_before_late_ack(tmp_path):
    controller = SerialGrblController(SerialConfig("fake", 115200), homing_recovery_file=tmp_path / "recovery")
    controller._serial = ScriptedSerial([b"", b"ok\n", b"ok\n"])
    with pytest.raises(SerialTimeout):
        controller.move_absolute("A", 1)
    with pytest.raises(RecoveryRequired):
        controller.move_absolute("A", 2)
    assert controller._serial.writes == [b"G90 G0 A1\n"]
    assert controller.recovery_required


@pytest.mark.parametrize("report,error", [(b"ALARM:1\n", ControllerAlarm), (b"Grbl restart\n", RecoveryRequired)])
def test_status_rejects_push_messages(tmp_path, report, error):
    controller = SerialGrblController(SerialConfig("fake", 115200), homing_recovery_file=tmp_path / "recovery")
    controller._serial = ScriptedSerial([report])
    with pytest.raises(error):
        controller.status()
    assert controller.recovery_required


def test_liquid_reads_and_saves_wait_for_idle(configured_machine, monkeypatch):
    m = configured_machine
    pending = {"polls": 0}
    move = m.grbl.move_absolute
    status = m.grbl.status
    def moving(*args, **kwargs):
        result = move(*args, **kwargs)
        pending["polls"] = 2
        return result
    def reporting():
        report = status()
        if pending["polls"]:
            pending["polls"] -= 1
            return report.replace("<Idle", "<Run")
        return report
    read, save = m.scale.read_stable, m.store.save
    def stable(*args, **kwargs):
        assert pending["polls"] == 0
        return read(*args, **kwargs)
    def persist(inventory):
        assert pending["polls"] == 0
        return save(inventory)
    monkeypatch.setattr(m.grbl, "move_absolute", moving)
    monkeypatch.setattr(m.grbl, "status", reporting)
    monkeypatch.setattr(m.scale, "read_stable", stable)
    monkeypatch.setattr(m.store, "save", persist)
    monkeypatch.setattr("time.sleep", lambda _: None)
    m.scale.queue(ScaleReading(10, True), ScaleReading(9.9, True))
    m.transfer("source", "dest", 100)
    assert m.inventory.get("dest").quantity_ul == 100


@pytest.mark.parametrize("error", [SerialTimeout("missed acknowledgement"), ControllerAlarm("ALARM:1"), ConnectionFailure("scale lost")])
def test_liquid_failure_withholds_trust(configured_machine, monkeypatch, error):
    m = configured_machine
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(m.scale if isinstance(error, ConnectionFailure) else m.grbl,
                        "read_stable" if isinstance(error, ConnectionFailure) else "move_absolute", fail)
    with pytest.raises(type(error)):
        m.draw("source", 100, PipetteId.P1)
    assert not m.state.position_trusted
    assert m.state.operation_state is OperationState.RECOVERY_REQUIRED
    if isinstance(error, ConnectionFailure):
        assert m.grbl.commands[-1] == "!"
        assert "CTRL-X" not in m.grbl.commands


def test_failed_draw_persists_uncertainty_until_dump_and_confirmation(configured_machine):
    m = configured_machine
    m.scale.queue(ScaleReading(10, True), *[ScaleReading(9.95, True)] * 3)
    with pytest.raises(CorrectionFailure):
        m.draw("source", 100, PipetteId.P1)
    saved = m.store.load()
    assert saved.unverified_sources == {"source"}
    assert saved.dump_required == {"P1"}
    with pytest.raises(ValidationError, match="Dump"):
        m.draw("clean", 100, PipetteId.P1)
    with pytest.raises(ValidationError, match="Dump"):
        m.dispense("dest", 1, PipetteId.P1)
    m.dump(PipetteId.P1)
    assert not m.store.load().dump_required
    with pytest.raises(ValidationError, match="Source quantity is unverified"):
        m.draw("source", 100, PipetteId.P1)


@pytest.mark.parametrize("action", ["home", "jog"])
@pytest.mark.parametrize("z", [None, 10])
def test_x_requires_z_clearance(configured_machine, action, z):
    m = configured_machine
    m.state.positions["Z"] = z
    if z is None:
        m.state.homed_axes.discard("Z")
    before = list(m.grbl.commands)
    with pytest.raises(RecoveryRequired, match="clearance"):
        m.home("X") if action == "home" else m.move_axis("X", 1, "steps")
    assert m.grbl.commands == before


def test_poll_failure_clears_old_limit_report(configured_machine, monkeypatch):
    m = configured_machine
    m.refresh_grbl_status()
    monkeypatch.setattr(m.grbl, "status", Mock(side_effect=SerialTimeout("poll timeout")))
    with pytest.raises(SerialTimeout):
        m.refresh_grbl_status()
    assert m.state.grbl_status_report is None
    assert m.state.grbl_status_updated_at is None


@pytest.mark.parametrize("amount,pipette", [(200, PipetteId.P1), (10000, PipetteId.P2)])
def test_buffered_capacity_plan_requires_confirmed_extra_cycle(configured_machine, amount, pipette):
    m = configured_machine
    plan = m.plan_transfer(amount, buffer_percent=5)
    assert plan.pipette is pipette
    assert len(plan.cycles_ul) == 2 and plan.requires_confirmation
    assert sum(plan.cycles_ul) == pytest.approx(amount)
    for cycle in plan.cycles_ul:
        m._travel_for_volume(cycle * 1.05, pipette)


def test_buffered_full_capacity_transfer_runs(configured_machine):
    m = configured_machine
    plan = m.plan_transfer(200, buffer_percent=5)
    readings = []
    for cycle in plan.cycles_ul:
        readings.extend([ScaleReading(10, True), ScaleReading(10 - cycle * 1.05 / 1000, True)])
    m.scale.queue(*readings)
    m.transfer("source", "dest", 200, confirm_multicycle=True, buffer_percent=5)
    assert m.inventory.get("dest").quantity_ul == pytest.approx(200, abs=.02)


def test_draw_bottom_clearance_rejected_before_motion(configured_machine):
    m = configured_machine
    m.inventory.get("source").quantity_ul = 100
    before = list(m.grbl.commands)
    with pytest.raises(ValidationError, match="clearance"):
        m.draw("source", 100, PipetteId.P1)
    assert m.grbl.commands == before


def test_inventory_changes_refuse_active_operation(configured_machine):
    m = configured_machine
    m.state.current_operation = "draw"
    with pytest.raises(ValidationError):
        m.apply_inventory(m.inventory)
    with pytest.raises(ValidationError):
        m.reset_session(True)


def test_poll_failure_does_not_cancel_queued_user_command():
    from autosampler.worker import MachineWorker
    worker = MachineWorker()
    called = threading.Event()
    worker.submit("poll", Mock(side_effect=SerialTimeout("poll")), cancel_on_error=False)
    worker.submit("user command", called.set)
    worker.start()
    try:
        assert called.wait(2)
    finally:
        worker.stop()


def test_mock_and_hardware_state_are_separate(tmp_path, monkeypatch):
    from autosampler.app_paths import ApplicationPaths
    from autosampler.main import build_runtime
    monkeypatch.setattr("autosampler.main.discover_serial_devices", lambda: [])
    paths = ApplicationPaths.under(tmp_path)
    mock = build_runtime(True, paths)[0]
    hardware = build_runtime(False, paths)[0]
    assert mock.config.state_file != hardware.config.state_file


def test_stable_read_returns_qualifying_sample(monkeypatch):
    from autosampler.config import AandFzScaleConfig
    from autosampler.controllers.scale import SerialScaleController
    scale = SerialScaleController(AandFzScaleConfig("fake", 9600))
    reading = ScaleReading(1, True)
    def measure(hold, timeout, on_reading):
        on_reading(reading)
        scale._latest = ScaleReading(2, False)
    monkeypatch.setattr(scale, "measure_stabilization", measure)
    assert scale.read_stable(0) is reading


def test_connected_scale_is_not_opened_twice(monkeypatch):
    from autosampler.config import AandFzScaleConfig
    from autosampler.controllers.scale import SerialScaleController
    monkeypatch.setattr(SerialScaleController, "connected", property(lambda _: True))
    opening = Mock(side_effect=AssertionError("already connected"))
    monkeypatch.setattr("autosampler.controllers.scale.open_serial_port", opening)
    SerialScaleController(AandFzScaleConfig("fake", 9600)).connect()
    opening.assert_not_called()


def test_calibration_save_fsyncs(configured_machine, tmp_path, monkeypatch):
    from autosampler.calibration import save_calibration
    sync = Mock()
    monkeypatch.setattr("autosampler.calibration.os.fsync", sync)
    save_calibration(configured_machine.config, tmp_path / "calibration.json", [])
    sync.assert_called_once()


def test_homing_origin_mismatch_withholds_trust(configured_machine, monkeypatch):
    m = configured_machine
    monkeypatch.setattr(m.grbl, "status", lambda: "<Idle|WPos:0,0,-5,0,0>")
    with pytest.raises(RecoveryRequired, match="HOMING_ORIGIN_MISMATCH"):
        m.home()
    assert not m.state.position_trusted
    assert m.grbl.commands[-1] == "$HZ"


def test_wait_reuses_last_work_offset(tmp_path):
    controller = SerialGrblController(SerialConfig("fake", 115200), homing_recovery_file=tmp_path / "recovery")
    controller._serial = ScriptedSerial([b"<Idle|MPos:10,0,0,0,0|WCO:10,0,0,0,0>\n", b"<Idle|MPos:20,0,0,0,0>\n"])
    controller.status()
    controller.wait_for_axis_position("X", 10, timeout_s=0)


@pytest.mark.parametrize("state", ["Hold:0", "Run", "Door:0", "Jog"])
def test_manual_cycle_start_cannot_resume_interrupted_motion(configured_machine, monkeypatch, state):
    m = configured_machine
    monkeypatch.setattr(m.grbl, "status", lambda: f"<{state}|WPos:0,0,0,0,0>")
    before = list(m.grbl.commands)
    with pytest.raises(RecoveryRequired, match="Cycle start"):
        m.send_manual_command("~")
    assert m.grbl.commands == before


@pytest.mark.parametrize("startup", [b"Grbl 1.1h\n", b""])
def test_connect_verifies_build_without_requiring_reconnect_banner(tmp_path, monkeypatch, startup):
    serial = ScriptedSerial([startup, b"[VER:1.1h:test]\n", b"ok\n", b"<Idle|WPos:0,0,0,0,0>\n"])
    monkeypatch.setattr("autosampler.controllers.grbl.open_serial_port", lambda *a, **k: serial)
    controller = SerialGrblController(SerialConfig("fake", 115200), homing_recovery_file=tmp_path / "recovery")
    controller.connect()
    assert controller.build_info == "[VER:1.1h:test]"
    assert serial.writes == [b"$I\n", b"?"]
    assert not controller.recovery_required


def test_connect_rejects_wrong_device_before_reset_or_settings(tmp_path, monkeypatch):
    import json
    path = tmp_path / "recovery"
    path.write_text(json.dumps({"port": "fake", "settings": {25: 100}}))
    serial = ScriptedSerial([b"", b"ST,+1.00 g\n", b""])
    monkeypatch.setattr("autosampler.controllers.grbl.open_serial_port", lambda *a, **k: serial)
    controller = SerialGrblController(SerialConfig("fake", 115200), homing_recovery_file=path)
    with pytest.raises(SerialTimeout):
        controller.connect()
    assert serial.writes == [b"$I\n"]
    assert controller.recovery_required and path.exists()


@pytest.mark.parametrize("state", ["Hold:0", "Run", "Door:0", "Jog"])
def test_connect_retains_recovery_for_unfinished_motion(tmp_path, monkeypatch, state):
    serial = ScriptedSerial([b"", b"[VER:1.1h:test]\n", b"ok\n", f"<{state}>\n".encode()])
    monkeypatch.setattr("autosampler.controllers.grbl.open_serial_port", lambda *a, **k: serial)
    controller = SerialGrblController(SerialConfig("fake", 115200), homing_recovery_file=tmp_path / "recovery")
    with pytest.raises(RecoveryRequired, match="unfinished motion"):
        controller.connect()
    assert controller.recovery_required and controller.connected
    assert b"\x18" not in serial.writes


def test_remote_abort_can_write_between_read_slices(monkeypatch, tmp_path):
    import base64
    import json
    from types import SimpleNamespace
    from autosampler.remote_serial import RemoteSerialPort
    reading = threading.Event()
    requests = []
    process = SimpleNamespace(request=None, timeout=None)
    def write(raw):
        process.request = json.loads(raw)
        requests.append(process.request)
    def readline():
        request = process.request
        if request["op"] == "open":
            process.timeout = request["timeout"]
            reply = {}
        elif request["op"] == "readline":
            reading.set()
            time.sleep(process.timeout)
            reply = {"data": ""}
        else:
            reply = {"count": len(base64.b64decode(request.get("data", "")))}
        return json.dumps({"ok": True, **reply}).encode() + b"\n"
    process.stdin = SimpleNamespace(write=write, flush=lambda: None)
    process.stdout = SimpleNamespace(readline=readline)
    process.poll = lambda: None
    monkeypatch.setattr("autosampler.remote_serial.subprocess.Popen", lambda *a, **k: process)
    port = RemoteSerialPort("ssh://user@host/dev/ttyUSB0", 115200, timeout=.3)
    controller = SerialGrblController(SerialConfig("fake", 115200), homing_recovery_file=tmp_path / "recovery")
    controller._serial = port
    thread = threading.Thread(target=port.readline)
    thread.start()
    try:
        assert reading.wait(1)
        start = time.monotonic()
        controller.abort()
        assert time.monotonic() - start < .2
    finally:
        thread.join(2)
    assert not thread.is_alive()
    assert process.timeout <= .05
    assert [base64.b64decode(r["data"]) for r in requests if r["op"] == "write"] == [b"!", b"\x18"]


def test_recovery_build_mismatch_rejected_before_reset(tmp_path, monkeypatch):
    import json
    path = tmp_path / "recovery"
    path.write_text(json.dumps({"port": "fake", "settings": {25: 100}, "build_info": "[VER:other]"}))
    serial = ScriptedSerial([b"", b"[VER:1.1h:test]\n", b"ok\n"])
    monkeypatch.setattr("autosampler.controllers.grbl.open_serial_port", lambda *a, **k: serial)
    controller = SerialGrblController(SerialConfig("fake", 115200), homing_recovery_file=path)
    with pytest.raises(RecoveryRequired, match="different GRBL build"):
        controller.connect()
    assert serial.writes == [b"$I\n"]


def test_unsupported_position_format_fails_without_full_motion_timeout(tmp_path, monkeypatch):
    controller = SerialGrblController(SerialConfig("fake", 115200), homing_recovery_file=tmp_path / "recovery")
    controller._serial = ScriptedSerial([b"<Idle|MPos:0,0,0,0,0,0>\n"] * 5)
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(RecoveryRequired, match="No usable A work coordinate"):
        controller.wait_for_axis_position("A", 0)
    assert controller._serial.writes == [b"?"] * 5


def test_minimum_correction_cannot_exceed_tolerance(configured_machine):
    from autosampler.calibration import geometry_errors
    m = configured_machine
    m.config.transfer.minimum_correction_volume_ul = 2
    assert "Minimum correction volume must not exceed tolerance" in geometry_errors(m.config)


def test_confirmed_vessel_edit_cannot_clear_required_dump(configured_machine):
    from autosampler.vessels.inventory import Inventory
    m = configured_machine
    m.inventory.dump_required.add("P1")
    m.inventory.unverified_sources.add("source")
    m.apply_inventory(Inventory.from_list(m.inventory.to_list()))
    assert m.store.load().dump_required == {"P1"}
    assert not m.inventory.unverified_sources


def test_posix_serial_open_requests_exclusive_access(monkeypatch):
    import sys
    from types import SimpleNamespace
    from autosampler.remote_serial import open_serial_port
    opening = Mock()
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=opening))
    monkeypatch.setattr("autosampler.remote_serial.os", SimpleNamespace(name="posix"))
    open_serial_port("fake", baudrate=115200)
    opening.assert_called_once_with(port="fake", baudrate=115200, exclusive=True)


def test_fault_cancellation_has_visible_error_type():
    from autosampler.errors import CommandCancelled
    from autosampler.worker import MachineWorker
    worker = MachineWorker()
    worker.submit("failure", Mock(side_effect=RecoveryRequired("fault")))
    worker.submit("user command", Mock())
    worker.start()
    try:
        events = [worker.events.get(timeout=2) for _ in range(3)]
        cancellation = next(e for e in events if e.kind == "cancelled")
        assert isinstance(cancellation.value, CommandCancelled)
    finally:
        worker.stop()


def test_post_draw_depth_keeps_initial_vessel_height_guard(configured_machine):
    m = configured_machine
    m.config.aspiration.vial_internal_height_mm = 74
    before = list(m.grbl.commands)
    with pytest.raises(ValidationError, match="Calculated liquid level exceeds"):
        m.draw("source", 100, PipetteId.P1)
    assert m.grbl.commands == before

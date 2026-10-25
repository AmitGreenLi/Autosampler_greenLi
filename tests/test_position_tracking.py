import pytest

from autosampler.errors import ValidationError, ConnectionFailure, RecoveryRequired
from autosampler.position_tracking import PositionTracking


def test_mega_axis_order_and_coordinate_conversion():
    p = PositionTracking()
    p.ingest("<Idle|MPos:100,999,30,40,50|WCO:10,900,3,4,5>")
    assert p.machine == dict(X=100, Y=999, Z=30, A=40, B=50)
    assert p.work == dict(X=90, Y=99, Z=27, A=36, B=45)
    p.set_reference("machine", ["Z"], True)
    p.set_reference("work", ["Z"], True)
    p.ingest("<Run|WPos:91,99,25,36,45>")
    assert p.machine["Z"] == 28
    assert p.displacement("machine", "Z") == -2
    assert p.displacement("work", "Z") == -2
    p.ingest("<Idle|MPos:101,999,28,40,50|WCO:10,900,8,4,5>")
    assert p.displacement("work", "Z") is None
    assert p.displacement("machine", "Z") == -2


@pytest.mark.parametrize("report", ["<Idle|MPos:1,2>", "<Idle|MPos:nan,2,3>", "<Idle|MPos:1,inf,3>", "<Idle|MPos:foo>"])
def test_bad_reports_never_produce_positions(report):
    p = PositionTracking()
    p.ingest(report)
    assert p.machine == {}
    assert p.availability(True) == "Unknown"


def test_partial_report_never_fills_missing_axes_or_assumes_work_zero():
    p = PositionTracking()
    p.ingest("<Idle|MPos:1,2,3>")
    assert p.machine["Z"] == 3
    assert "A" not in p.machine
    assert p.work == {}
    with pytest.raises(ValidationError):
        p.set_reference("machine", ["X", "A"], True)
    assert p.references == {}
    p.set_reference("machine", ["X"], True)
    assert p.availability(True, p.updated_at + 3) == "Stale"
    p.updated_at -= 3
    with pytest.raises(ValidationError):
        p.set_reference("machine", ["X"], True)
    assert p.availability(False) == "Disconnected"


def test_jogs_repeated_references_and_no_controller_writes(configured_machine):
    m = configured_machine
    m.refresh_grbl_status()
    before = list(m.grbl.commands)
    m.set_measurement_reference("machine")
    assert m.grbl.commands == before
    m.move_axis("Z", 3, "mm")
    assert m.state.reported_position.displacement("machine", "Z") == 0
    m.refresh_grbl_status()
    assert m.state.reported_position.displacement("machine", "Z") == 3
    m.set_measurement_reference("machine", "Z")
    m.move_axis("Z", -2, "mm")
    m.refresh_grbl_status()
    assert m.state.reported_position.displacement("machine", "Z") == -2
    m.set_measurement_reference("machine")
    assert all(v == 0 for v in (m.state.reported_position.displacement("machine", a) for a in "XZAB"))


def test_manual_motion_uses_feedback_and_preserves_reference(configured_machine, monkeypatch):
    m = configured_machine
    m.refresh_grbl_status()
    m.set_measurement_reference("machine", "A")
    m.set_measurement_reference("work", "A")
    def raw(command, on_response):
        m.grbl.positions["A"] = 8.5
        on_response("ok")
        return "ok"
    monkeypatch.setattr(m.grbl, "send_command", raw)
    m.send_manual_command("G91 G1 A8.5")
    m.refresh_grbl_status()
    assert m.state.reported_position.displacement("machine", "A") == 8.5
    assert m.state.reported_position.displacement("work", "A") == 8.5
    assert not m.state.position_trusted  # existing raw-command safeguard retained
    m.send_manual_command("G10 L20 P1 A0")
    assert m.state.reported_position.displacement("work", "A") is None


@pytest.mark.parametrize("event", ["home", "axis_home", "abort", "reconnect", "lost", "banner", "raw_home"])
def test_references_invalidated(configured_machine, event):
    m = configured_machine
    m.refresh_grbl_status()
    m.set_measurement_reference("machine")
    if event == "home":
        m.home()
    elif event == "axis_home":
        m.home("Z")
    elif event == "abort":
        m.abort()
    elif event == "reconnect":
        m.disconnect_grbl()
        m.connect_grbl()
    elif event == "lost":
        m.notify_grbl_disconnected()
    elif event == "banner":
        m._observe_report("Grbl 1.1h ['$' for help]")
    else:
        m.send_manual_command("$HZ")
    assert m.state.reported_position.references == {}
    assert m.state.reported_position.machine == {}


def test_unhomed_measurement_allowed_without_motion_trust(configured_machine):
    m = configured_machine
    m.unlock_for_calibration()
    m.refresh_grbl_status()
    m.set_measurement_reference("machine", "Z")
    m.move_axis("Z", 1, "mm")
    m.refresh_grbl_status()
    comparison = m.record_travel_comparison("machine", "Z", 0.9, True)
    assert comparison["reported_displacement"] == 1
    assert comparison["physical_distance"] == 0.9
    assert not m.state.homed and not m.state.position_trusted
    with pytest.raises(ValidationError):
        m.record_travel_comparison("machine", "Z", float("nan"), True)
    with pytest.raises(ValidationError):
        m.record_travel_comparison("machine", "Z", 1, False)


def test_mock_offset_change_is_not_machine_motion(configured_machine):
    m = configured_machine
    m.move_axis("X", 10, "steps")
    m.refresh_grbl_status()
    m.set_measurement_reference("machine", "X")
    m.record_carousel_reference()
    m.refresh_grbl_status()
    assert m.state.reported_position.displacement("machine", "X") == 0
    assert m.state.reported_position.work["X"] == 0


def test_settings_in_inches_make_tracking_unavailable(configured_machine):
    m = configured_machine
    m.send_manual_command("$13=1")
    m.refresh_grbl_status()
    assert m.state.reported_position.availability(True) == "Unknown ($13=0 required)"
    with pytest.raises(ValidationError):
        m.set_measurement_reference("machine")


def test_reset_during_command_invalidates_position(configured_machine):
    from autosampler.config import SerialConfig
    from autosampler.controllers.grbl import SerialGrblController
    from test_grbl_controller import ScriptedSerial
    m = configured_machine
    m.refresh_grbl_status()
    m.set_measurement_reference("machine")
    serial = SerialGrblController(SerialConfig("test", 115200))
    serial._serial = ScriptedSerial([b"Grbl 1.1h\n"])
    m.grbl = serial
    with pytest.raises(RecoveryRequired):
        m.send_manual_command("G1 Z3")
    assert not m.state.reported_position.references
    assert not m.state.homed


def test_poll_connection_loss_clears_references(configured_machine, monkeypatch):
    m = configured_machine
    m.refresh_grbl_status()
    m.set_measurement_reference("machine")
    def fail():
        raise ConnectionFailure("removed")
    monkeypatch.setattr(m.grbl, "status", fail)
    with pytest.raises(ConnectionFailure):
        m.refresh_grbl_status()
    assert not m.state.grbl_connected
    assert not m.state.reported_position.references


def test_reference_capture_rejects_running_controller(configured_machine, monkeypatch):
    m = configured_machine
    monkeypatch.setattr(m.grbl, "status", lambda: "<Run|MPos:1,0,2,3,4>")
    m.refresh_grbl_status()
    with pytest.raises(ValidationError, match="Idle"):
        m.set_measurement_reference("machine")


def test_carousel_comparison_converts_reported_steps_to_degrees(configured_machine):
    m = configured_machine
    m.refresh_grbl_status()
    m.set_measurement_reference("machine", "X")
    m.move_axis("X", 140, "steps")
    m.refresh_grbl_status()
    record = m.record_travel_comparison("machine", "X", 26.5, True)
    assert record["reported_displacement"] == pytest.approx(27)
    assert record["unit"] == "degrees"


def test_reset_report_invalidates_every_axis_and_fresh_status_never_retrusts(configured_machine):
    from autosampler.config import SerialConfig
    from autosampler.controllers.grbl import SerialGrblController
    from test_grbl_controller import ScriptedSerial
    m = configured_machine
    m.refresh_grbl_status()
    m.set_measurement_reference("machine")
    serial = SerialGrblController(SerialConfig("test", 115200))
    serial._serial = ScriptedSerial([b"Grbl 1.1h\n", b"<Idle|MPos:0,0,0,0,0>\n"])
    m.grbl = serial
    from autosampler.errors import RecoveryRequired
    with pytest.raises(RecoveryRequired):
        m.refresh_grbl_status()
    assert not m.state.homed_axes and not m.state.homed and not m.state.position_trusted
    assert not m.state.reported_position.references
    assert serial.recovery_required
    m.refresh_grbl_status()
    assert m.state.reported_position.machine["A"] == 0
    assert not m.state.homed_axes and not m.state.position_trusted
    assert not m.state.reported_position.references


def test_scale_loss_requests_hold_without_automatic_reset(configured_machine):
    m = configured_machine
    m.state.current_operation = "draw"
    m.grbl.commands.clear()
    m.notify_scale_disconnected()
    assert m.grbl.commands == ["!"]
    assert m.grbl.connected
    assert not m.state.position_trusted and not m.state.homed_axes
    assert m.state.abort_requested

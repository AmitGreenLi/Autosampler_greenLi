from collections import deque
import json

import pytest

from autosampler.config import CAROUSEL_STEPS_PER_DEGREE, SerialConfig
from autosampler.controllers.grbl import SerialGrblController
from autosampler.errors import ControllerError, OperationAborted, RecoveryRequired


SEARCH_RATE = 30 * CAROUSEL_STEPS_PER_DEGREE * 60


class HomingSerial:
    """Model EEPROM changes independently of whether their ACK reaches the sender."""

    def __init__(self, settings=None):
        self.settings = settings if settings is not None else {
            22: 1, 23: 0, 24: 25, 25: 100, 27: 5, 110: 2000, 130: 2000,
            100: 32, 101: 400, 102: 160, 103: 1600, 104: 1600,
            111: 2000, 112: 1000, 113: 500, 114: 500,
        }
        self.is_open = True
        self.responses = deque()
        self.writes = []
        self.home_response = b"ok\n"
        self.after_home = None
        self.pending_home = False
        self.drop_ack = None
        self.ignore_restore = False

    def write(self, data):
        self.writes.append(data)
        command = data.decode().strip()
        if data in (b"!", b"\x18"):
            return
        if command == "$$":
            self.responses.extend(f"${k}={v:.3f}\n".encode() for k, v in self.settings.items())
        elif command.startswith("$") and "=" in command:
            key, value = command[1:].split("=")
            if not (self.ignore_restore and float(value) == 100):
                self.settings[int(key)] = float(value)
            if self.drop_ack == int(key):
                raise OSError("Connection lost after EEPROM write")
        elif command.startswith("$H"):
            self.pending_home = True
            self.responses.append(self.home_response)
            return
        self.responses.append(b"ok\n")

    def readline(self):
        if self.pending_home:
            self.pending_home = False
            if self.after_home:
                self.after_home()
        return self.responses.popleft() if self.responses else b""

    def reset_input_buffer(self):
        self.responses.clear()

    def close(self):
        self.is_open = False


@pytest.fixture
def controller(tmp_path):
    controller = SerialGrblController(
        SerialConfig("test-controller", 115200),
        homing_recovery_file=tmp_path / "homing-recovery.json",
    )
    controller._serial = HomingSerial()
    return controller


def test_x_search_speed_and_maximum_are_restored_without_changing_locating(controller):
    serial = controller._serial
    original = serial.settings.copy()
    controller.home("X", search_rate=SEARCH_RATE)
    assert SEARCH_RATE == pytest.approx(9333.333333)
    assert serial.writes == [
        b"$$\n", b"$25=9333.33333\n", b"$110=9333.33333\n", b"$HX\n",
        b"$25=100\n", b"$110=2000\n", b"$$\n",
    ]
    assert serial.settings == original
    assert not controller._homing_recovery_file.exists()


def test_maximum_is_left_alone_if_already_fast_enough(controller):
    controller._serial.settings[110] = 10000
    controller.home("X", search_rate=SEARCH_RATE)
    assert not any(line.startswith(b"$110=") for line in controller._serial.writes)


def test_z_uses_1000_then_restores_60_without_changing_locating_or_direction(controller):
    serial = controller._serial
    serial.settings.update({25: 60, 23: 4})
    original = serial.settings.copy()
    controller.home("Z", search_rate=1000)
    assert serial.writes == [b"$$\n", b"$25=1000\n", b"$HZ\n", b"$25=60\n", b"$$\n"]
    assert serial.settings == original
    assert not controller._homing_recovery_file.exists()


def test_z_speed_cap_is_temporarily_raised_and_recovered_after_alarm(controller, monkeypatch):
    serial = controller._serial
    serial.settings.update({25: 60, 112: 100})
    serial.home_response = b"ALARM:9\n"
    with pytest.raises(RecoveryRequired, match="\\$HZ"):
        controller.home("Z", search_rate=500)
    assert b"$112=500\n" in serial.writes
    assert b"\x18" not in serial.writes
    assert json.loads(controller._homing_recovery_file.read_text())["settings"] == {"25": 60, "112": 100}
    recovered = reconnect(controller, monkeypatch)
    assert recovered.settings[25] == 60 and recovered.settings[112] == 100
    assert not controller.recovery_required


def test_failed_z_restoration_never_starts_x(controller, configured_machine):
    machine = configured_machine
    machine.grbl = controller
    controller._serial.ignore_restore = True
    with pytest.raises(RecoveryRequired, match="did not confirm"):
        machine.home()
    assert b"$HZ\n" in controller._serial.writes
    assert b"$HX\n" not in controller._serial.writes
    assert not machine.state.position_trusted


def test_individual_z_home_uses_configured_search_rate(configured_machine):
    machine = configured_machine
    machine.config.z_homing_search_rate = 750
    machine.grbl.homing_search_rates.clear()
    machine.home("Z")
    assert machine.grbl.homing_search_rates == [("Z", 750)]


def test_z_search_rate_is_included_in_preflight_before_movement(controller, configured_machine):
    configured_machine.grbl = controller
    controller._serial.settings.update({102: 8000, 25: 60, 112: 60})
    with pytest.raises(ControllerError, match="133333"):
        configured_machine.home("Z")
    assert controller._serial.writes == [b"$$\n"]


@pytest.mark.parametrize("key,value,match", [
    (22, 0, "requires"), (23, 1, "direction"), (130, 100, "one carousel revolution"),
    (25, 0, "positive"), (27, float("nan"), "did not report"),
])
def test_invalid_homing_configuration_cannot_move_or_change_settings(controller, key, value, match):
    controller._serial.settings[key] = value
    with pytest.raises(ControllerError, match=match):
        controller.home("X", search_rate=SEARCH_RATE)
    assert controller._serial.writes == [b"$$\n"]
    assert not controller._homing_recovery_file.exists()


@pytest.mark.parametrize("rate", [0, -1, float("nan"), float("inf")])
def test_invalid_search_speed_is_rejected(controller, rate):
    with pytest.raises(ControllerError):
        controller.home("X", search_rate=rate)
    assert not controller._serial.writes


def test_homing_alarm_retains_connection_without_reset_and_blocks_commands(controller):
    serial = controller._serial
    serial.home_response = b"ALARM:9\n"
    with pytest.raises(RecoveryRequired, match="Reconnect"):
        controller.home("X", search_rate=SEARCH_RATE)
    assert serial.writes[-1:] == [b"!"]
    assert b"\x18" not in serial.writes
    assert controller.connected
    assert controller._homing_recovery_file.exists()
    count = len(serial.writes)
    for command in (lambda: controller.home("Z"), controller.unlock,
                    lambda: controller.send_command("$H"),
                    lambda: controller.move_relative("Z", 1)):
        with pytest.raises(RecoveryRequired):
            command()
    assert len(serial.writes) == count


def reconnect(controller, monkeypatch):
    serial = HomingSerial(controller._serial.settings)
    controller.disconnect()
    monkeypatch.setattr("autosampler.controllers.grbl.open_serial_port", lambda *a, **k: serial)
    monkeypatch.setattr("autosampler.controllers.grbl.time.sleep", lambda *_: None)
    controller.connect()
    return serial


def test_lost_setting_ack_is_recovered_after_application_restart(controller, monkeypatch):
    serial = controller._serial
    serial.drop_ack = 25
    with pytest.raises(RecoveryRequired):
        controller.home("X", search_rate=SEARCH_RATE)
    record = json.loads(controller._homing_recovery_file.read_text())
    assert record["settings"] == {"25": 100, "110": 2000}
    settings = serial.settings
    restarted = SerialGrblController(controller.config, homing_recovery_file=controller._homing_recovery_file)
    restarted._serial = HomingSerial(settings)
    serial = reconnect(restarted, monkeypatch)
    assert serial.writes == [b"\x18", b"$25=100\n", b"$110=2000\n", b"$$\n"]
    assert serial.settings[25] == 100
    assert not restarted._homing_recovery_file.exists()
    restarted.home("Z")
    assert serial.writes[-1] == b"$HZ\n"


def test_reconnect_never_resets_or_restores_another_serial_port(controller, monkeypatch):
    controller._save_homing_settings({25: 100})
    controller.disconnect()
    controller.config.port = "other-controller"
    monkeypatch.setattr("autosampler.controllers.grbl.open_serial_port", lambda *a, **k: pytest.fail("Must not open another controller"))
    with pytest.raises(RecoveryRequired, match="original serial port"):
        controller.connect()


def test_restore_must_be_confirmed_by_settings_readback(controller):
    controller._serial.ignore_restore = True
    with pytest.raises(RecoveryRequired, match="did not confirm"):
        controller.home("X", search_rate=SEARCH_RATE)
    assert controller._homing_recovery_file.exists()


def test_abort_during_homing_never_restores_using_stale_ack(controller):
    serial = controller._serial
    serial.after_home = controller.abort
    with pytest.raises(RecoveryRequired, match="Reconnect"):
        controller.home("X", search_rate=SEARCH_RATE)
    assert b"$25=100\n" not in serial.writes
    assert controller._homing_recovery_file.exists()


def test_home_all_uses_axis_specific_search_rates(configured_machine):
    machine = configured_machine
    machine.grbl.commands.clear()
    machine.grbl.homing_search_rates.clear()
    machine.home()
    assert machine.grbl.commands == ["$HZ", "$HX", "$HA", "$HB"]
    assert machine.grbl.homing_search_rates == [("Z", 1000), ("X", SEARCH_RATE), ("A", None), ("B", None)]
    assert machine.state.position_trusted


def test_serial_home_all_restores_global_search_before_a_and_b(controller, configured_machine, monkeypatch):
    machine = configured_machine
    machine.grbl = controller
    serial = controller._serial
    serial.settings.update({25: 60, 103: 8000, 104: 8000, 113: 60, 114: 60})
    rates_at_command = {}
    original_write = serial.write
    def record_write(data):
        if data in (b"$HZ\n", b"$HX\n", b"$HA\n", b"$HB\n"):
            rates_at_command[data] = (serial.settings[25], serial.settings[110])
        return original_write(data)
    monkeypatch.setattr(serial, "write", record_write)
    machine.home()
    assert rates_at_command[b"$HZ\n"] == (1000, 2000)
    assert rates_at_command[b"$HX\n"] == pytest.approx((SEARCH_RATE, SEARCH_RATE))
    assert rates_at_command[b"$HA\n"] == (60, 2000)
    assert rates_at_command[b"$HB\n"] == (60, 2000)
    assert not controller._homing_recovery_file.exists()


def test_serial_home_all_never_starts_a_if_x_settings_fail_readback(controller, configured_machine, monkeypatch):
    machine = configured_machine
    machine.grbl = controller
    serial = controller._serial
    original_write = serial.write
    def fail_x_restore(data):
        if data == b"$HX\n":
            serial.ignore_restore = True  # ACK the original $25=100 without applying it.
        return original_write(data)
    monkeypatch.setattr(serial, "write", fail_x_restore)
    with pytest.raises(RecoveryRequired, match="did not confirm"):
        machine.home()
    assert b"$HX\n" in serial.writes
    assert b"$HA\n" not in serial.writes and b"$HB\n" not in serial.writes
    assert controller._homing_recovery_file.exists()
    assert controller.connected and controller.recovery_required
    assert b"\x18" not in serial.writes


def test_abort_between_axes_prevents_next_axis_and_position_trust(configured_machine, monkeypatch):
    machine = configured_machine
    original = machine.grbl.home
    def abort_after_x(axis=None, **kwargs):
        original(axis, **kwargs)
        if axis == "X":
            machine.abort()
    monkeypatch.setattr(machine.grbl, "home", abort_after_x)
    machine.grbl.commands.clear()
    with pytest.raises(OperationAborted, match="Abort"):
        machine.home()
    assert "$HA" not in machine.grbl.commands
    assert not machine.state.position_trusted
    assert not machine.state.homed_axes


@pytest.mark.parametrize("axis", ["Z", "X", "A", "B"])
def test_any_axis_missing_ack_retains_connection_without_reset(controller, axis):
    serial = controller._serial
    controller.HOMING_RESPONSE_TIMEOUT_S = 0
    with pytest.raises(RecoveryRequired, match=f"Homing \\$H{axis} failed"):
        controller.home(axis)
    assert serial.writes == [f"$H{axis}\n".encode(), b"!"]
    assert controller.connected
    assert controller.recovery_required
    before = len(serial.writes)
    for command in (controller.unlock, lambda: controller.home("B"), lambda: controller.move_relative("Z", 1)):
        with pytest.raises(RecoveryRequired, match="Reconnect"):
            command()
    assert len(serial.writes) == before


def test_a_timeout_never_starts_b_and_invalidates_all_position(controller, configured_machine, monkeypatch):
    machine = configured_machine
    machine.grbl = controller
    original_home = controller.home
    def home(axis=None, **kwargs):
        if axis == "A":
            controller.HOMING_RESPONSE_TIMEOUT_S = 0
        original_home(axis, **kwargs)
    monkeypatch.setattr(controller, "home", home)
    with pytest.raises(RecoveryRequired, match="\\$HA"):
        machine.home()
    assert b"$HB\n" not in controller._serial.writes
    assert b"$HA\n" in controller._serial.writes
    assert controller._serial.writes[-1:] == [b"!"]
    assert b"\x18" not in controller._serial.writes
    assert controller.connected
    assert not machine.state.homed and not machine.state.position_trusted
    assert "Homing A (3/4) failed" in machine.state.homing_progress
    assert machine.state.homing_axis_started_at is None


def test_reconnect_clears_failed_homing_latch_without_moving(controller, monkeypatch):
    controller.HOMING_RESPONSE_TIMEOUT_S = 0
    with pytest.raises(RecoveryRequired):
        controller.home("A")
    serial = reconnect(controller, monkeypatch)
    assert not controller.recovery_required
    assert not any(w.startswith(b"$H") for w in serial.writes)


def test_manual_homing_timeout_has_same_recovery_rules(controller):
    controller.HOMING_RESPONSE_TIMEOUT_S = 0
    with pytest.raises(RecoveryRequired, match="\\$HA"):
        controller.send_command("$HA")
    assert controller.recovery_required
    assert controller._serial.writes[-1:] == [b"!"]
    assert b"\x18" not in controller._serial.writes
    assert controller.connected


def test_duplicate_connect_never_reopens_live_port(controller, monkeypatch):
    monkeypatch.setattr("autosampler.controllers.grbl.open_serial_port", lambda *a, **k: pytest.fail("Must not reopen a live port"))
    serial = controller._serial
    controller.connect()
    assert controller._serial is serial
    assert serial.writes == []
    controller.HOMING_RESPONSE_TIMEOUT_S = 0
    with pytest.raises(RecoveryRequired):
        controller.home("A")
    writes = list(serial.writes)
    with pytest.raises(RecoveryRequired, match="connection retained"):
        controller.connect()
    assert serial.writes == writes
    assert controller.connected


def test_failed_settings_recovery_keeps_transport_open_and_position_untrusted(controller, configured_machine, monkeypatch):
    controller._save_homing_settings({25: 100})
    controller.disconnect()
    serial = HomingSerial()
    serial.settings[25] = 500
    serial.ignore_restore = True
    monkeypatch.setattr("autosampler.controllers.grbl.open_serial_port", lambda *a, **k: serial)
    monkeypatch.setattr("autosampler.controllers.grbl.time.sleep", lambda *_: None)
    machine = configured_machine
    machine.grbl = controller
    with pytest.raises(RecoveryRequired, match="restoration pending"):
        machine.connect_grbl()
    assert serial.is_open and controller.connected and machine.state.grbl_connected
    assert controller.recovery_required and controller._homing_recovery_file.exists()
    assert not machine.state.homed_axes and not machine.state.position_trusted
    # One reset belongs to the operator's explicit pending-settings recovery.
    # The restoration failure never adds a reset or closes that connection.
    assert serial.writes.count(b"\x18") == 1


def test_captured_8000_step_settings_block_home_all_before_z(controller, configured_machine):
    machine = configured_machine
    machine.grbl = controller
    serial = controller._serial
    serial.settings.update({25: 500, 103: 8000, 104: 8000, 113: 500, 114: 500})
    before = serial.settings.copy()
    with pytest.raises(ControllerError, match="A: 66667.*B: 66667"):
        machine.home()
    assert serial.writes == [b"$$\n"]
    assert serial.settings == before
    assert controller.connected and not controller.recovery_required
    # No physical action/reset took place, so preflight rejection alone must
    # not erase previously valid homing or turn it into a controller fault.
    assert machine.state.homed and machine.state.position_trusted


@pytest.mark.parametrize("seek,locate,maximum", [(500, 25, 60), (60, 25, 500), (60, 500, 60)])
def test_preflight_checks_search_locate_and_rapid_rate(controller, seek, locate, maximum):
    controller._serial.settings.update({103: 8000, 25: seek, 24: locate, 113: maximum})
    with pytest.raises(ControllerError, match="66667"):
        controller.validate_homing(("A",))
    assert controller._serial.writes == [b"$$\n"]


def test_lower_pulse_rates_pass_preflight_without_settings_writes(controller):
    controller._serial.settings.update({103: 8000, 104: 8000, 25: 60, 113: 60, 114: 60})
    controller.validate_homing(("Z", "X", "A", "B"), {"X": SEARCH_RATE})
    assert controller._serial.writes == [b"$$\n"]


def test_missing_resolution_fails_closed_before_any_motion(controller):
    del controller._serial.settings[103]
    with pytest.raises(ControllerError, match="Cannot validate A"):
        controller.validate_homing(("A",))
    assert controller._serial.writes == [b"$$\n"]


def test_raw_manual_home_cannot_bypass_pulse_rate_preflight(controller):
    controller._serial.settings.update({103: 8000, 25: 500, 113: 500})
    with pytest.raises(ControllerError, match="66667"):
        controller.send_command("$HA")
    assert controller._serial.writes == [b"$$\n"]


def test_reset_during_preflight_invalidates_existing_trust(controller, configured_machine, monkeypatch):
    machine = configured_machine
    machine.grbl = controller
    def restarted(*args):
        raise RecoveryRequired("Controller restarted during $$")
    monkeypatch.setattr(controller, "validate_homing", restarted)
    with pytest.raises(RecoveryRequired, match="restarted"):
        machine.home()
    assert not machine.state.homed_axes and not machine.state.position_trusted
    assert controller._serial.writes == []


@pytest.mark.parametrize("action", ["home", "unlock"])
def test_old_request_cannot_clear_new_abort(configured_machine, action):
    machine = configured_machine
    submitted_generation = machine._abort_generation
    machine.abort()
    commands = list(machine.grbl.commands)
    with pytest.raises(OperationAborted, match="cancelled"):
        if action == "home":
            machine.home(abort_generation=submitted_generation)
        else:
            machine.unlock_for_calibration(abort_generation=submitted_generation)
    assert machine.grbl.commands == commands
    assert machine.state.abort_requested
    assert not machine.state.position_trusted

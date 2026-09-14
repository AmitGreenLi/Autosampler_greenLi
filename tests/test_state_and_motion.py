import pytest

from autosampler.errors import ConnectionFailure, RecoveryRequired, ValidationError
from autosampler.controllers.mock_grbl import MockGrblController
from autosampler.models import Axis, OperationState
from autosampler.reference_sensor import ManualReferenceSensor


def test_connection_requires_home_before_move(configured_machine):
    machine = configured_machine
    machine.disconnect_grbl()
    machine.connect_grbl()
    with pytest.raises(RecoveryRequired):
        machine.move_axis(Axis.Z, 1, "mm")


def test_home_establishes_all_trusted_coordinates(configured_machine):
    state = configured_machine.state
    assert state.homed and state.position_trusted and state.carousel_referenced
    assert state.carousel_steps == 0
    assert all(value == 0 for value in state.positions.values())
    assert state.homed_axes == {axis.value for axis in Axis}


def test_realtime_status_tracks_pressed_limit_axes(configured_machine):
    machine = configured_machine
    machine.grbl.limit_pins = {"X", "A"}
    report = machine.refresh_grbl_status()
    assert "Pn:AX" in report
    assert machine.state.limit_pins_pressed == {"X", "A"}

    machine.grbl.limit_pins.clear()
    machine.refresh_grbl_status()
    assert machine.state.limit_pins_pressed == set()


def test_axes_can_be_homed_individually_without_prematurely_trusting_machine(configured_machine):
    machine = configured_machine
    machine.disconnect_grbl()
    machine.connect_grbl()

    for axis in (Axis.CAROUSEL, Axis.Z, Axis.PIPETTE_1):
        machine.home(axis)
        assert machine.grbl.commands[-1] == f"$H{axis.value}"
        assert not machine.state.homed
        assert not machine.state.position_trusted
        assert machine.state.operation_state is OperationState.RECOVERY_REQUIRED

    machine.home(Axis.PIPETTE_2)
    assert machine.grbl.commands[-1] == "$HB"
    assert machine.state.homed
    assert machine.state.position_trusted
    assert machine.state.homed_axes == {axis.value for axis in Axis}


def test_partially_homed_axis_can_be_jogged_and_counts_steps_from_home(configured_machine):
    machine = configured_machine
    machine.disconnect_grbl()
    machine.connect_grbl()
    machine.home(Axis.CAROUSEL)

    machine.move_axis(Axis.CAROUSEL, 15, "degrees")
    assert machine.steps_from_home(Axis.CAROUSEL) == 78
    assert machine.state.operation_state is OperationState.RECOVERY_REQUIRED

    with pytest.raises(RecoveryRequired):
        machine.move_axis(Axis.Z, 1, "mm")

    machine.home(Axis.CAROUSEL)
    assert machine.steps_from_home(Axis.CAROUSEL) == 0


def test_linear_axis_step_count_uses_its_configured_steps_per_mm(configured_machine):
    machine = configured_machine
    machine.move_axis(Axis.Z, 1.25, "mm")
    assert machine.steps_from_home(Axis.Z) == 100


def test_abort_invalidates_position_and_requires_operator_home(configured_machine):
    machine = configured_machine
    machine.abort()
    assert machine.state.operation_state is OperationState.RECOVERY_REQUIRED
    assert not machine.state.position_trusted
    assert machine.state.carousel_steps is None
    with pytest.raises(RecoveryRequired):
        machine.move_axis(Axis.Z, 1, "mm")
    machine.home()
    assert machine.state.position_trusted


def test_carousel_degree_move_is_integer_grbl_units(configured_machine):
    result = configured_machine.move_axis(Axis.CAROUSEL, 15, "degrees")
    assert result["integer_steps"] == 78
    assert configured_machine.grbl.commands[-1] == "G91 G0 X78"
    assert configured_machine.state.carousel_steps == 78


def test_raw_step_conversion_for_z(configured_machine):
    result = configured_machine.move_axis(Axis.Z, 80, "steps")
    assert result["grbl_amount"] == 1.0
    assert result["integer_steps"] == 80


def test_raw_steps_must_be_integral(configured_machine):
    with pytest.raises(ValidationError):
        configured_machine.move_axis(Axis.CAROUSEL, 1.2, "steps")


def test_reference_event_corrects_x_to_zero(configured_machine):
    configured_machine.move_axis(Axis.CAROUSEL, 20, "steps")
    configured_machine.record_carousel_reference()
    assert configured_machine.state.carousel_steps == 0
    assert configured_machine.state.carousel_referenced
    assert configured_machine.grbl.positions["X"] == 0
    assert configured_machine.grbl.commands[-1] == "G10 L20 P1 X0"


def test_unexpected_grbl_disconnection_enters_recovery(configured_machine):
    class DisconnectingGrbl(MockGrblController):
        def move_relative(self, axis, amount, feed=None):
            self._connected = False
            raise ConnectionFailure("GRBL USB device disappeared.")

    machine = configured_machine
    machine.grbl = DisconnectingGrbl()
    machine.connect_grbl()
    machine.home()
    with pytest.raises(ConnectionFailure):
        machine.move_axis(Axis.Z, 1, "mm")
    assert not machine.state.grbl_connected
    assert not machine.state.position_trusted
    assert machine.state.operation_state is OperationState.RECOVERY_REQUIRED


def test_reference_sensor_abstraction_can_correct_carousel(configured_machine):
    sensor = ManualReferenceSensor()
    configured_machine.move_axis(Axis.CAROUSEL, 12, "steps")
    assert not configured_machine.poll_reference_sensor(sensor)
    sensor.record_event()
    assert configured_machine.poll_reference_sensor(sensor)
    assert configured_machine.state.carousel_steps == 0


def test_clean_shutdown_saves_and_disconnects_without_motion(configured_machine):
    machine = configured_machine
    command_count = len(machine.grbl.commands)
    machine.shutdown()
    assert not machine.grbl.connected and not machine.scale.connected
    assert not machine.state.position_trusted
    assert machine.config.state_file.exists()
    # Disconnect is bookkeeping/transport closure, not a movement G-code.
    assert machine.grbl.commands[command_count:] == ["DISCONNECT"]


def test_calibration_unlock_sends_x_and_allows_only_relative_jogging(configured_machine):
    machine = configured_machine
    machine.disconnect_grbl()
    machine.connect_grbl()
    assert not machine.state.position_trusted
    assert machine.unlock_for_calibration() == "ok"
    assert machine.grbl.commands[-1] == "$X"
    assert machine.state.calibration_unlocked
    result = machine.move_axis(Axis.Z, 1, "mm")
    assert result["response"] == "ok"
    # A relative jog cannot establish an absolute position.
    assert machine.state.positions["Z"] is None
    with pytest.raises(RecoveryRequired):
        machine.go_to_spot("dest", machine.planner.plan(100).pipette)


def test_abort_clears_calibration_unlock(configured_machine):
    machine = configured_machine
    machine.disconnect_grbl()
    machine.connect_grbl()
    machine.unlock_for_calibration()
    machine.abort()
    assert not machine.state.calibration_unlocked
    with pytest.raises(RecoveryRequired):
        machine.move_axis(Axis.Z, 1, "mm")

import pytest

from autosampler.errors import OperationAborted, RecoveryRequired, ValidationError


def test_manual_command_available_before_calibration_and_home(configured_machine):
    machine = configured_machine
    machine.config.z.safe_position_mm = None
    machine.disconnect_grbl()
    machine.connect_grbl()
    assert machine.calibration_issues()
    responses = []
    machine.send_manual_command("  $100=1  ", responses.append)
    assert machine.grbl.commands[-1] == "  $100=1  "
    assert responses
    assert not machine.state.homed
    assert not machine.state.position_trusted


def test_manual_command_invalidates_existing_position_trust(configured_machine):
    configured_machine.send_manual_command("G1")
    assert not configured_machine.state.position_trusted
    assert not configured_machine.state.homed_axes
    assert configured_machine.state.current_operation is None


def test_manual_command_requires_connection_and_preserves_abort(configured_machine):
    machine = configured_machine
    machine.abort()
    commands = list(machine.grbl.commands)
    with pytest.raises(OperationAborted):
        machine.send_manual_command("$X")
    assert machine.grbl.commands == commands
    assert machine.state.abort_requested
    machine.disconnect_grbl()
    with pytest.raises(RecoveryRequired):
        machine.send_manual_command("$$")


def test_manual_command_queued_before_abort_cannot_run_after_home(configured_machine):
    machine = configured_machine
    generation = machine._abort_generation
    machine.abort()
    machine.home()
    with pytest.raises(OperationAborted):
        machine.send_manual_command("G1", abort_generation=generation)


def test_abort_during_manual_command_keeps_recovery_state(configured_machine, monkeypatch):
    machine = configured_machine

    def interrupted(command, on_response):
        machine.abort()
        return "ok"

    monkeypatch.setattr(machine.grbl, "send_command", interrupted)
    with pytest.raises(OperationAborted):
        machine.send_manual_command("G1")
    assert machine.state.abort_requested
    assert not machine.state.position_trusted
    assert machine.state.current_operation is None


def test_connection_loss_during_manual_command_updates_state(configured_machine, monkeypatch):
    from autosampler.errors import ConnectionFailure

    machine = configured_machine

    def disconnected(command, on_response):
        machine.grbl.disconnect()
        raise ConnectionFailure("USB connection lost")

    monkeypatch.setattr(machine.grbl, "send_command", disconnected)
    with pytest.raises(ConnectionFailure):
        machine.send_manual_command("$$")
    assert not machine.state.grbl_connected
    assert not machine.state.position_trusted


@pytest.mark.parametrize("command", ["", "   ", "G1\n$X", "G1\r", "\x18", "é"])
def test_manual_command_rejects_empty_or_multiple_lines(configured_machine, command):
    machine = configured_machine
    commands = list(machine.grbl.commands)
    with pytest.raises(ValidationError):
        machine.send_manual_command(command)
    assert machine.grbl.commands == commands
    assert machine.state.position_trusted

from copy import deepcopy

import pytest

from autosampler.calibration import get_value, load_calibration, measurements, set_value
from autosampler.config import MachineConfig
from autosampler.errors import MissingCalibration, RecoveryRequired, ValidationError
from autosampler.models import PipetteId


def test_guided_measurements_fill_typed_config_without_motion(configured_machine, tmp_path):
    machine = configured_machine
    expected = deepcopy(machine.config)
    machine.config = MachineConfig()
    machine.calibration_file = tmp_path / "calibration.json"
    commands = list(machine.grbl.commands)
    for spec in measurements(machine.inventory.vessels):
        assert machine.calibration_issues()
        machine.record_calibration(spec.key, get_value(expected, spec.key), confirmed=True)
    assert not machine.calibration_issues()
    assert machine.grbl.commands == commands
    assert isinstance(machine.config.transfer.max_correction_attempts, int)
    restored = MachineConfig()
    load_calibration(restored, machine.calibration_file, machine.inventory.vessels)
    for spec in measurements(machine.inventory.vessels):
        assert get_value(restored, spec.key) == get_value(expected, spec.key)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0, True, "12"])
def test_invalid_measurements_do_not_mutate(configured_machine, value):
    machine = configured_machine
    before = deepcopy(machine.config)
    with pytest.raises(ValidationError):
        machine.record_calibration("z.max_travel_mm", value, True)
    assert machine.config == before


def test_confirmation_and_consistency_required(configured_machine):
    machine = configured_machine
    for key, value, confirmed in (
        ("z.safe_position_mm", 1, False),
        ("z.safe_position_mm", 999, True),
        ("pipette_1.volume_per_mm_ul", 0.1, True),
        ("transfer.max_correction_attempts", 1.5, True),
        ("grbl.port", 1, True),
    ):
        before = deepcopy(machine.config)
        with pytest.raises(ValidationError):
            machine.record_calibration(key, value, confirmed)
        assert machine.config == before


@pytest.mark.parametrize("operation", [
    lambda m: m.go_to_spot("source", PipetteId.P1),
    lambda m: m.draw("source", 100, PipetteId.P1),
    lambda m: m.dispense("dest", 100, PipetteId.P1),
    lambda m: m.dump(PipetteId.P1),
    lambda m: m.clean(PipetteId.P1),
    lambda m: m.transfer("source", "dest", 100),
    lambda m: m.empty_pipette(PipetteId.P1),
])
def test_every_automated_entry_point_is_locked_before_motion(configured_machine, operation):
    machine = configured_machine
    machine.config.pipette_2_positions_deg["waste"] = None
    before = list(machine.grbl.commands)
    with pytest.raises(MissingCalibration):
        operation(machine)
    assert machine.grbl.commands == before


def test_each_required_measurement_locks_automation(configured_machine):
    machine = configured_machine
    before = list(machine.grbl.commands)
    for spec in measurements(machine.inventory.vessels):
        value = get_value(machine.config, spec.key)
        set_value(machine.config, spec.key, None)
        with pytest.raises(MissingCalibration):
            machine.go_to_spot("source", PipetteId.P1)
        set_value(machine.config, spec.key, value)
    assert machine.grbl.commands == before


def test_recording_does_not_restore_trust_or_disable_manual_recovery(configured_machine):
    machine = configured_machine
    machine.abort()
    machine.record_calibration("z.safe_position_mm", 0, True)
    assert not machine.state.position_trusted
    with pytest.raises(RecoveryRequired):
        machine.go_to_spot("source", PipetteId.P1)
    machine.unlock_for_calibration()
    machine.config.z.safe_position_mm = None
    machine.move_axis("Z", 0.1, "mm")
    assert not machine.state.position_trusted
    machine.home()
    with pytest.raises(MissingCalibration):
        machine.go_to_spot("source", PipetteId.P1)


def test_failed_save_keeps_previous_config(configured_machine, tmp_path):
    machine = configured_machine
    machine.calibration_file = tmp_path  # a directory cannot be replaced by a file
    with pytest.raises(OSError):
        machine.record_calibration("z.safe_position_mm", 1, True)
    assert machine.config.z.safe_position_mm == 0


def test_bad_file_does_not_partially_load(configured_machine, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"version":1,"values":{"z.safe_position_mm":2,"z.max_travel_mm":-1}}')
    before = deepcopy(configured_machine.config)
    with pytest.raises(ValidationError):
        load_calibration(configured_machine.config, path, configured_machine.inventory.vessels)
    assert configured_machine.config == before


def test_runtime_restores_measurements_without_trust_and_isolates_mock(tmp_path, monkeypatch):
    from autosampler.app_paths import ApplicationPaths
    from autosampler.main import build_runtime

    paths = ApplicationPaths.under(tmp_path)
    first, _, _ = build_runtime(True, paths)
    first.record_calibration("z.safe_position_mm", 2, True)
    first.shutdown()
    second, _, _ = build_runtime(True, paths)
    assert second.config.z.safe_position_mm == 2
    assert not second.state.position_trusted
    assert not second.state.homed
    second.shutdown()
    monkeypatch.setattr("autosampler.main.discover_serial_devices", lambda: [])
    hardware, _, _ = build_runtime(False, paths)
    assert hardware.config.z.safe_position_mm is None
    assert hardware.calibration_file != second.calibration_file
    hardware.shutdown()


def test_gui_confirmation_recording_and_lock(configured_machine, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from autosampler.app_paths import ApplicationPaths
    from autosampler.devices import DevicePreferenceStore
    from autosampler.gui import AutosamplerWindow

    app = QApplication.instance() or QApplication([])
    paths = ApplicationPaths.under(tmp_path)
    machine = configured_machine
    machine.config.z.safe_position_mm = None
    window = AutosamplerWindow(machine, paths, DevicePreferenceStore(paths.device_preferences))
    try:
        window._submit = lambda name, function, *args: function(*args)
        errors = []
        window.show_error = lambda title, error: errors.append(str(error))
        assert not any(button.isEnabled() for button in window.operation_buttons)
        window.measurement_combo.setCurrentIndex(window.measurement_combo.findData("z.safe_position_mm"))
        window.measurement_value.setText("0")
        window.record_measurement_button.click()
        assert errors and machine.config.z.safe_position_mm is None
        window.measurement_confirm.setChecked(True)
        window.measurement_value.setText("1")
        assert not window.measurement_confirm.isChecked()
        window.measurement_confirm.setChecked(True)
        window.record_measurement_button.click()
        assert machine.config.z.safe_position_mm == 1
        window._render_state(machine.state)
        assert not any(button.isEnabled() for button in window.operation_buttons)
        assert "exactly one complete Wash and one complete Dump" in window.vessel_status.text()
        machine.abort()
        window._render_state(machine.state)
        assert not any(button.isEnabled() for button in window.operation_buttons)
    finally:
        window.close()
        app.processEvents()

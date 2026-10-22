import json

import pytest

from autosampler.calibration import load_calibration, save_calibration
from autosampler.config import MachineConfig
from autosampler.errors import MissingCalibration, ValidationError
from autosampler.models import PipetteId, ScaleReading


def negative_machine(machine, tmp_path):
    path = tmp_path / "legacy.json"
    save_calibration(machine.config, path, machine.inventory.vessels)
    data = json.loads(path.read_text())
    data.pop("z_work_direction")
    data["values"].update({
        "z.max_travel_mm": 250, "z.safe_position_mm": 125,
        "z.dispense_position_mm": 160,
        "aspiration.vial_bottom_mm": 243, "aspiration.beaker_bottom_mm": 230,
        "aspiration.z_up_direction": -1,
    })
    path.write_text(json.dumps(data))
    config = MachineConfig()
    load_calibration(config, path, machine.inventory.vessels)
    machine.config = config
    return machine


def test_legacy_positive_distances_convert_to_negative_coordinates(configured_machine, tmp_path):
    m = negative_machine(configured_machine, tmp_path)
    assert m.config.z.work_direction == -1
    assert m.config.z.safe_position_mm == -125
    assert m.config.z.dispense_position_mm == -160
    assert m.config.aspiration.vial_bottom_mm == -243
    assert m.config.aspiration.beaker_bottom_mm == -230
    assert m.config.aspiration.z_up_direction == 1
    assert m.config.z.max_travel_mm == 250
    assert not m.calibration_issues()
    path = tmp_path / "signed.json"
    save_calibration(m.config, path, m.inventory.vessels)
    restored = MachineConfig()
    load_calibration(restored, path, m.inventory.vessels)
    assert restored.z == m.config.z
    assert restored.aspiration == m.config.aspiration


@pytest.mark.parametrize("value", [125, -125])
def test_position_entry_accepts_distance_or_negative_coordinate(configured_machine, tmp_path, value):
    m = negative_machine(configured_machine, tmp_path)
    m.record_calibration("z.safe_position_mm", value, True)
    assert m.config.z.safe_position_mm == -125
    before = len(m.grbl.commands)
    m.go_to_spot("source", PipetteId.P1)
    assert m.grbl.commands[before] == "G90 G0 Z-125 F100"


@pytest.mark.parametrize("target", [1, 125, -251, float("nan")])
def test_wrong_side_or_out_of_range_z_rejected_before_positioning(configured_machine, tmp_path, target):
    m = negative_machine(configured_machine, tmp_path)
    m.config.z.safe_position_mm = target
    before = list(m.grbl.commands)
    with pytest.raises(MissingCalibration):
        m.go_to_spot("source", PipetteId.P1)
    assert m.grbl.commands == before
    with pytest.raises(ValidationError):
        m._move_z(target)
    assert m.grbl.commands == before


def test_liquid_commands_stay_inside_negative_envelope(configured_machine, tmp_path):
    m = negative_machine(configured_machine, tmp_path)
    m.scale.queue(ScaleReading(10, True), ScaleReading(9.9, True))
    m.draw("source", 100, PipetteId.P1)
    m.dispense("dest", 100, PipetteId.P1)
    z_commands = [c for c in m.grbl.commands if c.startswith("G90 G0 Z")]
    assert z_commands
    assert all(-250 <= float(c.split("Z", 1)[1].split()[0]) <= 0 for c in z_commands)
    assert "G90 G0 Z-160" in z_commands


def test_aspiration_target_cannot_cross_home(configured_machine, tmp_path):
    m = negative_machine(configured_machine, tmp_path)
    m.config.aspiration.vial_bottom_mm = -1
    before = list(m.grbl.commands)
    with pytest.raises(ValidationError, match="work envelope"):
        m.draw("source", 100, PipetteId.P1)
    assert m.grbl.commands == before

import json
import re

import pytest

from autosampler.calibration import load_calibration, save_calibration
from autosampler.config import MachineConfig
from autosampler.errors import MissingCalibration, ValidationError, OperationAborted
from autosampler.models import PipetteId, ScaleReading


@pytest.fixture
def negative_liquid_machine(configured_machine):
    m = configured_machine
    m.config.z.work_direction = -1
    m.config.z.max_travel_mm = 250
    m.config.z.safe_position_mm = -125
    m.config.z.dispense_position_mm = -160
    m.config.aspiration.vial_bottom_mm = -243
    m.config.aspiration.beaker_bottom_mm = -230
    m.config.aspiration.z_up_direction = 1
    for cfg in (m.config.pipette_1, m.config.pipette_2):
        cfg.work_direction = -1
        cfg.contact_position_mm = -1
    assert not m.calibration_issues()
    return m


def check_targets(m, commands):
    for command in commands:
        match = re.match(r"G90 G0 ([ZAB])([-\d.e+]+)", command)
        if not match:
            continue
        axis, value = match.groups()
        target = float(value)
        cfg = {"Z": m.config.z, "A": m.config.pipette_1, "B": m.config.pipette_2}[axis]
        assert cfg.target_error(target) is None
        assert target <= 0


@pytest.mark.parametrize("pipette", [PipetteId.P1, PipetteId.P2])
@pytest.mark.parametrize("operation", ["draw", "under", "over", "dispense", "dump", "clean"])
def test_every_liquid_sequence_uses_negative_coordinates(negative_liquid_machine, pipette, operation):
    m = negative_liquid_machine
    cfg = m._pipette_config(pipette)
    start = len(m.grbl.commands)
    if operation in ("draw", "under", "over"):
        readings = {
            "draw": [10, 9.9], "under": [10, 9.95, 9.9], "over": [10, 9.89, 9.9],
        }[operation]
        m.scale.queue(*(ScaleReading(value, True) for value in readings))
        assert m.draw("source", 100, pipette) == pytest.approx(100)
        assert m.state.pipette_contents_ul[pipette.value] == pytest.approx(100)
    elif operation == "dispense":
        m.state.pipette_contents_ul[pipette.value] = 120
        m.dispense("dest", 100, pipette)
        assert m.state.pipette_contents_ul[pipette.value] == 0
    elif operation == "dump":
        m.state.pipette_contents_ul[pipette.value] = 120
        assert m.dump(pipette) == 120
    else:
        before = m.inventory.get("clean").quantity_ul
        m.scale.queue(ScaleReading(50, True), ScaleReading(50 - cfg.max_volume_ul / 1000, True))
        m.clean(pipette, cycles=1)
        assert m.inventory.get("clean").quantity_ul == pytest.approx(before)
    commands = m.grbl.commands[start:]
    check_targets(m, commands)
    axis_targets = [float(c.split(cfg.axis, 1)[1].split()[0]) for c in commands if c.startswith(f"G90 G0 {cfg.axis}")]
    assert axis_targets
    assert axis_targets[-1] == 0
    if operation == "dump":
        assert axis_targets == [-20, 0, -20, 0]
    else:
        assert axis_targets[0] == -1
        assert axis_targets[1] < axis_targets[0]
        assert axis_targets[2] == 0


@pytest.mark.parametrize("amount,pipette", [(100, PipetteId.P1), (1200, PipetteId.P2)])
def test_transfer_both_pipettes_have_correct_motion_and_mass_signs(negative_liquid_machine, amount, pipette):
    m = negative_liquid_machine
    m.scale.queue(ScaleReading(10, True), ScaleReading(10 - amount / 1000, True))
    start = len(m.grbl.commands)
    plan = m.transfer("source", "dest", amount)
    assert plan.pipette is pipette
    assert m.inventory.get("source").quantity_ul == pytest.approx(10000 - amount)
    assert m.inventory.get("dest").quantity_ul == pytest.approx(amount)
    check_targets(m, m.grbl.commands[start:])


@pytest.mark.parametrize("pipette", [PipetteId.P1, PipetteId.P2])
def test_wrong_side_contact_and_overtravel_fail_before_motion(negative_liquid_machine, pipette):
    m = negative_liquid_machine
    cfg = m._pipette_config(pipette)
    before = list(m.grbl.commands)
    for contact in (1, -30):
        cfg.contact_position_mm = contact
        with pytest.raises(MissingCalibration):
            m.draw("source", 100, pipette)
        assert m.grbl.commands == before


def test_legacy_contacts_convert_and_new_signed_file_round_trips(negative_liquid_machine, tmp_path):
    m = negative_liquid_machine
    path = tmp_path / "calibration.json"
    save_calibration(m.config, path, m.inventory.vessels)
    data = json.loads(path.read_text())
    data.pop("pipette_work_directions")
    data["values"]["pipette_1.contact_position_mm"] = 1
    data["values"]["pipette_2.contact_position_mm"] = 2
    path.write_text(json.dumps(data))
    config = MachineConfig()
    load_calibration(config, path, m.inventory.vessels)
    assert config.pipette_1.contact_position_mm == -1
    assert config.pipette_2.contact_position_mm == -2
    assert config.pipette_1.max_travel_mm == 20
    assert config.pipette_1.volume_per_step_ul > 0
    save_calibration(config, path, m.inventory.vessels)
    restored = MachineConfig()
    load_calibration(restored, path, m.inventory.vessels)
    assert restored.pipette_1 == config.pipette_1
    assert restored.pipette_2 == config.pipette_2


@pytest.mark.parametrize("pipette", [PipetteId.P1, PipetteId.P2])
def test_empty_rounds_inward_to_safe_step_and_rejects_wrong_side(negative_liquid_machine, pipette):
    m = negative_liquid_machine
    cfg = m._pipette_config(pipette)
    cfg.max_travel_mm = 20.0001
    assert cfg.empty_position_mm == -20
    before = list(m.grbl.commands)
    with pytest.raises(ValidationError):
        m._move_pipette(cfg, 0.1)
    assert m.grbl.commands == before


def test_insufficient_contents_rejected_before_z_moves(negative_liquid_machine):
    m = negative_liquid_machine
    before = list(m.grbl.commands)
    with pytest.raises(ValidationError, match="insufficient"):
        m.dispense("dest", 100, PipetteId.P1)
    assert m.grbl.commands == before


@pytest.mark.parametrize("pipette", [PipetteId.P1, PipetteId.P2])
def test_abort_between_contact_and_stroke_prevents_following_targets(negative_liquid_machine, pipette, monkeypatch):
    m = negative_liquid_machine
    cfg = m._pipette_config(pipette)
    original = m.grbl.move_absolute
    def stop_after_contact(axis, position, feed=None):
        response = original(axis, position, feed)
        m.state.abort_requested = True
        return response
    monkeypatch.setattr(m.grbl, "move_absolute", stop_after_contact)
    start = len(m.grbl.commands)
    with pytest.raises(OperationAborted):
        m._aspirate_motion(100, pipette)
    assert m.grbl.commands[start:] == [f"G90 G0 {cfg.axis}-1"]


def test_wrong_z_height_sign_blocks_liquid_but_not_positioning(negative_liquid_machine):
    m = negative_liquid_machine
    m.config.aspiration.z_up_direction = -1
    before = list(m.grbl.commands)
    with pytest.raises(MissingCalibration, match="upward direction"):
        m.draw("source", 100, PipetteId.P1)
    assert m.grbl.commands == before
    m.go_to_spot("source", PipetteId.P1)


@pytest.mark.parametrize("value", [-1, 0, float("nan"), float("inf")])
def test_invalid_liquid_amounts_cannot_start_motion(negative_liquid_machine, value):
    m = negative_liquid_machine
    before = list(m.grbl.commands)
    for operation in (
        lambda: m.draw("source", value, PipetteId.P1),
        lambda: m.dispense("dest", value, PipetteId.P1),
        lambda: m.transfer("source", "dest", value),
    ):
        with pytest.raises(ValidationError):
            operation()
        assert m.grbl.commands == before


@pytest.mark.parametrize("pipette,contact,limit", [(PipetteId.P1, 14.8, 34.2), (PipetteId.P2, 56.6, 79.5)])
@pytest.mark.parametrize("entry_sign", [-1, 1])
def test_operator_contact_distances_and_signed_coordinates(negative_liquid_machine, pipette, contact, limit, entry_sign):
    m = negative_liquid_machine
    cfg = m._pipette_config(pipette)
    cfg.max_travel_mm = limit
    number = 1 if pipette is PipetteId.P1 else 2
    m.record_calibration(f"pipette_{number}.contact_position_mm", entry_sign * contact, True)
    assert cfg.contact_position_mm == -contact
    assert cfg.max_travel_mm == limit
    start = len(m.grbl.commands)
    m._aspirate_motion(100, pipette)
    m._empty_pipette(pipette)
    check_targets(m, m.grbl.commands[start:])
    assert m.grbl.commands[-2:] == [f"G90 G0 {cfg.axis}{cfg.empty_position_mm:.10g} F100", f"G90 G0 {cfg.axis}0 F100"]

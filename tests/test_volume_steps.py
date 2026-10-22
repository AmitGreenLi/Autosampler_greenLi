import json

import pytest

from autosampler.calibration import load_calibration, save_calibration
from autosampler.config import MachineConfig
from autosampler.errors import ValidationError
from autosampler.models import PipetteId


@pytest.mark.parametrize("pipette", [PipetteId.P1, PipetteId.P2])
def test_volume_rounds_to_whole_driver_steps(configured_machine, pipette):
    m = configured_machine
    cfg = m._pipette_config(pipette)
    cfg.volume_per_step_ul = 0.05
    travel = m._travel_for_volume(1.08, pipette)
    assert travel == 22 / cfg.steps_per_mm
    m._aspirate_motion(1.08, pipette)
    endpoint = cfg.contact_position_mm + 22 / cfg.steps_per_mm
    assert f"G90 G0 {cfg.axis}{endpoint:.10g} F100" in m.grbl.commands
    assert m.state.pipette_contents_ul[pipette.value] == pytest.approx(1.1)


def test_substep_request_cannot_silently_send_zero_stroke(configured_machine):
    m = configured_machine
    m.config.pipette_1.volume_per_step_ul = 0.05
    before = list(m.grbl.commands)
    with pytest.raises(ValidationError, match="zero driver steps"):
        m._aspirate_motion(0.02, PipetteId.P1)
    assert m.grbl.commands == before


def test_rounded_contact_and_endpoint_obey_stroke_limit(configured_machine):
    m = configured_machine
    cfg = m.config.pipette_1
    cfg.volume_per_step_ul = 1
    cfg.contact_position_mm = 1 + 0.6 / cfg.steps_per_mm
    cfg.max_travel_mm = 1 + 1 / cfg.steps_per_mm
    with pytest.raises(ValidationError, match="maximum safe travel"):
        m._aspirate_motion(1, PipetteId.P1)


def test_dispense_inventory_tracks_quantized_stroke(configured_machine):
    m = configured_machine
    cfg = m.config.pipette_1
    # Preserve full-volume travel validity while exercising fractional-volume rounding.
    cfg.volume_per_step_ul = 0.05
    m.state.pipette_contents_ul["P1"] = 2
    m.dispense("dest", 1.08, PipetteId.P1)
    assert m.inventory.get("dest").quantity_ul == pytest.approx(1.1)
    assert m.inventory.get("waste").quantity_ul is None
    assert m.state.pipette_contents_ul["P1"] == 0


def test_old_volume_per_mm_converts_independently_for_each_axis(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({"version": 1, "values": {
        "pipette_1.volume_per_mm_ul": 20,
        "pipette_2.volume_per_mm_ul": 1000,
    }}))
    config = MachineConfig()
    config.pipette_1.steps_per_mm = 1600
    config.pipette_2.steps_per_mm = 800
    load_calibration(config, path, [])
    assert config.pipette_1.volume_per_step_ul == 0.0125
    assert config.pipette_2.volume_per_step_ul == 1.25
    save_calibration(config, path, [])
    values = json.loads(path.read_text())["values"]
    assert values["pipette_1.volume_per_step_ul"] == 0.0125
    assert "pipette_1.volume_per_mm_ul" not in values


def test_new_step_calibration_takes_precedence_over_legacy_value(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({"version": 1, "values": {
        "pipette_1.volume_per_mm_ul": 20,
        "pipette_1.volume_per_step_ul": 0.02,
    }}))
    config = MachineConfig()
    load_calibration(config, path, [])
    assert config.pipette_1.volume_per_step_ul == 0.02

import json

import pytest

from autosampler.calibration import (
    CAROUSEL_OFFSETS_DEG, carousel_angle, load_calibration, measurements, save_calibration,
)
from autosampler.config import MachineConfig
from autosampler.errors import MissingCalibration, ValidationError
from autosampler.models import PipetteId
from autosampler.motion.carousel import degrees_to_steps
from autosampler.vessels import Vessel, VesselCategory, VesselType


def test_layout_closes_circle_in_positive_numbering_direction():
    config = MachineConfig()
    config.pipette_1_positions_deg["V24"] = 276
    assert [carousel_angle(config, f"V{n:02d}", 1) for n in range(1, 25)] == list(range(0, 277, 12))
    assert carousel_angle(config, "B2", 1) == 305
    assert carousel_angle(config, "B1", 1) == 331
    assert 23 * 12 + 29 + 26 + 29 == 360


@pytest.mark.parametrize("pipette,number", [(PipetteId.P1, 1), (PipetteId.P2, 2)])
def test_all_physical_targets_use_selected_anchor(configured_machine, pipette, number):
    m = configured_machine
    mapping = getattr(m.config, f"pipette_{number}_positions_deg")
    mapping["V24"] = 350.25
    for index, (location, offset) in enumerate(CAROUSEL_OFFSETS_DEG.items(), 1):
        kind = VesselType.VIAL_15ML if location.startswith("V") else VesselType.BEAKER_100ML
        m.inventory.add(Vessel(location, index, location, VesselCategory.INPUT, kind))
        if location != "V24":
            mapping[location] = 123  # obsolete independent measurements must not take precedence
        assert m.go_to_spot(location, pipette) == degrees_to_steps((350.25 + offset) % 360)
    mapping["V24"] = 10
    assert m.go_to_spot("B2", pipette) == degrees_to_steps(39)


def test_anchor_required_and_empty_slots_still_blocked(configured_machine):
    m = configured_machine
    before = list(m.grbl.commands)
    with pytest.raises(ValidationError, match="not configured"):
        m.go_to_spot("V01", PipetteId.P1)
    assert m.grbl.commands == before
    m.config.pipette_1_positions_deg["V24"] = None
    m.config.pipette_1_positions_deg["B2"] = 100
    m.inventory.add(Vessel("B2", 26, "Beaker", VesselCategory.INPUT, VesselType.BEAKER_100ML))
    with pytest.raises(MissingCalibration):
        m.go_to_spot("B2", PipetteId.P1)
    assert m.grbl.commands == before


def test_only_anchors_are_calibrated_and_persisted(configured_machine, tmp_path):
    m = configured_machine
    specs = measurements(CAROUSEL_OFFSETS_DEG)
    assert [s.key for s in specs if "positions_deg" in s.key] == [
        "pipette_1_positions_deg.V24", "pipette_2_positions_deg.V24",
    ]
    path = tmp_path / "layout.json"
    save_calibration(m.config, path, CAROUSEL_OFFSETS_DEG)
    values = json.loads(path.read_text())["values"]
    assert "pipette_1_positions_deg.B2" not in values
    config = MachineConfig()
    load_calibration(config, path, CAROUSEL_OFFSETS_DEG)
    assert carousel_angle(config, "B1", 1) == 55
    assert carousel_angle(config, "B1", 2) == 75


def test_legacy_individual_spots_do_not_replace_anchor(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": 1, "values": {
        "pipette_1_positions_deg.B2": 200,
        "pipette_1_positions_deg.V24": 10,
        "pipette_2_positions_deg.B2": 250,
    }}))
    config = MachineConfig()
    load_calibration(config, path, CAROUSEL_OFFSETS_DEG)
    assert carousel_angle(config, "B2", 1) == 39
    assert carousel_angle(config, "B2", 2) is None

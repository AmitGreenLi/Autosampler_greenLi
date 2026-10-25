import pytest

from autosampler.calibration import load_calibration, save_calibration
from autosampler.config import MachineConfig
from autosampler.errors import MissingCalibration, ValidationError
from autosampler.models import PipetteId


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("pipette", [PipetteId.P1, PipetteId.P2])
@pytest.mark.parametrize("location,family", [("source", "vial"), ("clean", "beaker")])
def test_aspiration_uses_shared_tip_height_and_direction(configured_machine, direction, pipette, location, family):
    m = configured_machine
    a = m.config.aspiration
    a.z_up_direction = direction
    a.vial_bottom_mm = 100
    a.beaker_bottom_mm = 100
    m.config.z.max_travel_mm = 300
    vessel = m.inventory.get(location)
    height = m._geometry(vessel).liquid_height_mm(vessel.quantity_ul) - 1
    bottom = getattr(a, f"{family}_bottom_mm")
    assert m.aspiration_z(vessel, pipette) == pytest.approx(bottom + direction * height)


@pytest.mark.parametrize("problem", ["missing", "low_liquid", "above_rim", "travel"])
def test_invalid_aspiration_fails_before_motion(configured_machine, problem):
    m = configured_machine
    if problem == "missing":
        m.config.aspiration.beaker_bottom_mm = None
    elif problem == "low_liquid":
        m.inventory.get("source").quantity_ul = 100
        m.config.aspiration.bottom_clearance_mm = 10
    elif problem == "above_rim":
        m.config.aspiration.vial_internal_height_mm = 25
    else:
        m.config.z.max_travel_mm = 50
    commands = list(m.grbl.commands)
    with pytest.raises((MissingCalibration, ValidationError)):
        m.draw("source", 50, PipetteId.P1)
    assert m.grbl.commands == commands


def test_new_constants_round_trip_and_old_offset_cannot_unlock(configured_machine, tmp_path):
    m = configured_machine
    path = tmp_path / "calibration.json"
    save_calibration(m.config, path, m.inventory.vessels)
    restored = MachineConfig()
    load_calibration(restored, path, m.inventory.vessels)
    assert restored.aspiration == m.config.aspiration
    restored.aspiration.vial_bottom_mm = None
    restored.z.aspiration_machine_offset_mm = 123
    m.config = restored
    with pytest.raises(MissingCalibration):
        m.aspiration_z(m.inventory.get("source"), PipetteId.P1)


def test_direction_and_geometry_validation(configured_machine):
    m = configured_machine
    for key, value in (
        ("aspiration.z_up_direction", 0),
        ("aspiration.vial_cone_height_mm", 200),
        ("aspiration.vial_internal_diameter_mm", float("nan")),
        ("aspiration.bottom_clearance_mm", 0),
    ):
        with pytest.raises(ValidationError):
            m.record_calibration(key, value, True)
    m.record_calibration("aspiration.vial_cone_height_mm", 0, True)
    assert m.aspiration_z(m.inventory.get("source"), PipetteId.P1) > 5


@pytest.mark.parametrize("old_values,expected", [([5, 5], 5), ([5, 6], None), ([5, None], None)])
def test_legacy_tip_offsets_require_agreement(tmp_path, old_values, expected):
    import json

    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": 1, "values": {
        "aspiration.vial_p1_bottom_mm": old_values[0],
        "aspiration.vial_p2_bottom_mm": old_values[1],
    }}))
    config = MachineConfig()
    load_calibration(config, path, [])
    assert config.aspiration.vial_bottom_mm == expected


@pytest.mark.parametrize("location", ["source", "clean"])
def test_draw_positions_tip_at_calculated_depth_below_surface(configured_machine, location):
    from autosampler.models import ScaleReading

    m = configured_machine
    m.config.transfer.aspiration_immersion_depth_mm = 2.0
    vessel = m.inventory.get(location)
    surface_height = m._geometry(vessel).liquid_height_mm(vessel.quantity_ul - 100)
    family = "vial" if location == "source" else "beaker"
    bottom = getattr(m.config.aspiration, f"{family}_bottom_mm")
    expected = bottom + m.config.aspiration.z_up_direction * (surface_height - 2)
    m.scale.queue(ScaleReading(10.0, True), ScaleReading(9.9, True))
    m.draw(location, 100, PipetteId.P1)
    assert f"G90 G0 Z{expected:.10g}" in m.grbl.commands


def test_deprecated_liquid_clearance_cannot_replace_immersion(tmp_path):
    import json
    from autosampler.calibration import calibration_issues, measurements

    path = tmp_path / "old-clearance.json"
    path.write_text(json.dumps({"version": 1, "values": {
        "transfer.z_liquid_clearance_mm": 5,
        "transfer.aspiration_immersion_depth_mm": None,
    }}))
    config = MachineConfig()
    load_calibration(config, path, [])
    assert config.transfer.aspiration_immersion_depth_mm is None
    assert "Aspiration: tip depth below liquid surface (mm)" in calibration_issues(config, [])
    assert "transfer.z_liquid_clearance_mm" not in [m.key for m in measurements([])]


def test_zero_surface_depth_is_rejected(configured_machine):
    with pytest.raises(ValidationError):
        configured_machine.record_calibration("transfer.aspiration_immersion_depth_mm", 0, True)


@pytest.mark.parametrize("direction", [-1, 1])
def test_one_ml_reference_excludes_tip_volume(configured_machine, direction):
    import math

    m = configured_machine
    a = m.config.aspiration
    a.vial_cone_height_mm = 0
    a.vial_volume_below_reference_ul = 1000
    a.z_up_direction = direction
    a.vial_bottom_mm = 50
    vessel = m.inventory.get("source")
    vessel.quantity_ul = 3000
    area = math.pi * (a.vial_internal_diameter_mm / 2) ** 2
    assert m._geometry(vessel).liquid_height_mm(1000) == 0
    assert m._geometry(vessel).liquid_height_mm(3000) == pytest.approx(2000 / area)
    expected = a.vial_bottom_mm + direction * (2000 / area - 1)
    assert m.aspiration_z(vessel, PipetteId.P1) == pytest.approx(expected)


@pytest.mark.parametrize("quantity,requested_ul,buffer", [(1100, 101, 0), (1100, 100, 1), (1000, 1, 0)])
def test_reserve_is_preserved_before_motion(configured_machine, quantity, requested_ul, buffer):
    m = configured_machine
    m.config.aspiration.vial_cone_height_mm = 0
    m.config.aspiration.vial_volume_below_reference_ul = 1000
    m.inventory.get("source").quantity_ul = quantity
    before = list(m.grbl.commands)
    with pytest.raises(ValidationError, match="reserve"):
        m.draw("source", requested_ul, PipetteId.P1, buffer_percent=buffer)
    assert m.grbl.commands == before


def test_reserve_requires_excluded_cone_and_explicit_calibration(configured_machine):
    m = configured_machine
    with pytest.raises(ValidationError, match="Exclude the cone"):
        m.record_calibration("aspiration.vial_volume_below_reference_ul", 1000, True)
    m.config.aspiration.vial_volume_below_reference_ul = None
    with pytest.raises(MissingCalibration):
        m.aspiration_z(m.inventory.get("source"), PipetteId.P1)

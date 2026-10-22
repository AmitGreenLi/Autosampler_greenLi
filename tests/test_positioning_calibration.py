import pytest

from autosampler.config import AspirationConfig, PipetteConfig, TransferConfig
from autosampler.errors import MissingCalibration, RecoveryRequired
from autosampler.models import PipetteId
from autosampler.motion.carousel import degrees_to_steps


def test_positioning_needs_no_liquid_or_pipette_stroke_calibration(configured_machine):
    m = configured_machine
    m.config.aspiration = AspirationConfig()
    m.config.transfer = TransferConfig()
    m.config.pipette_1 = PipetteConfig("Pipette 1", "A", 1600, 200)
    m.config.pipette_2 = PipetteConfig("Pipette 2", "B", 1600, 10000)
    m.config.pipette_2_positions_deg.clear()
    m.disconnect_scale()
    before = len(m.grbl.commands)
    assert m.go_to_spot("source", PipetteId.P1) == degrees_to_steps(0)
    assert m.grbl.commands[before:] == ["G90 G0 Z0 F100", "G90 G0 X0"]
    with pytest.raises(MissingCalibration):
        m.draw("source", 100, PipetteId.P1)


@pytest.mark.parametrize("missing", ["safe", "max", "speed", "angle", "range"])
def test_positioning_prerequisites_fail_before_motion(configured_machine, missing):
    m = configured_machine
    if missing == "safe":
        m.config.z.safe_position_mm = None
    elif missing == "max":
        m.config.z.max_travel_mm = None
    elif missing == "speed":
        m.config.z.motion_speed_mm_min = None
    elif missing == "range":
        m.config.z.safe_position_mm = 200
    else:
        m.config.pipette_1_positions_deg["source"] = None
    before = list(m.grbl.commands)
    with pytest.raises(MissingCalibration):
        m.go_to_spot("source", PipetteId.P1)
    assert m.grbl.commands == before


def test_positioning_still_requires_home_after_abort(configured_machine):
    m = configured_machine
    m.abort()
    before = list(m.grbl.commands)
    with pytest.raises(RecoveryRequired):
        m.go_to_spot("source", PipetteId.P1)
    assert m.grbl.commands == before

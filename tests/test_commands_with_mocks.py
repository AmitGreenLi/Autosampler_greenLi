import pytest

from autosampler.errors import MissingCalibration, ValidationError
from autosampler.models import PipetteId, ScaleReading


def test_go_to_spot_uses_absolute_integer_target(configured_machine):
    target = configured_machine.go_to_spot("dest", PipetteId.P1)
    assert target == 156
    assert "G90 G0 X156" in configured_machine.grbl.commands


def test_uncalibrated_location_fails_clearly(configured_machine):
    with pytest.raises(MissingCalibration, match="has not been calibrated"):
        configured_machine.go_to_spot("unknown", PipetteId.P1)


def test_draw_uses_scale_delta_and_updates_source(configured_machine):
    configured_machine.scale.queue(ScaleReading(10.0, True), ScaleReading(9.9, True))
    actual = configured_machine.draw("source", 100, PipetteId.P1)
    assert actual == pytest.approx(100)
    assert configured_machine.inventory.get("source").quantity_ul == pytest.approx(9900)
    assert configured_machine.state.pipette_contents_ul["P1"] == pytest.approx(100)


def test_draw_correction_loop_corrects_underdraw(configured_machine):
    configured_machine.scale.queue(
        ScaleReading(10.0, True),
        ScaleReading(9.95, True),  # 50 µL: under by 50
        ScaleReading(9.9, True),   # 100 µL: converged
    )
    actual = configured_machine.draw("source", 100, PipetteId.P1)
    assert actual == pytest.approx(100)
    # Two positive aspiration strokes occurred.
    contact_moves = [c for c in configured_machine.grbl.commands if c == "G90 G0 A1"]
    assert len(contact_moves) == 2


def test_draw_correction_loop_returns_overdraw_to_source(configured_machine):
    configured_machine.scale.queue(
        ScaleReading(10.0, True),
        ScaleReading(9.89, True),  # 110 µL: over by 10
        ScaleReading(9.9, True),   # 100 µL after correction
    )
    actual = configured_machine.draw("source", 100, PipetteId.P1)
    assert actual == pytest.approx(100)


def test_dispense_updates_destination_and_always_dumps_remainder(configured_machine):
    configured_machine.state.pipette_contents_ul["P1"] = 120
    configured_machine.dispense("dest", 100, PipetteId.P1)
    assert configured_machine.inventory.get("dest").quantity_ul == 100
    assert configured_machine.inventory.get("waste").quantity_ul is None
    assert configured_machine.state.pipette_contents_ul["P1"] == 0
    assert configured_machine.state.vessel_under_pipette == "waste"


def test_clean_returns_liquid_to_cleaning_not_waste(configured_machine):
    configured_machine.scale.queue(ScaleReading(50.0, True), ScaleReading(49.8, True))
    configured_machine.clean(PipetteId.P1, cycles=1)
    assert configured_machine.inventory.get("clean").quantity_ul == pytest.approx(50_000)
    assert configured_machine.inventory.get("waste").quantity_ul is None


def test_transfer_sequence_with_mock_measurement(configured_machine):
    configured_machine.scale.queue(ScaleReading(10.0, True), ScaleReading(9.9, True))
    plan = configured_machine.transfer("source", "dest", 100)
    assert plan.cycles_ul == (100.0,)
    assert configured_machine.inventory.get("source").quantity_ul == pytest.approx(9900)
    assert configured_machine.inventory.get("dest").quantity_ul == 100


def test_multicycle_requires_confirmation(configured_machine):
    with pytest.raises(ValidationError, match="requires operator confirmation"):
        configured_machine.transfer("source", "dest", 201)

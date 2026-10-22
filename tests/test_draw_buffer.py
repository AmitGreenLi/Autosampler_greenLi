import pytest

from autosampler.errors import ValidationError
from autosampler.models import PipetteId, ScaleReading


@pytest.mark.parametrize("operation", ["draw", "transfer"])
def test_buffer_override_changes_actual_draw_without_changing_default(configured_machine, operation):
    m = configured_machine
    m.config.transfer.default_draw_buffer_percent = 5
    m.scale.queue(ScaleReading(10, True), ScaleReading(9.89, True))
    if operation == "draw":
        assert m.draw("source", 100, PipetteId.P1, buffer_percent=10) == pytest.approx(110)
    else:
        m.transfer("source", "dest", 100, buffer_percent=10)
        assert m.inventory.get("dest").quantity_ul == 100
        assert m.inventory.get("waste").quantity_ul is None
    assert m.inventory.get("source").quantity_ul == pytest.approx(9890)
    assert m.config.transfer.default_draw_buffer_percent == 5


@pytest.mark.parametrize("buffer", [-1, float("nan"), float("inf"), 101])
def test_invalid_or_overcapacity_buffer_fails_before_motion(configured_machine, buffer):
    m = configured_machine
    before = list(m.grbl.commands)
    with pytest.raises(ValidationError):
        m.draw("source", 100, PipetteId.P1, buffer_percent=buffer)
    assert m.grbl.commands == before

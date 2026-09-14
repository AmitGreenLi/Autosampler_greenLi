import pytest

from autosampler.motion.carousel import degrees_to_steps, normalize_steps, steps_to_degrees


def test_degree_to_integer_step_rounding():
    assert degrees_to_steps(1) == 5
    assert degrees_to_steps(15) == 78
    assert isinstance(degrees_to_steps(12.34), int)


def test_steps_round_trip_is_theoretical_actual():
    target = degrees_to_steps(37.5)
    assert steps_to_degrees(target) == pytest.approx(37.5, abs=0.1)


def test_reference_normalization():
    revolution = round(360 * (140 / 27))
    assert normalize_steps(revolution) == 0
    assert normalize_steps(revolution + 12) == 12


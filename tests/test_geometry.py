import math

import pytest

from autosampler.errors import ValidationError
from autosampler.vessels.geometry import ConicalTubeGeometry, CylinderGeometry, LookupGeometry, aspiration_height_mm


def test_cylinder_height_uses_ul_as_cubic_mm():
    geometry = CylinderGeometry(10.0, 10_000)
    assert geometry.liquid_height_mm(1000) == pytest.approx(1000 / (math.pi * 25))


def test_cone_and_cylinder_piecewise_continuity():
    geometry = ConicalTubeGeometry()
    at_boundary = geometry.liquid_height_mm(geometry.cone_volume_ul)
    above = geometry.liquid_height_mm(geometry.cone_volume_ul + 0.01)
    assert at_boundary == pytest.approx(geometry.cone_height_mm)
    assert above > at_boundary


def test_conical_low_volume_similarity():
    geometry = ConicalTubeGeometry()
    assert geometry.liquid_height_mm(geometry.cone_volume_ul / 8) == pytest.approx(geometry.cone_height_mm / 2)


def test_lookup_geometry_and_immersion():
    geometry = LookupGeometry(((0, 0), (100, 10), (200, 15)))
    assert geometry.liquid_height_mm(50) == 5
    assert aspiration_height_mm(geometry, 100, 2) == 8


def test_geometry_rejects_impossible_volume():
    with pytest.raises(ValidationError):
        CylinderGeometry(10, 100).liquid_height_mm(101)


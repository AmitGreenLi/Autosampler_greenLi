from __future__ import annotations

import math
from dataclasses import dataclass

from autosampler.errors import ValidationError


class VesselGeometry:
    def liquid_height_mm(self, volume_ul: float) -> float:
        raise NotImplementedError


@dataclass(frozen=True)
class CylinderGeometry(VesselGeometry):
    internal_diameter_mm: float
    capacity_ul: float

    def liquid_height_mm(self, volume_ul: float) -> float:
        _validate_volume(volume_ul, self.capacity_ul)
        if self.internal_diameter_mm <= 0:
            raise ValidationError("Cylinder diameter must be positive.")
        return volume_ul / (math.pi * (self.internal_diameter_mm / 2) ** 2)


@dataclass(frozen=True)
class ConicalTubeGeometry(VesselGeometry):
    top_internal_diameter_mm: float = 14.66
    cone_height_mm: float = 22.48
    total_height_mm: float = 118.8
    capacity_ul: float = 15_000.0

    @property
    def cross_section_mm2(self) -> float:
        return math.pi * (self.top_internal_diameter_mm / 2) ** 2

    @property
    def cone_volume_ul(self) -> float:
        return self.cross_section_mm2 * self.cone_height_mm / 3

    def liquid_height_mm(self, volume_ul: float) -> float:
        _validate_volume(volume_ul, self.capacity_ul)
        radius = self.top_internal_diameter_mm / 2
        if radius <= 0 or self.cone_height_mm <= 0 or self.total_height_mm <= self.cone_height_mm:
            raise ValidationError("Invalid conical-tube geometry.")
        if volume_ul <= self.cone_volume_ul:
            if volume_ul == 0:
                return 0.0
            return (3 * volume_ul * self.cone_height_mm**2 / (math.pi * radius**2)) ** (1 / 3)
        return self.cone_height_mm + (volume_ul - self.cone_volume_ul) / self.cross_section_mm2


@dataclass(frozen=True)
class LookupGeometry(VesselGeometry):
    """Piecewise-linear replacement for experimentally measured volume/height data."""
    points: tuple[tuple[float, float], ...]

    def liquid_height_mm(self, volume_ul: float) -> float:
        if not self.points or any(self.points[i][0] >= self.points[i + 1][0] for i in range(len(self.points) - 1)):
            raise ValidationError("Geometry lookup volumes must be strictly increasing.")
        _validate_volume(volume_ul, self.points[-1][0])
        if volume_ul <= self.points[0][0]:
            return self.points[0][1]
        for (v0, h0), (v1, h1) in zip(self.points, self.points[1:]):
            if volume_ul <= v1:
                fraction = (volume_ul - v0) / (v1 - v0)
                return h0 + fraction * (h1 - h0)
        return self.points[-1][1]


def aspiration_height_mm(geometry: VesselGeometry, volume_ul: float, immersion_depth_mm: float) -> float:
    if immersion_depth_mm < 0:
        raise ValidationError("Aspiration immersion depth cannot be negative.")
    return max(0.0, geometry.liquid_height_mm(volume_ul) - immersion_depth_mm)


def _validate_volume(volume_ul: float, capacity_ul: float) -> None:
    if capacity_ul <= 0:
        raise ValidationError("Vessel capacity must be positive.")
    if volume_ul < 0 or volume_ul > capacity_ul + 1e-9:
        raise ValidationError(f"Volume must be between 0 and {capacity_ul:g} µL.")


BEAKER_100ML_GEOMETRY = CylinderGeometry(48.2, 100_000.0)
VIAL_15ML_GEOMETRY = ConicalTubeGeometry()


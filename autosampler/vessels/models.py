from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

from autosampler.errors import ValidationError


class VesselCategory(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    WASTE = "waste"
    CLEANING = "cleaning"


class VesselType(str, Enum):
    VIAL_15ML = "vial_15ml"
    BEAKER_100ML = "beaker_100ml"

    @property
    def capacity_ul(self) -> float:
        return 15_000.0 if self is VesselType.VIAL_15ML else 100_000.0


@dataclass
class Vessel:
    location_id: str
    number: int
    name: str
    category: VesselCategory
    vessel_type: VesselType
    quantity_ul: float = 0.0
    density_g_ml: float = 1.0
    material_name: str = "Water"

    @property
    def capacity_ul(self) -> float:
        return self.vessel_type.capacity_ul

    def validate(self) -> None:
        if self.quantity_ul < 0:
            raise ValidationError(f"{self.name} cannot have a negative quantity.")
        if self.quantity_ul > self.capacity_ul + 1e-9:
            raise ValidationError(f"{self.name} exceeds its {self.capacity_ul:g} µL capacity.")
        if self.density_g_ml <= 0:
            raise ValidationError(f"{self.name} density must be greater than zero.")

    def to_dict(self) -> dict:
        result = asdict(self)
        result["category"] = self.category.value
        result["vessel_type"] = self.vessel_type.value
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "Vessel":
        item = dict(data)
        item["category"] = VesselCategory(item["category"])
        item["vessel_type"] = VesselType(item["vessel_type"])
        vessel = cls(**item)
        vessel.validate()
        return vessel


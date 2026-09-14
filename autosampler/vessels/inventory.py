from __future__ import annotations

from autosampler.errors import ValidationError
from autosampler.vessels.models import Vessel, VesselCategory


class Inventory:
    def __init__(self, vessels=()):
        self.vessels: dict[str, Vessel] = {v.location_id: v for v in vessels}

    def add(self, vessel: Vessel) -> None:
        vessel.validate()
        self.vessels[vessel.location_id] = vessel

    def get(self, location_id: str) -> Vessel:
        try:
            return self.vessels[location_id]
        except KeyError as exc:
            raise ValidationError(f"Vessel {location_id!r} is not configured.") from exc

    def by_category(self, category: VesselCategory) -> list[Vessel]:
        return [v for v in self.vessels.values() if v.category is category]

    def require_special(self, category: VesselCategory) -> Vessel:
        matches = self.by_category(category)
        if not matches:
            raise ValidationError(f"A {category.value.title()} vessel has not been configured.")
        if len(matches) > 1 and category in (VesselCategory.WASTE, VesselCategory.CLEANING):
            raise ValidationError(f"Multiple {category.value.title()} vessels are configured.")
        return matches[0]

    def validate_transfer(self, source_id: str, destination_id: str, amount_ul: float) -> tuple[Vessel, Vessel]:
        if amount_ul <= 0:
            raise ValidationError("Transfer amount must be greater than zero.")
        if source_id == destination_id:
            raise ValidationError("Source and destination must be different.")
        source, destination = self.get(source_id), self.get(destination_id)
        source.validate()
        destination.validate()
        if source.quantity_ul + 1e-9 < amount_ul:
            raise ValidationError(f"{source.name} contains insufficient liquid.")
        if destination.quantity_ul + amount_ul > destination.capacity_ul + 1e-9:
            raise ValidationError(f"Transfer would overfill {destination.name}.")
        return source, destination

    def remove(self, location_id: str, amount_ul: float) -> None:
        vessel = self.get(location_id)
        if amount_ul < 0 or amount_ul > vessel.quantity_ul + 1e-9:
            raise ValidationError(f"Cannot remove {amount_ul:g} µL from {vessel.name}.")
        vessel.quantity_ul = max(0.0, vessel.quantity_ul - amount_ul)

    def add_volume(self, location_id: str, amount_ul: float) -> None:
        vessel = self.get(location_id)
        if amount_ul < 0 or vessel.quantity_ul + amount_ul > vessel.capacity_ul + 1e-9:
            raise ValidationError(f"Cannot add {amount_ul:g} µL to {vessel.name}.")
        vessel.quantity_ul += amount_ul

    def to_list(self) -> list[dict]:
        return [v.to_dict() for v in sorted(self.vessels.values(), key=lambda x: x.number)]

    @classmethod
    def from_list(cls, data: list[dict]) -> "Inventory":
        return cls(Vessel.from_dict(item) for item in data)


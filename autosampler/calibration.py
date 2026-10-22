"""Validated, motion-free calibration records shared by the GUI and machine."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from autosampler.errors import ValidationError


@dataclass(frozen=True)
class Measurement:
    key: str
    label: str
    minimum: float | None = None
    positive: bool = False
    integer: bool = False

    def validate(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValidationError(f"{self.label}: enter a finite number.")
        if self.positive and value <= 0 or self.minimum is not None and value < self.minimum:
            raise ValidationError(f"{self.label}: value is outside the safe range.")
        if self.integer and int(value) != value:
            raise ValidationError(f"{self.label}: enter a whole number.")
        return int(value) if self.integer else float(value)


MEASUREMENTS = (
    Measurement("z.max_travel_mm", "Z maximum distance from home (mm)", positive=True),
    Measurement("z.safe_position_mm", "Z collision-free carousel clearance position (mm)"),
    Measurement("z.dispense_position_mm", "Z dispense position (mm)"),
    Measurement("z.aspiration_machine_offset_mm", "Z vessel-bottom machine offset (mm)"),
    Measurement("z.motion_speed_mm_min", "Z verified motion speed (mm/min)", positive=True),
    Measurement("z.acceleration_mm_s2", "Z verified firmware acceleration (mm/s²)", positive=True),
) + tuple(
    Measurement(f"pipette_{number}.{field}", f"{axis}: {label}", positive=positive)
    for number, axis in ((1, "A / P1"), (2, "B / P2"))
    for field, label, positive in (
        ("contact_position_mm", "contact position from home (mm)", False),
        ("max_travel_mm", "maximum safe coordinate / stroke limit (mm)", True),
        ("volume_per_mm_ul", "measured volume per travel (µL/mm)", True),
        ("speed_mm_min", "verified speed (mm/min)", True),
        ("acceleration_mm_s2", "verified firmware acceleration (mm/s²)", True),
    )
) + (
    Measurement("transfer.default_draw_buffer_percent", "Draw buffer (%)", minimum=0),
    Measurement("transfer.tolerance_ul", "Transfer tolerance (µL)", positive=True),
    Measurement("transfer.max_correction_attempts", "Maximum correction attempts", positive=True, integer=True),
    Measurement("transfer.scale_stabilization_time_s", "Scale stabilization timeout (s)", minimum=0),
    Measurement("transfer.minimum_correction_volume_ul", "Minimum correction (µL)", positive=True),
    Measurement("transfer.aspiration_immersion_depth_mm", "Aspiration immersion (mm)", minimum=0),
    Measurement("transfer.z_liquid_clearance_mm", "Liquid clearance (mm)", minimum=0),
)


def measurements(locations):
    return MEASUREMENTS + tuple(
        Measurement(f"pipette_{p}_positions_deg.{location}", f"Carousel P{p} / {location}: aligned angle from X home (degrees)")
        for p in (1, 2) for location in sorted(locations)
    )


def get_value(config, key):
    group, field = key.split(".", 1)
    target = getattr(config, group)
    return target.get(field) if isinstance(target, dict) else getattr(target, field)


def set_value(config, key, value):
    group, field = key.split(".", 1)
    target = getattr(config, group)
    if isinstance(target, dict):
        target[field] = value
    else:
        setattr(target, field, value)


def geometry_errors(config):
    errors = []
    z = config.z
    if z.max_travel_mm is not None:
        for field in ("safe_position_mm", "dispense_position_mm", "aspiration_machine_offset_mm"):
            value = getattr(z, field)
            if value is not None and abs(value - z.home_position_mm) > z.max_travel_mm:
                errors.append(f"Z {field} exceeds maximum travel")
    for p in (config.pipette_1, config.pipette_2):
        if p.contact_position_mm is not None and p.max_travel_mm is not None:
            if not p.home_position_mm <= p.contact_position_mm <= p.max_travel_mm:
                errors.append(f"{p.name} contact is outside safe travel")
            if p.volume_per_mm_ul and p.contact_position_mm + p.max_volume_ul / p.volume_per_mm_ul > p.max_travel_mm:
                errors.append(f"{p.name} full-volume stroke exceeds safe travel")
    return errors


def calibration_issues(config, locations):
    issues = []
    for measurement in measurements(locations):
        value = get_value(config, measurement.key)
        if value is None:
            issues.append(measurement.label)
        else:
            try:
                measurement.validate(value)
            except ValidationError as exc:
                issues.append(str(exc))
    return issues + geometry_errors(config)


def save_calibration(config, path, locations):
    path = Path(path)
    values = {m.key: get_value(config, m.key) for m in measurements(locations)}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"version": 1, "values": values}, allow_nan=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_calibration(config, path, locations):
    """Validate the entire file before publishing any values; never restore position trust."""
    from copy import deepcopy

    if not Path(path).exists():
        return
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("values"), dict):
        raise ValidationError("Unsupported calibration file.")
    allowed = {m.key: m for m in measurements(locations)}
    candidate = deepcopy(config)
    for key, value in data["values"].items():
        if key not in allowed:
            # Vessel locations can change between sessions; never apply unknown entries.
            continue
        if value is not None:
            set_value(candidate, key, allowed[key].validate(value))
    errors = geometry_errors(candidate)
    if errors:
        raise ValidationError("; ".join(errors))
    for key in allowed:
        set_value(config, key, get_value(candidate, key))

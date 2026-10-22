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
    choices: tuple = ()

    def validate(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValidationError(f"{self.label}: enter a finite number.")
        if self.positive and value <= 0 or self.minimum is not None and value < self.minimum:
            raise ValidationError(f"{self.label}: value is outside the safe range.")
        if self.integer and int(value) != value:
            raise ValidationError(f"{self.label}: enter a whole number.")
        if self.choices and value not in self.choices:
            raise ValidationError(f"{self.label}: choose one of {self.choices}.")
        if self.key in ("carousel_overshoot_deg", "carousel_secondary_overshoot_deg", "carousel_jostle_deg"):
            from autosampler.config import CAROUSEL_STEPS_PER_DEGREE
            steps = value * CAROUSEL_STEPS_PER_DEGREE
            if not math.isfinite(steps) or value > 0 and round(steps) == 0:
                raise ValidationError("Carousel overshoot must be zero or at least one achievable motor step.")
        return int(value) if self.integer else float(value)


MEASUREMENTS = (
    Measurement("carousel_overshoot_deg", "Carousel A: primary overshoot beyond destination in travel direction (degrees)", minimum=0),
    Measurement("carousel_secondary_overshoot_deg", "Carousel B: secondary overshoot past destination on reverse leg (degrees; 0 disables)", minimum=0),
    Measurement("carousel_stability_timeout_s", "Carousel X: post-position scale stability timeout (seconds; 0 disables)", minimum=0),
    Measurement("carousel_jostle_deg", "Carousel Y: corrective jostle angle (degrees; 0 disables jostle)", minimum=0),
    Measurement("z.max_travel_mm", "Z maximum distance from home (mm)", positive=True),
    Measurement("z.safe_position_mm", "Z collision-free carousel clearance position (mm)"),
    Measurement("z.dispense_position_mm", "Z dispense position (mm)"),
    Measurement("aspiration.z_up_direction", "Z direction moving the tip upward: +1 or -1", integer=True, choices=(-1, 1)),
    Measurement("aspiration.vial_bottom_mm", "Vial: shared Z coordinate at height reference (mm)"),
    Measurement("aspiration.beaker_bottom_mm", "Beaker: shared Z coordinate at internal bottom reference (mm)"),
    Measurement("aspiration.vial_internal_diameter_mm", "Vial: internal cylindrical-section diameter (mm)", positive=True),
    Measurement("aspiration.vial_cone_height_mm", "Vial: modeled cone height (mm; 0 to exclude tip section)", minimum=0),
    Measurement("aspiration.vial_volume_below_reference_ul", "Vial: known reserve volume below height reference (µL; 1000 for 1 mL mark)", minimum=0),
    Measurement("aspiration.vial_internal_height_mm", "Vial: internal height from reference to rim (mm)", positive=True),
    Measurement("aspiration.beaker_internal_diameter_mm", "Beaker: internal diameter (mm)", positive=True),
    Measurement("aspiration.beaker_internal_height_mm", "Beaker: internal bottom-to-rim height (mm)", positive=True),
    Measurement("aspiration.bottom_clearance_mm", "Aspiration: minimum tip clearance above internal bottom (mm)", positive=True),
) + tuple(
    Measurement(f"pipette_{number}.{field}", f"{axis}: {label}", positive=positive)
    for number, axis in ((1, "A / P1"), (2, "B / P2"))
    for field, label, positive in (
        ("contact_position_mm", "contact position from home (mm)", False),
        ("max_travel_mm", "maximum safe distance from home (mm; positive)", True),
        ("volume_per_step_ul", "measured volume per driver step (µL/step)", True),
    )
) + (
    Measurement("transfer.default_draw_buffer_percent", "Draw buffer (%)", minimum=0),
    Measurement("transfer.tolerance_ul", "Transfer tolerance (µL)", positive=True),
    Measurement("transfer.max_correction_attempts", "Maximum correction attempts", positive=True, integer=True),
    Measurement("transfer.scale_stabilization_time_s", "Scale stabilization timeout (s)", minimum=0),
    Measurement("transfer.minimum_correction_volume_ul", "Minimum correction (µL)", positive=True),
    Measurement("transfer.aspiration_immersion_depth_mm", "Aspiration: tip depth below liquid surface (mm)", positive=True),
)

FIRMWARE_SETTINGS = {
    "z.motion_speed_mm_min": 112,
    "z.acceleration_mm_s2": 122,
    "pipette_1.speed_mm_min": 113,
    "pipette_1.acceleration_mm_s2": 123,
    "pipette_2.speed_mm_min": 114,
    "pipette_2.acceleration_mm_s2": 124,
}
Z_POSITION_KEYS = ("z.safe_position_mm", "z.dispense_position_mm", "aspiration.vial_bottom_mm", "aspiration.beaker_bottom_mm")


def normalize_z_entry(config, key, value):
    if key in Z_POSITION_KEYS and config.z.work_direction == -1 and value > config.z.home_position_mm:
        # Accept the operator's distance-from-home convention explicitly.
        return config.z.home_position_mm - abs(value - config.z.home_position_mm)
    for number in (1, 2):
        p = getattr(config, f"pipette_{number}")
        if key == f"pipette_{number}.contact_position_mm" and p.work_direction == -1 and value > p.home_position_mm:
            return p.home_position_mm - abs(value - p.home_position_mm)
    return value
FIRMWARE_MEASUREMENTS = tuple(
    Measurement(key, f"GRBL ${number}: {key}", positive=True)
    for key, number in FIRMWARE_SETTINGS.items()
)

CAROUSEL_OFFSETS_DEG = {
    **{f"V{number:02d}": -(24 - number) * 12.0 for number in range(1, 25)},
    "B2": 29.0,
    "B1": 55.0,
}


def carousel_angle(config, location, pipette_number):
    mapping = getattr(config, f"pipette_{pipette_number}_positions_deg")
    if location not in CAROUSEL_OFFSETS_DEG:
        return mapping.get(location)
    anchor = mapping.get("V24")
    if anchor is None:
        return None
    return (anchor + CAROUSEL_OFFSETS_DEG[location]) % 360.0


def measurements(locations):
    return MEASUREMENTS + tuple(
        Measurement(f"pipette_{p}_positions_deg.{location}", f"Carousel P{p} / {location}: aligned angle from X home (degrees)")
        for p in (1, 2)
        for location in ("V24", *sorted(set(locations) - CAROUSEL_OFFSETS_DEG.keys()))
    )


def aspiration_measurement_help(key):
    if key in ("carousel_stability_timeout_s", "carousel_jostle_deg"):
        return (
            "After all positioning stages finish, wait up to X seconds for a fresh balance ST reading. "
            "If valid readings remain unstable for the full deadline, move Y degrees opposite the last "
            "completed carousel segment, wait, then return exactly to the intended destination and wait again. "
            "At most one jostle is performed, with no second stability wait. X=0 disables this check; "
            "Y=0 disables corrective movement. Scale unavailability skips the check/jostle and warns once "
            "until scale communication recovers. With no carousel displacement there is no direction to reverse. "
            "The A/B calibration sweep uses its own stability scoring without corrective jostles."
        )
    if key in ("carousel_overshoot_deg", "carousel_secondary_overshoot_deg"):
        return (
            "A and B are independent non-negative angles. For travel direction d and target T, "
            "the stages are T+d×A, T−d×B, then T. Each stage must finish before the next starts. "
            "B=0 preserves the primary overshoot and direct return; A=B=0 uses one move. "
            "A=0 with B>0 first reaches T, then crosses to T−d×B and returns. "
            "Angles are rounded to whole motor steps. No wrapping is applied to either overshoot. "
            "The extra path must be mechanically clear and permitted by controller limits. "
            "A destination already reached needs no overshoot."
        )
    if key and key.startswith("pipette_") and key.endswith((".contact_position_mm", ".max_travel_mm")):
        return (
            "A/B home is 0 and working coordinates are negative. Contact is the signed point where the actuator "
            "engages the pipette; a positive contact distance is converted to negative on recording. "
            "Maximum travel is a POSITIVE total distance from home, including the approach to contact. "
            "A working stroke proceeds from contact toward more-negative coordinates and then returns home. "
            "Dump/empty moves to the negative maximum-travel endpoint. Verify this endpoint is safe for repeated emptying."
        )
    if key in ("z.safe_position_mm", "z.dispense_position_mm"):
        return (
            "Z home is 0 and the working area is negative. Enter a signed coordinate, for example −125 mm. "
            "A positive distance from home is converted to its negative Z coordinate when recorded. "
            "Maximum travel remains a positive distance. Clearance must clear every vessel before carousel rotation."
        )
    if key and key.endswith(".volume_per_step_ul"):
        return (
            "Measure the liquid volume moved by a known number of working driver steps after contact. "
            "Enter measured µL divided by step count. A step includes the configured microstepping; "
            "use the same driver-step units as manual A/B jogging. The program calculates "
            "round(requested µL / µL per step), then converts that whole-step stroke to GRBL mm "
            "using this axis's configured steps/mm. Positive requests smaller than half a step are rejected. "
            "Changing microstepping or steps/mm requires checking this calibration."
        )
    if key and key.endswith("positions_deg.V24"):
        return (
            "Align V24 with the selected pipette and enter its carousel angle from X home. "
            "All vial and beaker angles are derived from this reference: Vnn = V24 − (24 − nn) × 12°, "
            "B2 = V24 + 29°, B1 = V24 + 55°, wrapped into 0–360°. "
            "P1 and P2 need separate carousel references because their horizontal positions differ. "
            "A derived position is usable only when its vessel row is fully configured."
        )
    if key == "aspiration.vial_volume_below_reference_ul":
        return (
            "Enter the known liquid volume below the vial's Z height reference. For a reference at the "
            "1 mL mark enter 1000 µL and set modeled cone height to 0. The program subtracts this reserve "
            "from total vessel quantity before calculating cylindrical liquid height; it never estimates "
            "the excluded tip shape. Draws cannot consume this reserve. Initial quantity remains TOTAL "
            "liquid volume including the reserve. Enter 0 when the reference is the actual internal bottom."
        )
    if key == "aspiration.vial_bottom_mm":
        return (
            "For this machine use a negative Z coordinate; a positive distance is converted on recording. "
            "Enter the shared P1/P2 machine Z coordinate where the tips reach the chosen vial height reference. "
            "When excluding the tip section, use the start of the cylinder (for example the 1 mL mark), "
            "and enter its known reserve volume separately. Measure reference-to-rim height from that same plane. "
            "Minimum bottom clearance is then measured ABOVE this reference plane, not the physical apex."
        )
    if key == "transfer.aspiration_immersion_depth_mm":
        return (
            "Enter a small positive depth that keeps the pipette opening submerged during aspiration. "
            "The program estimates liquid surface height from the vessel's recorded quantity and calibrated "
            "internal dimensions; no separate liquid-top coordinate is required. "
            "Tip height above bottom = calculated liquid height − this depth. "
            "Machine Z = vessel-bottom Z + upward direction × tip height. "
            "The same depth applies to P1 and P2 and to aspiration during Draw, Transfer, and Clean. "
            "The target is calculated before each draw; the program does not sense or continuously follow "
            "the falling liquid surface during a stroke. Choose a depth validated for the intended draw volume. "
            "A target below the minimum bottom clearance is rejected. "
            "Bottom-to-rim height describes the vessel, not its current liquid level."
        )
    if not key or not key.startswith("aspiration."):
        return ""
    field = key.split(".", 1)[1]
    if field.endswith("_bottom_mm"):
        return (
            "P1 and P2 tips share the same height. Measure the signed Z coordinate where either tip would meet the internal "
            "bottom of this vessel type in its installed holder. Infer it from a measured safe gap; "
            "do not drive the tip into the bottom. This includes holder height and tip length. "
            "Used as: aspiration Z = bottom Z + upward direction × (liquid height − immersion). "
            "All slots of this vessel type must share this bottom reference; recalibrate after changing tips or holders."
        )
    if field == "z_up_direction":
        return (
            "Enter +1 if increasing machine Z raises the tip relative to the vessel bottom; "
            "enter -1 if it lowers it. This determines whether calculated liquid height is added "
            "to or subtracted from the bottom coordinate."
        )
    if field.endswith("internal_diameter_mm"):
        return (
            "Measure the inside diameter of the straight cylindrical section, excluding wall thickness. "
            "The code uses its cross-sectional area to convert recorded liquid volume into liquid height. "
            "The beaker model is a flat-bottom cylinder; the vial model is a cylinder above a conical bottom."
        )
    if field == "vial_cone_height_mm":
        return (
            "Measure the internal vertical distance from the bottom apex to the start of the straight "
            "cylindrical section only if modeling the cone. Enter 0 to exclude the tip section and model "
            "only the cylinder; set the known volume below the reference separately. A positive cone height "
            "requires zero reserve volume so that the tip volume is not counted twice."
        )
    if field.endswith("internal_height_mm"):
        return (
            "Measure internal vertical height from the calibrated height reference to the rim. "
            "For a vial excluding its tip section, start at the chosen mark, not the bottom apex. "
            "Calculated liquid levels above this height are rejected before movement. "
            "This is not the external vessel height."
        )
    return (
        "Enter the minimum positive tip-to-bottom gap required for safe aspiration. "
        "The code rejects an aspiration depth that would leave less than this clearance, "
        "rather than moving the tip to the bottom. Immersion below the liquid surface is configured separately."
    )


def get_value(config, key):
    if "." not in key:
        return getattr(config, key)
    group, field = key.split(".", 1)
    target = getattr(config, group)
    return target.get(field) if isinstance(target, dict) else getattr(target, field)


def set_value(config, key, value):
    if "." not in key:
        setattr(config, key, value)
        return
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
        for field in ("safe_position_mm", "dispense_position_mm"):
            value = getattr(z, field)
            if value is not None and z.target_error(value):
                errors.append(f"Z {field}: {z.target_error(value)}")
        for field in ("vial_bottom_mm", "beaker_bottom_mm"):
            value = getattr(config.aspiration, field)
            if value is not None and z.target_error(value):
                errors.append(f"{field}: {z.target_error(value)}")
    a = config.aspiration
    if z.work_direction == -1 and a.z_up_direction is not None and a.z_up_direction != 1:
        errors.append("With negative Z working travel from home, height above the vessel reference must use Z upward direction +1")
    reserve = a.vial_volume_below_reference_ul
    if reserve is not None:
        if reserve >= 15000:
            errors.append("Vial reserve must be less than 15000 µL")
        if reserve > 0 and a.vial_cone_height_mm is not None and a.vial_cone_height_mm != 0:
            errors.append("Exclude the cone (height 0) when using a known vial reserve volume")
    if a.vial_cone_height_mm is not None and a.vial_internal_height_mm is not None:
        if a.vial_cone_height_mm >= a.vial_internal_height_mm:
            errors.append("Vial cone height must be less than internal height")
    for height in (a.vial_internal_height_mm, a.beaker_internal_height_mm):
        if height is not None and a.bottom_clearance_mm is not None and a.bottom_clearance_mm >= height:
            errors.append("Bottom clearance must be less than vessel internal height")
    for p in (config.pipette_1, config.pipette_2):
        if p.contact_position_mm is not None and p.max_travel_mm is not None:
            if p.target_error(p.contact_position_mm):
                errors.append(f"{p.name} contact: {p.target_error(p.contact_position_mm)}")
            if p.volume_per_step_ul and p.steps_per_mm > 0 and (
                round((p.contact_position_mm - p.home_position_mm) * p.work_direction * p.steps_per_mm)
                + round(p.max_volume_ul / p.volume_per_step_ul)
            ) / p.steps_per_mm > p.max_travel_mm:
                errors.append(f"{p.name} full-volume stroke exceeds safe travel")
    return errors


def calibration_issues(config, locations):
    issues = []
    for measurement in measurements(locations) + FIRMWARE_MEASUREMENTS:
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
    values = {m.key: get_value(config, m.key) for m in measurements(locations) + FIRMWARE_MEASUREMENTS}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"version": 1, "z_work_direction": config.z.work_direction,
        "pipette_work_directions": {p.axis: p.work_direction for p in (config.pipette_1, config.pipette_2)},
        "values": values}, allow_nan=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_calibration(config, path, locations):
    """Validate the entire file before publishing any values; never restore position trust."""
    from copy import deepcopy

    if not Path(path).exists():
        return
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("values"), dict):
        raise ValidationError("Unsupported calibration file.")
    allowed = {m.key: m for m in measurements(locations) + FIRMWARE_MEASUREMENTS}
    candidate = deepcopy(config)
    if "carousel_secondary_overshoot_deg" not in data["values"]:
        candidate.carousel_secondary_overshoot_deg = 0.0
    pipette_directions = data.get("pipette_work_directions", {})
    if not isinstance(pipette_directions, dict):
        raise ValidationError("Invalid pipette work directions")
    for number in (1, 2):
        p = getattr(candidate, f"pipette_{number}")
        p.work_direction = pipette_directions.get(p.axis, p.work_direction)
        if p.work_direction not in (-1, 1):
            raise ValidationError(f"Invalid {p.axis} work direction")
        contact_key = f"pipette_{number}.contact_position_mm"
        value = data["values"].get(contact_key)
        if p.axis not in pipette_directions and value is not None:
            value = allowed[contact_key].validate(value)
            data["values"][contact_key] = normalize_z_entry(candidate, contact_key, value)
    direction = data.get("z_work_direction", config.z.work_direction)
    if direction not in (-1, 1):
        raise ValidationError("Invalid Z work direction in calibration file.")
    candidate.z.work_direction = direction
    if "z_work_direction" not in data and direction == -1:
        # Earlier UI accepted distances that operators entered as positive Z.
        # Their saved bottom references also used the opposite height sign.
        old_bottoms = [data["values"].get(key) for key in Z_POSITION_KEYS[2:]]
        if any(isinstance(v, (int, float)) and v > 0 for v in old_bottoms):
            data["values"]["aspiration.z_up_direction"] = 1
        for key in Z_POSITION_KEYS:
            value = data["values"].get(key)
            if value is not None:
                value = allowed[key].validate(value)
                data["values"][key] = normalize_z_entry(candidate, key, value)
    for number in (1, 2):
        new_key = f"pipette_{number}.volume_per_step_ul"
        old_key = f"pipette_{number}.volume_per_mm_ul"
        old_value = data["values"].get(old_key)
        if new_key not in data["values"] and old_value is not None:
            old_value = Measurement(old_key, "Legacy volume per mm", positive=True).validate(old_value)
            steps_per_mm = getattr(config, f"pipette_{number}").steps_per_mm
            Measurement("steps_per_mm", "Driver steps/mm", positive=True).validate(steps_per_mm)
            data["values"][new_key] = old_value / steps_per_mm
    # Preserve only agreeing, valid legacy measurements. Conflicting or incomplete
    # pairs leave the shared measurement unset for operator recalibration.
    for family in ("vial", "beaker"):
        shared = f"aspiration.{family}_bottom_mm"
        if shared not in data["values"]:
            legacy = [data["values"].get(f"aspiration.{family}_p{p}_bottom_mm") for p in (1, 2)]
            for value in legacy:
                if value is not None:
                    allowed[shared].validate(value)
            data["values"][shared] = legacy[0] if legacy[0] == legacy[1] else None
            set_value(candidate, shared, None)
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
    config.z.work_direction = candidate.z.work_direction
    config.pipette_1.work_direction = candidate.pipette_1.work_direction
    config.pipette_2.work_direction = candidate.pipette_2.work_direction

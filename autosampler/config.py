from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

AXIS_CAROUSEL = "X"
AXIS_Z = "Z"
AXIS_PIPETTE_1 = "A"
AXIS_PIPETTE_2 = "B"

CAROUSEL_MOTOR_STEPS_PER_REV = 200
CAROUSEL_DRIVER_GEAR_TEETH = 12
CAROUSEL_DRIVEN_GEAR_TEETH = 112
CAROUSEL_STEPS_PER_DEGREE = 140 / 27
Z_STEPS_PER_MM = 80.0
PIPETTE_MICROSTEPS = 16
PIPETTE_MOTOR_FULL_STEPS_PER_REV = 200
PIPETTE_TRAVEL_MM_PER_REV = 2.0
PIPETTE_1_FULL_STEPS_PER_MM = (
    PIPETTE_MOTOR_FULL_STEPS_PER_REV / PIPETTE_TRAVEL_MM_PER_REV
)
PIPETTE_2_FULL_STEPS_PER_MM = (
    PIPETTE_MOTOR_FULL_STEPS_PER_REV / PIPETTE_TRAVEL_MM_PER_REV
)
PIPETTE_1_STEPS_PER_MM = PIPETTE_1_FULL_STEPS_PER_MM * PIPETTE_MICROSTEPS
PIPETTE_2_STEPS_PER_MM = PIPETTE_2_FULL_STEPS_PER_MM * PIPETTE_MICROSTEPS
VIAL_MAX_VOLUME_ML = 15.0
BEAKER_MAX_VOLUME_ML = 100.0


@dataclass
class SerialConfig:
    port: str | None
    baud_rate: int | None
    timeout_s: float = 2.0


@dataclass
class AandFzScaleConfig(SerialConfig):
    """A&D FZ-500i RS-232C defaults (balance function settings bp5/btpr/Crlf)."""

    data_bits: int = 7
    parity: str = "E"
    stop_bits: int = 1
    terminator: bytes = b"\r\n"
    query_command: bytes = b"Q"
    poll_interval_s: float = 0.2
    expected_unit: str = "g"


@dataclass
class ZConfig:
    work_direction: int = -1  # From home into this machine's usable Z travel.
    steps_per_mm: float = Z_STEPS_PER_MM
    home_position_mm: float = 0.0
    safe_position_mm: float | None = None
    max_travel_mm: float | None = None
    dispense_position_mm: float | None = None
    aspiration_machine_offset_mm: float | None = None
    motion_speed_mm_min: float | None = None
    acceleration_mm_s2: float | None = None

    def target_error(self, target):
        import math
        if target is None or not math.isfinite(target):
            return "Z target must be a finite signed coordinate"
        if self.max_travel_mm is None or not math.isfinite(self.max_travel_mm) or self.max_travel_mm <= 0:
            return "Z maximum travel must be a positive distance"
        if self.work_direction not in (-1, 1):
            return "Z work direction is invalid"
        distance = (target - self.home_position_mm) * self.work_direction
        if distance < -1e-9 or distance > self.max_travel_mm + 1e-9:
            endpoint = self.home_position_mm + self.work_direction * self.max_travel_mm
            return f"Z target {target:g} is outside the work envelope [{min(self.home_position_mm, endpoint):g}, {max(self.home_position_mm, endpoint):g}]"
        return None


@dataclass
class PipetteConfig:
    name: str
    axis: str
    steps_per_mm: float
    max_volume_ul: float
    home_position_mm: float = 0.0
    contact_position_mm: float | None = None
    max_travel_mm: float | None = None
    # Microlitres per commanded driver step (including configured microstepping).
    volume_per_step_ul: float | None = None
    speed_mm_min: float | None = None
    acceleration_mm_s2: float | None = None
    work_direction: int = -1

    @property
    def empty_position_mm(self):
        import math
        # Round inward so the endpoint cannot exceed the measured safe distance.
        return self.home_position_mm + self.work_direction * math.floor(self.max_travel_mm * self.steps_per_mm) / self.steps_per_mm

    def target_error(self, target):
        import math
        if target is None or not math.isfinite(target):
            return f"{self.axis} target must be finite"
        if self.max_travel_mm is None or not math.isfinite(self.max_travel_mm) or self.max_travel_mm <= 0:
            return f"{self.axis} maximum travel must be a positive distance"
        if self.work_direction not in (-1, 1):
            return f"{self.axis} work direction is invalid"
        distance = (target - self.home_position_mm) * self.work_direction
        if distance < -1e-9 or distance > self.max_travel_mm + 1e-9:
            end = self.home_position_mm + self.work_direction * self.max_travel_mm
            return f"{self.axis} target {target:g} is outside work envelope [{min(end, self.home_position_mm):g}, {max(end, self.home_position_mm):g}]"
        return None


@dataclass
class TransferConfig:
    p1_multi_threshold_ul: float = 1100.0
    default_draw_buffer_percent: float | None = None
    tolerance_ul: float | None = None
    max_correction_attempts: int | None = None
    scale_stabilization_time_s: float | None = None
    minimum_correction_volume_ul: float | None = None
    aspiration_immersion_depth_mm: float | None = None
    default_cleaning_cycles: int = 3


@dataclass
class AspirationConfig:
    # Physical height above the internal bottom maps to signed machine Z.
    z_up_direction: int | None = None
    vial_bottom_mm: float | None = None
    beaker_bottom_mm: float | None = None
    vial_internal_diameter_mm: float | None = None
    vial_cone_height_mm: float | None = None
    vial_volume_below_reference_ul: float | None = None
    vial_internal_height_mm: float | None = None
    beaker_internal_diameter_mm: float | None = None
    beaker_internal_height_mm: float | None = None
    bottom_clearance_mm: float | None = None


@dataclass
class MachineConfig:
    carousel_overshoot_deg: float = 0.0
    carousel_secondary_overshoot_deg: float = 0.0
    carousel_stability_timeout_s: float = 2.0
    carousel_jostle_deg: float = 1.0
    carousel_homing_search_deg_s: float = 30.0
    z_homing_search_rate: float = 1000.0  # GRBL coordinate units/min, matching $25.
    # Ports are selected by saved USB identity or the operator, never by OS order.
    grbl: SerialConfig = field(default_factory=lambda: SerialConfig(None, 115200))
    scale: AandFzScaleConfig = field(default_factory=lambda: AandFzScaleConfig(None, 2400))
    z: ZConfig = field(default_factory=ZConfig)
    pipette_1: PipetteConfig = field(
        default_factory=lambda: PipetteConfig(
            "Pipette 1", AXIS_PIPETTE_1, PIPETTE_1_STEPS_PER_MM, 200.0
        )
    )
    pipette_2: PipetteConfig = field(
        default_factory=lambda: PipetteConfig(
            "Pipette 2", AXIS_PIPETTE_2, PIPETTE_2_STEPS_PER_MM, 10_000.0
        )
    )
    transfer: TransferConfig = field(default_factory=TransferConfig)
    aspiration: AspirationConfig = field(default_factory=AspirationConfig)
    # Location -> calibrated carousel angle. Pipette offsets stay independent.
    pipette_1_positions_deg: dict[str, float | None] = field(default_factory=dict)
    pipette_2_positions_deg: dict[str, float | None] = field(default_factory=dict)
    state_file: Path | None = None
    log_file: Path | None = None

    def pipette(self, pipette: str):
        return self.pipette_1 if str(pipette).upper().endswith("P1") else self.pipette_2

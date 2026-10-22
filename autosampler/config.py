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
    steps_per_mm: float = Z_STEPS_PER_MM
    home_position_mm: float = 0.0
    safe_position_mm: float | None = None
    max_travel_mm: float | None = None
    dispense_position_mm: float | None = None
    aspiration_machine_offset_mm: float | None = None
    motion_speed_mm_min: float | None = None
    acceleration_mm_s2: float | None = None


@dataclass
class PipetteConfig:
    name: str
    axis: str
    steps_per_mm: float
    max_volume_ul: float
    home_position_mm: float = 0.0
    contact_position_mm: float | None = None
    max_travel_mm: float | None = None
    # Initial model: microlitres per millimetre. Must be physically calibrated.
    volume_per_mm_ul: float | None = None
    speed_mm_min: float | None = None
    acceleration_mm_s2: float | None = None


@dataclass
class TransferConfig:
    p1_multi_threshold_ul: float = 1100.0
    default_draw_buffer_percent: float | None = None
    tolerance_ul: float | None = None
    max_correction_attempts: int | None = None
    scale_stabilization_time_s: float | None = None
    minimum_correction_volume_ul: float | None = None
    aspiration_immersion_depth_mm: float | None = None
    z_liquid_clearance_mm: float | None = None
    default_cleaning_cycles: int = 3


@dataclass
class MachineConfig:
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
    # Location -> calibrated carousel angle. Pipette offsets stay independent.
    pipette_1_positions_deg: dict[str, float | None] = field(default_factory=dict)
    pipette_2_positions_deg: dict[str, float | None] = field(default_factory=dict)
    state_file: Path | None = None
    log_file: Path | None = None

    def pipette(self, pipette: str):
        return self.pipette_1 if str(pipette).upper().endswith("P1") else self.pipette_2

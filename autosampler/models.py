from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Axis(str, Enum):
    CAROUSEL = "X"
    Z = "Z"
    PIPETTE_1 = "A"
    PIPETTE_2 = "B"

    @property
    def friendly_name(self) -> str:
        return {
            Axis.CAROUSEL: "Carousel",
            Axis.Z: "Z Carriage",
            Axis.PIPETTE_1: "Pipette 1",
            Axis.PIPETTE_2: "Pipette 2",
        }[self]


class PipetteId(str, Enum):
    P1 = "P1"
    P2 = "P2"


class OperationState(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class ScaleReading:
    mass_g: float
    stable: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MachineState:
    grbl_connected: bool = False
    scale_connected: bool = False
    homed: bool = False
    position_trusted: bool = False
    carousel_referenced: bool = False
    carousel_steps: int | None = None
    last_reference_event: datetime | None = None
    active_pipette: PipetteId | None = None
    positions: dict[str, float | None] = field(
        default_factory=lambda: {axis.value: None for axis in Axis}
    )
    homed_axes: set[str] = field(default_factory=set)
    limit_pins_pressed: set[str] = field(default_factory=set)
    grbl_status_report: str | None = None
    vessel_under_pipette: str | None = None
    pipette_contents_ul: dict[str, float] = field(
        default_factory=lambda: {PipetteId.P1.value: 0.0, PipetteId.P2.value: 0.0}
    )
    operation_state: OperationState = OperationState.IDLE
    current_operation: str | None = None
    abort_requested: bool = False
    calibration_unlocked: bool = False
    scale_reading: ScaleReading | None = None
    last_error: str | None = None

    @property
    def carousel_angle_deg(self) -> float | None:
        if self.carousel_steps is None:
            return None
        from .motion.carousel import steps_to_degrees

        return steps_to_degrees(self.carousel_steps)

    def invalidate_position(self, reason: str | None = None) -> None:
        self.homed = False
        self.position_trusted = False
        self.carousel_referenced = False
        self.carousel_steps = None
        self.positions = {axis.value: None for axis in Axis}
        self.homed_axes.clear()
        self.vessel_under_pipette = None
        self.calibration_unlocked = False
        if reason:
            self.last_error = reason

    def mark_homed(self) -> None:
        self.homed_axes = {axis.value for axis in Axis}
        self.homed = True
        self.position_trusted = True
        self.carousel_referenced = True
        self.carousel_steps = 0
        self.positions = {axis.value: 0.0 for axis in Axis}
        self.last_reference_event = datetime.now(timezone.utc)
        self.abort_requested = False
        self.calibration_unlocked = False
        self.operation_state = OperationState.IDLE
        self.last_error = None

    def mark_axis_homed(self, axis: Axis) -> None:
        self.homed_axes.add(axis.value)
        self.positions[axis.value] = 0.0
        if axis is Axis.CAROUSEL:
            self.carousel_referenced = True
            self.carousel_steps = 0
            self.last_reference_event = datetime.now(timezone.utc)
        all_axes = {item.value for item in Axis}
        self.homed = self.homed_axes == all_axes
        self.position_trusted = self.homed
        self.abort_requested = False
        self.calibration_unlocked = False
        self.operation_state = OperationState.IDLE if self.homed else OperationState.RECOVERY_REQUIRED
        self.last_error = None

from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager

from autosampler.config import MachineConfig
from autosampler.controllers.interfaces import GrblController, ScaleController
from autosampler.errors import (
    ConnectionFailure,
    ControllerError,
    CorrectionFailure,
    MissingCalibration,
    OperationAborted,
    RecoveryRequired,
    ScaleError,
    ValidationError,
)
from autosampler.models import Axis, MachineState, OperationState, PipetteId
from autosampler.motion.carousel import degrees_to_steps, steps_to_degrees
from autosampler.persistence import StateStore
from autosampler.transfer_planner import TransferPlan, TransferPlanner
from autosampler.units import mass_g_to_volume_ul
from autosampler.vessels.geometry import (
    BEAKER_100ML_GEOMETRY,
    VIAL_15ML_GEOMETRY,
    aspiration_height_mm,
)
from autosampler.vessels.inventory import Inventory
from autosampler.vessels.models import VesselCategory, VesselType

log = logging.getLogger(__name__)


class AutosamplerMachine:
    """Hardware-independent command façade and single source of machine truth."""

    def __init__(
        self,
        config: MachineConfig,
        grbl: GrblController,
        scale: ScaleController,
        inventory: Inventory | None = None,
        store: StateStore | None = None,
    ):
        self.config = config
        self.grbl = grbl
        self.scale = scale
        self.state = MachineState()
        if store is None and config.state_file is None:
            from autosampler.app_paths import ApplicationPaths
            config.state_file = ApplicationPaths.platform_default().session_state
        self.store = store or StateStore(config.state_file)
        self.inventory = inventory if inventory is not None else self.store.load()
        self.planner = TransferPlanner(
            config.pipette_1.max_volume_ul,
            config.pipette_2.max_volume_ul,
            config.transfer.p1_multi_threshold_ul,
        )
        self._command_lock = threading.RLock()
        self._abort_generation = 0

    @contextmanager
    def _operation(
        self,
        name: str,
        require_home: bool = True,
        allow_calibration_bypass: bool = False,
        allow_homed_axis: Axis | None = None,
    ):
        with self._command_lock:
            if not self.grbl.connected:
                raise RecoveryRequired("GRBL is disconnected.")
            trusted = self.state.homed and self.state.position_trusted
            bypass = allow_calibration_bypass and self.state.calibration_unlocked
            axis_trusted = bool(
                allow_homed_axis
                and allow_homed_axis.value in self.state.homed_axes
                and self.state.positions[allow_homed_axis.value] is not None
            )
            if require_home and not (trusted or bypass or axis_trusted):
                raise RecoveryRequired("Machine position is untrusted; Home is required.")
            if self.state.operation_state is OperationState.RECOVERY_REQUIRED and not (bypass or axis_trusted):
                raise RecoveryRequired("Machine is in recovery-required state; Home is required.")
            if self.state.abort_requested:
                raise OperationAborted("Operation was aborted.")
            previous = self.state.current_operation
            self.state.current_operation = name
            self.state.operation_state = OperationState.BUSY
            try:
                yield
                if self.state.abort_requested:
                    raise OperationAborted("Operation was aborted.")
                self.state.operation_state = (
                    OperationState.IDLE
                    if self.state.position_trusted
                    else OperationState.RECOVERY_REQUIRED
                )
                self.state.last_error = None
            except Exception as exc:
                grbl_lost = isinstance(exc, (ConnectionFailure, ControllerError)) and not self.grbl.connected
                if grbl_lost:
                    self.state.grbl_connected = False
                    self.state.invalidate_position("GRBL connection lost; Home is required after reconnect.")
                    self.state.operation_state = OperationState.RECOVERY_REQUIRED
                if isinstance(exc, (ConnectionFailure, ScaleError)) and not self.scale.connected:
                    self.state.scale_connected = False
                if grbl_lost or isinstance(exc, (OperationAborted, RecoveryRequired)) or self.state.abort_requested:
                    self.state.operation_state = OperationState.RECOVERY_REQUIRED
                else:
                    self.state.operation_state = OperationState.ERROR
                self.state.last_error = str(exc)
                raise
            finally:
                self.state.current_operation = previous

    def connect_grbl(self) -> None:
        self.grbl.connect()
        self.state.grbl_connected = True
        self.state.invalidate_position("Connection established; Home is required.")
        self.state.operation_state = OperationState.RECOVERY_REQUIRED
        log.info("GRBL connected; coordinates untrusted until homed")

    def disconnect_grbl(self) -> None:
        self.grbl.disconnect()
        self.state.grbl_connected = False
        self.state.limit_pins_pressed.clear()
        self.state.grbl_status_report = None
        self.state.invalidate_position("GRBL disconnected.")
        self.state.operation_state = OperationState.RECOVERY_REQUIRED

    def connect_scale(self) -> None:
        self.scale.connect()
        self.state.scale_connected = True
        log.info("Scale connected")

    def refresh_scale_state(self) -> None:
        """Copy the controller's nonblocking live sample into authoritative state."""
        if self.state.scale_connected and not self.scale.connected:
            self.notify_scale_disconnected("A&D FZ-500i connection lost.")
            return
        self.state.scale_reading = self.scale.latest_reading

    def disconnect_scale(self) -> None:
        self.scale.disconnect()
        self.state.scale_connected = False
        self.state.scale_reading = None

    def notify_grbl_disconnected(self, reason="GRBL USB device disappeared.") -> None:
        self._abort_generation += 1
        try:
            self.grbl.disconnect()
        finally:
            self.state.grbl_connected = False
            self.state.limit_pins_pressed.clear()
            self.state.grbl_status_report = None
            self.state.abort_requested = True
            self.state.invalidate_position(reason + " Home is required after reconnect.")
            self.state.operation_state = OperationState.RECOVERY_REQUIRED
            log.error(reason)

    def notify_scale_disconnected(self, reason="Scale USB device disappeared.") -> None:
        try:
            self.scale.disconnect()
        finally:
            self.state.scale_connected = False
            self.state.last_error = reason
            if self.state.current_operation:
                try:
                    self.abort()
                except Exception:
                    log.exception("Could not issue GRBL Abort after scale loss")
            log.error(reason)

    def home(self, axis: Axis | str | None = None) -> None:
        if not self.grbl.connected:
            raise RecoveryRequired("GRBL is disconnected.")
        selected_axis = Axis(axis) if axis is not None else None
        label = selected_axis.value if selected_axis else "all"
        self.state.current_operation = f"home {label}"
        self.state.operation_state = OperationState.BUSY
        try:
            self.grbl.home(selected_axis.value if selected_axis else None)
            if selected_axis is None:
                self.state.mark_homed()
                log.info("Homing completed; X/Z/A/B positions trusted")
            else:
                self.state.mark_axis_homed(selected_axis)
                log.info(
                    "Homing completed for %s; homed axes: %s",
                    selected_axis.value,
                    ",".join(sorted(self.state.homed_axes)),
                )
        except Exception as exc:
            self.state.invalidate_position(str(exc))
            self.state.operation_state = OperationState.RECOVERY_REQUIRED
            raise
        finally:
            self.state.current_operation = None

    def unlock_for_calibration(self) -> str:
        """Send GRBL $X while retaining unknown coordinates and limiting use to manual jogs."""
        if not self.grbl.connected:
            raise RecoveryRequired("GRBL is disconnected.")
        generation = self._abort_generation
        response = self.grbl.unlock()
        if generation != self._abort_generation:
            raise OperationAborted("Calibration unlock was cancelled by Abort/disconnection.")
        self.state.invalidate_position("Calibration unlock active: coordinates remain untrusted.")
        self.state.calibration_unlocked = True
        self.state.abort_requested = False
        self.state.operation_state = OperationState.RECOVERY_REQUIRED
        log.warning("CALIBRATION BYPASS enabled with $X; only relative move_axis jogging is allowed")
        return response

    def disable_calibration_bypass(self) -> None:
        self.state.calibration_unlocked = False
        if not self.state.position_trusted:
            self.state.operation_state = OperationState.RECOVERY_REQUIRED
        log.warning("Calibration bypass disabled")

    def abort(self) -> None:
        self._abort_generation += 1
        self.state.abort_requested = True
        try:
            if self.grbl.connected:
                self.grbl.abort()
        except ConnectionFailure:
            self.state.grbl_connected = False
            raise
        finally:
            self.state.invalidate_position("Abort/reset: operator-initiated Home is required.")
            self.state.abort_requested = True
            self.state.operation_state = OperationState.RECOVERY_REQUIRED
            self.state.current_operation = None
            log.warning("ABORT: motion stopped and all physical coordinates invalidated")

    def status(self) -> str:
        return self.grbl.status()

    def refresh_grbl_status(self) -> str:
        """Read GRBL's realtime report and retain its currently triggered limit axes."""
        report = self.grbl.status()
        match = re.search(r"(?:^|[|<])Pn:([^|>]*)", report)
        active = set(match.group(1).upper()) if match else set()
        self.state.limit_pins_pressed = {
            axis.value for axis in Axis if axis.value in active
        }
        self.state.grbl_status_report = report
        return report

    def steps_from_home(self, axis: Axis | str) -> int | None:
        """Return the commanded signed motor-step position since this axis was homed."""
        axis = Axis(axis)
        if axis.value not in self.state.homed_axes:
            return None
        if axis is Axis.CAROUSEL:
            return self.state.carousel_steps
        position_mm = self.state.positions[axis.value]
        if position_mm is None:
            return None
        steps_per_mm = {
            Axis.Z: self.config.z.steps_per_mm,
            Axis.PIPETTE_1: self.config.pipette_1.steps_per_mm,
            Axis.PIPETTE_2: self.config.pipette_2.steps_per_mm,
        }[axis]
        return round(position_mm * steps_per_mm)

    def move_axis(self, axis: Axis | str, amount: float, unit: str) -> dict:
        axis = Axis(axis)
        amount = float(amount)
        unit = unit.lower().strip()
        if amount == 0:
            raise ValidationError("Movement amount cannot be zero.")
        with self._operation(
            "move_axis",
            allow_calibration_bypass=True,
            allow_homed_axis=axis,
        ):
            grbl_amount, physical_steps, actual = self._convert_manual_move(axis, amount, unit)
            start = self.state.positions[axis.value]
            response = self.grbl.move_relative(axis.value, grbl_amount)
            if start is not None:
                self.state.positions[axis.value] = start + grbl_amount
            if axis is Axis.CAROUSEL:
                if self.state.carousel_steps is not None:
                    self.state.carousel_steps += physical_steps
                    self.state.positions[axis.value] = float(self.state.carousel_steps)
            result = {
                "axis": axis.value,
                "friendly_axis": axis.friendly_name,
                "requested": amount,
                "unit": unit,
                "grbl_amount": grbl_amount,
                "integer_steps": physical_steps,
                "theoretical_actual": actual,
                "response": response,
                "start": start,
                "end": self.state.positions[axis.value],
            }
            log.info("CALIBRATION_MOVE %s", result)
            return result

    def _convert_manual_move(self, axis: Axis, amount: float, unit: str) -> tuple[float, int, float]:
        if axis is Axis.CAROUSEL:
            if unit in ("degree", "degrees", "deg"):
                steps = degrees_to_steps(amount)
                if steps == 0:
                    raise ValidationError("Requested angle rounds to zero physical motor steps.")
                return float(steps), steps, steps_to_degrees(steps)
            if unit in ("step", "steps", "raw steps"):
                if not amount.is_integer():
                    raise ValidationError("Carousel raw steps must be an integer.")
                return amount, int(amount), steps_to_degrees(int(amount))
            raise ValidationError("Carousel unit must be degrees or steps.")
        steps_per_mm = {
            Axis.Z: self.config.z.steps_per_mm,
            Axis.PIPETTE_1: self.config.pipette_1.steps_per_mm,
            Axis.PIPETTE_2: self.config.pipette_2.steps_per_mm,
        }[axis]
        if unit in ("mm", "millimetres", "millimeters"):
            return amount, round(amount * steps_per_mm), amount
        if unit in ("step", "steps", "raw steps"):
            if not amount.is_integer():
                raise ValidationError("Raw motor steps must be an integer.")
            mm = amount / steps_per_mm
            return mm, int(amount), mm
        raise ValidationError("Unit must be mm or steps for this axis.")

    def record_carousel_reference(self) -> None:
        if not self.grbl.connected:
            raise RecoveryRequired("GRBL is disconnected.")
        self.grbl.set_axis_position(Axis.CAROUSEL.value, 0.0)
        self.state.carousel_steps = 0
        self.state.positions[Axis.CAROUSEL.value] = 0.0
        self.state.carousel_referenced = True
        from datetime import datetime, timezone
        self.state.last_reference_event = datetime.now(timezone.utc)
        log.info("Carousel index/reference event: X corrected to zero")

    def go_to_spot(self, location_id: str, pipette: PipetteId) -> int:
        with self._operation("go_to_spot"):
            self._move_z_safe()
            mapping = (
                self.config.pipette_1_positions_deg
                if pipette is PipetteId.P1
                else self.config.pipette_2_positions_deg
            )
            angle = mapping.get(location_id)
            if angle is None:
                raise MissingCalibration(f"Carousel position for {location_id} using {pipette.value} has not been calibrated.")
            target = degrees_to_steps(angle)
            self.grbl.move_absolute(Axis.CAROUSEL.value, float(target))
            self.state.carousel_steps = target
            self.state.positions[Axis.CAROUSEL.value] = float(target)
            self.state.active_pipette = pipette
            self.state.vessel_under_pipette = location_id
            log.info("GO_TO_SPOT location=%s pipette=%s angle=%g steps=%d", location_id, pipette.value, angle, target)
            return target

    def _move_z_safe(self) -> None:
        position = self.config.z.safe_position_mm
        if position is None:
            raise MissingCalibration("Z safe position has not been calibrated.")
        self.grbl.move_absolute(Axis.Z.value, position, self.config.z.motion_speed_mm_min)
        self.state.positions[Axis.Z.value] = position

    def _pipette_config(self, pipette: PipetteId):
        return self.config.pipette_1 if pipette is PipetteId.P1 else self.config.pipette_2

    def _travel_for_volume(self, volume_ul: float, pipette: PipetteId) -> float:
        cfg = self._pipette_config(pipette)
        if volume_ul < 0 or volume_ul > cfg.max_volume_ul + 1e-9:
            raise ValidationError(f"{cfg.name} cannot handle {volume_ul:g} µL in one cycle.")
        if cfg.contact_position_mm is None:
            raise MissingCalibration(f"{cfg.name} contact position has not been calibrated.")
        if cfg.volume_per_mm_ul is None:
            raise MissingCalibration(f"{cfg.name} volume-to-travel calibration has not been calibrated.")
        if cfg.max_travel_mm is None:
            raise MissingCalibration(f"{cfg.name} maximum safe travel has not been calibrated.")
        travel = volume_ul / cfg.volume_per_mm_ul
        if travel > cfg.max_travel_mm + 1e-9:
            raise ValidationError(f"{cfg.name} requested travel exceeds maximum safe travel.")
        return travel

    def _aspirate_motion(self, volume_ul: float, pipette: PipetteId) -> None:
        cfg = self._pipette_config(pipette)
        travel = self._travel_for_volume(volume_ul, pipette)
        self.grbl.move_absolute(cfg.axis, cfg.contact_position_mm)
        self.grbl.move_absolute(cfg.axis, cfg.contact_position_mm + travel, cfg.speed_mm_min)
        self.grbl.move_absolute(cfg.axis, cfg.home_position_mm, cfg.speed_mm_min)
        self.state.pipette_contents_ul[pipette.value] += volume_ul

    def _dispense_motion(self, volume_ul: float, pipette: PipetteId) -> None:
        held = self.state.pipette_contents_ul[pipette.value]
        if volume_ul > held + 1e-9:
            raise ValidationError(f"{pipette.value} contains only {held:g} µL.")
        cfg = self._pipette_config(pipette)
        travel = self._travel_for_volume(volume_ul, pipette)
        self.grbl.move_absolute(cfg.axis, cfg.contact_position_mm)
        self.grbl.move_absolute(cfg.axis, cfg.contact_position_mm + travel, cfg.speed_mm_min)
        self.grbl.move_absolute(cfg.axis, cfg.home_position_mm, cfg.speed_mm_min)
        self.state.pipette_contents_ul[pipette.value] = max(0.0, held - volume_ul)

    def empty_pipette(self, pipette: PipetteId) -> float:
        cfg = self._pipette_config(pipette)
        if cfg.max_travel_mm is None:
            raise MissingCalibration(f"{cfg.name} maximum safe travel has not been calibrated.")
        self.grbl.move_absolute(cfg.axis, cfg.max_travel_mm, cfg.speed_mm_min)
        self.grbl.move_absolute(cfg.axis, cfg.home_position_mm, cfg.speed_mm_min)
        self.grbl.move_absolute(cfg.axis, cfg.max_travel_mm, cfg.speed_mm_min)
        self.grbl.move_absolute(cfg.axis, cfg.home_position_mm, cfg.speed_mm_min)
        emptied = self.state.pipette_contents_ul[pipette.value]
        self.state.pipette_contents_ul[pipette.value] = 0.0
        return emptied

    def _geometry(self, vessel):
        return VIAL_15ML_GEOMETRY if vessel.vessel_type is VesselType.VIAL_15ML else BEAKER_100ML_GEOMETRY

    def draw(self, source_id: str, requested_ul: float, pipette: PipetteId, buffer_percent: float | None = None) -> float:
        with self._operation("draw"):
            source = self.inventory.get(source_id)
            buffer = self.config.transfer.default_draw_buffer_percent if buffer_percent is None else buffer_percent
            if buffer is None:
                raise MissingCalibration("Default draw buffer percentage has not been calibrated.")
            if buffer < 0:
                raise ValidationError("Draw buffer cannot be negative.")
            target = requested_ul * (1 + buffer / 100)
            if source.quantity_ul < target:
                raise ValidationError(f"{source.name} contains insufficient liquid for draw plus buffer.")
            tolerance = self.config.transfer.tolerance_ul
            attempts = self.config.transfer.max_correction_attempts
            minimum = self.config.transfer.minimum_correction_volume_ul
            immersion = self.config.transfer.aspiration_immersion_depth_mm
            if tolerance is None or attempts is None or minimum is None:
                raise MissingCalibration("Transfer correction limits have not been calibrated.")
            if immersion is None or self.config.z.aspiration_machine_offset_mm is None:
                raise MissingCalibration("Aspiration Z geometry has not been calibrated.")
            self.go_to_spot(source_id, pipette)
            baseline = self.scale.read_stable(self.config.transfer.scale_stabilization_time_s)
            self.state.scale_reading = baseline
            height = aspiration_height_mm(self._geometry(source), source.quantity_ul, immersion)
            z_target = self.config.z.aspiration_machine_offset_mm + height
            self.grbl.move_absolute(Axis.Z.value, z_target)
            correction = target
            measured = 0.0
            for attempt in range(1, attempts + 1):
                if self.state.abort_requested:
                    raise OperationAborted("Draw aborted.")
                if abs(correction) >= minimum:
                    if correction > 0:
                        self._aspirate_motion(correction, pipette)
                    else:
                        self._dispense_motion(-correction, pipette)  # return excess to source
                reading = self.scale.read_stable(self.config.transfer.scale_stabilization_time_s)
                self.state.scale_reading = reading
                measured = mass_g_to_volume_ul(baseline.mass_g - reading.mass_g, source.density_g_ml)
                correction = target - measured
                log.info("DRAW attempt=%d target_ul=%g measured_ul=%g correction_ul=%g", attempt, target, measured, correction)
                if abs(correction) <= tolerance:
                    self.inventory.remove(source_id, measured)
                    self.state.pipette_contents_ul[pipette.value] = measured
                    self._move_z_safe()
                    self.store.save(self.inventory)
                    return measured
            raise CorrectionFailure(f"Draw did not converge after {attempts} correction attempts (error {correction:g} µL).")

    def dump(self, pipette: PipetteId) -> float:
        with self._operation("dump"):
            waste = self.inventory.require_special(VesselCategory.WASTE)
            self.go_to_spot(waste.location_id, pipette)
            if self.config.z.dispense_position_mm is None:
                raise MissingCalibration("Z dispense position has not been calibrated.")
            self.grbl.move_absolute(Axis.Z.value, self.config.z.dispense_position_mm)
            amount = self.empty_pipette(pipette)
            self.inventory.add_volume(waste.location_id, amount)
            self._move_z_safe()
            self.store.save(self.inventory)
            return amount

    def dispense(self, destination_id: str, amount_ul: float, pipette: PipetteId) -> None:
        with self._operation("dispense"):
            destination = self.inventory.get(destination_id)
            if destination.quantity_ul + amount_ul > destination.capacity_ul:
                raise ValidationError(f"Dispense would overfill {destination.name}.")
            self.go_to_spot(destination_id, pipette)
            if self.config.z.dispense_position_mm is None:
                raise MissingCalibration("Z dispense position has not been calibrated.")
            self.grbl.move_absolute(Axis.Z.value, self.config.z.dispense_position_mm)
            self._dispense_motion(amount_ul, pipette)
            self.inventory.add_volume(destination_id, amount_ul)
            self._move_z_safe()
            self.store.save(self.inventory)
            self.dump(pipette)  # invariant: every normal dispense is followed by dump

    def clean(self, pipette: PipetteId, cycles: int | None = None) -> None:
        cycles = self.config.transfer.default_cleaning_cycles if cycles is None else cycles
        if cycles <= 0:
            raise ValidationError("Cleaning cycle count must be positive.")
        with self._operation("clean"):
            cleaning = self.inventory.require_special(VesselCategory.CLEANING)
            cfg = self._pipette_config(pipette)
            for _ in range(cycles):
                self.draw(cleaning.location_id, cfg.max_volume_ul, pipette, buffer_percent=0.0)
                self.go_to_spot(cleaning.location_id, pipette)
                if self.config.z.dispense_position_mm is None:
                    raise MissingCalibration("Z dispense position has not been calibrated.")
                self.grbl.move_absolute(Axis.Z.value, self.config.z.dispense_position_mm)
                returned = self.empty_pipette(pipette)
                self.inventory.add_volume(cleaning.location_id, returned)
                self._move_z_safe()
            self.store.save(self.inventory)

    def plan_transfer(self, amount_ul: float, prefer_p1_multicycle: bool | None = None) -> TransferPlan:
        return self.planner.plan(amount_ul, prefer_p1_multicycle)

    def transfer(self, source_id: str, destination_id: str, amount_ul: float,
                 confirm_multicycle: bool | None = None, prefer_p1_multicycle: bool | None = None) -> TransferPlan:
        self.inventory.validate_transfer(source_id, destination_id, amount_ul)
        plan = self.plan_transfer(amount_ul, prefer_p1_multicycle)
        if plan.requires_confirmation and confirm_multicycle is not True:
            if confirm_multicycle is False:
                raise ValidationError("Multi-cycle transfer was declined.")
            raise ValidationError("This transfer plan requires operator confirmation.")
        with self._operation("transfer"):
            for cycle in plan.cycles_ul:
                self.draw(source_id, cycle, plan.pipette)
                self.dispense(destination_id, cycle, plan.pipette)
            return plan

    def reset_session(self, confirmed: bool = False) -> None:
        if not confirmed:
            raise ValidationError("Session reset requires explicit confirmation.")
        self.inventory = self.store.reset()
        log.warning("Persistent liquid/session state reset; physical position unchanged")

    def poll_reference_sensor(self, sensor) -> bool:
        if sensor.consume_reference_event():
            self.record_carousel_reference()
            return True
        return False

    def shutdown(self) -> None:
        """Stop safely, persist logical state, and close transports without movement."""
        if self.state.current_operation and self.grbl.connected:
            try:
                self.abort()
            except Exception:
                log.exception("Error while aborting during shutdown")
        try:
            self.store.save(self.inventory)
        except Exception:
            log.exception("Could not save logical state during shutdown")
        try:
            if self.scale.connected:
                self.disconnect_scale()
        finally:
            if self.grbl.connected:
                self.disconnect_grbl()

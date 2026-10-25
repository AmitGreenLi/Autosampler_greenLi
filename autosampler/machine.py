from __future__ import annotations

import logging
import math
import re
import threading
import time
from contextlib import contextmanager

from autosampler.config import CAROUSEL_STEPS_PER_DEGREE, MachineConfig
from autosampler.calibration import calibration_issues, geometry_errors, measurements, set_value, save_calibration, FIRMWARE_SETTINGS, FIRMWARE_MEASUREMENTS, carousel_angle, CAROUSEL_OFFSETS_DEG, normalize_z_entry
from autosampler.controllers.interfaces import GrblController, ScaleController
from autosampler.errors import (
    ConnectionFailure,
    ControllerError,
    ControllerAlarm,
    CorrectionFailure,
    MissingCalibration,
    OperationAborted,
    RecoveryRequired,
    ScaleError,
    ScaleUnstable,
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
        self.state.pipette_contents_ul = self.inventory.pipette_contents_ul
        self.planner = TransferPlanner(
            config.pipette_1.max_volume_ul,
            config.pipette_2.max_volume_ul,
            config.transfer.p1_multi_threshold_ul,
        )
        self._command_lock = threading.RLock()
        self._abort_generation = 0
        self.calibration_file = None
        self.overshoot_progress = "No overshoot calibration run."
        self.overshoot_result = None
        self._overshoot_running = False
        self._positioning_scale_warning = False
        self._optional_positioning_scale = False

    def calibration_issues(self):
        return calibration_issues(self.config, self.inventory.vessels)

    def positioning_issues(self, location_id, pipette):
        """Only prerequisites for retracting Z and positioning the carousel."""
        issues = []
        for spec in (m for m in measurements(()) if m.key in (
                "carousel_overshoot_deg", "carousel_secondary_overshoot_deg",
                "carousel_stability_timeout_s", "carousel_jostle_deg")):
            try:
                spec.validate(getattr(self.config, spec.key))
            except ValidationError as exc:
                issues.append(str(exc))
        try:
            self.inventory.get(location_id).validate()
        except ValidationError as exc:
            issues.append(str(exc))
        z = self.config.z
        for label, value in (("Z maximum travel", z.max_travel_mm), ("Z firmware speed", z.motion_speed_mm_min)):
            if value is None or not math.isfinite(value) or value <= 0:
                issues.append(f"{label} must be calibrated/read from GRBL")
        if z.safe_position_mm is None or not math.isfinite(z.safe_position_mm):
            issues.append("Z safe position must be calibrated")
        elif z.target_error(z.safe_position_mm):
            issues.append(z.target_error(z.safe_position_mm))
        angle = carousel_angle(self.config, location_id, 1 if pipette is PipetteId.P1 else 2)
        if angle is None or not math.isfinite(angle):
            issues.append(f"Carousel position for {location_id} using {pipette.value} has not been calibrated.")
        return issues

    def read_firmware_calibration(self):
        """Import all six firmware limits atomically; never establish position trust."""
        from copy import deepcopy

        with self._command_lock:
            if not self.grbl.connected:
                raise RecoveryRequired("Connect GRBL before reading firmware settings.")
            if self.state.current_operation:
                raise ValidationError("Wait for the current operation to finish.")
            generation = self._abort_generation
            settings = self.grbl.read_settings()
            candidate = deepcopy(self.config)
            values = {}
            for spec in FIRMWARE_MEASUREMENTS:
                number = FIRMWARE_SETTINGS[spec.key]
                if number not in settings:
                    raise ValidationError(f"GRBL did not report required setting ${number}; no values updated.")
                values[spec.key] = spec.validate(settings[number])
                set_value(candidate, spec.key, values[spec.key])
            if generation != self._abort_generation or not self.grbl.connected:
                raise OperationAborted("Firmware import cancelled by Abort/disconnection.")
            if self.calibration_file:
                save_calibration(candidate, self.calibration_file, self.inventory.vessels)
            for key, value in values.items():
                set_value(self.config, key, value)
            log.info("GRBL firmware limits imported: %s", values)
            return values

    def record_calibration(self, key: str, value: float, confirmed: bool = False):
        """Record an operator measurement without moving or establishing position trust."""
        from copy import deepcopy

        if confirmed is not True:
            raise ValidationError("Calibration requires explicit operator confirmation.")
        with self._command_lock:
            if self.state.current_operation:
                raise ValidationError("Wait for motion to finish before recording calibration.")
            spec = next((m for m in measurements(self.inventory.vessels) if m.key == key), None)
            if spec is None:
                raise ValidationError("Unknown calibration measurement.")
            candidate = deepcopy(self.config)
            value = spec.validate(value)
            value = normalize_z_entry(candidate, key, value)
            set_value(candidate, key, value)
            errors = geometry_errors(candidate)
            if errors:
                raise ValidationError("; ".join(errors))
            if self.calibration_file:
                save_calibration(candidate, self.calibration_file, self.inventory.vessels)
            set_value(self.config, key, value)
            log.info("OPERATOR_CONFIRMED_CALIBRATION %s=%s", key, value)

    @contextmanager
    def _operation(
        self,
        name: str,
        require_home: bool = True,
        allow_calibration_bypass: bool = False,
        allow_homed_axis: Axis | None = None,
        require_calibration: bool = True,
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
            if require_calibration and not allow_calibration_bypass:
                issues = self.calibration_issues()
                if issues:
                    raise MissingCalibration("Automated operations locked; calibration required: " + "; ".join(issues))
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
                if (name in ("draw", "dispense", "dump", "clean", "transfer", "empty_pipette")
                        and isinstance(exc, (ConnectionFailure, ScaleError))
                        and not isinstance(exc, ScaleUnstable) and self.grbl.connected):
                    self._fault_hold(str(exc))
                if isinstance(exc, ControllerError) and self.grbl.connected:
                    self._fault_hold(str(exc))
                if grbl_lost or isinstance(exc, (OperationAborted, RecoveryRequired, ControllerError)) or self.state.abort_requested or not self.state.position_trusted:
                    self.state.invalidate_position(str(exc))
                    self.state.operation_state = OperationState.RECOVERY_REQUIRED
                else:
                    self.state.operation_state = OperationState.ERROR
                self.state.last_error = str(exc)
                raise
            finally:
                self.state.current_operation = previous

    def connect_grbl(self) -> None:
        try:
            self.grbl.connect()
        except Exception:
            self.state.grbl_connected = self.grbl.connected
            self.state.invalidate_position("GRBL connection/recovery failed; coordinates remain untrusted.")
            self.state.operation_state = OperationState.RECOVERY_REQUIRED
            raise
        self.state.grbl_connected = True
        self.state.homing_progress = None
        self.state.homing_axis_started_at = None
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

    def recover_grbl_connection(self):
        """Explicit operator recovery/reset. Never move or restore position trust."""
        with self._command_lock:
            if self.state.current_operation:
                raise ValidationError("Wait for the interrupted operation to finish before recovering.")
            self.overshoot_result = None
            if self.grbl.connected:
                self.grbl.abort()
            self.disconnect_grbl()
            self.connect_grbl()
            self.overshoot_progress = "GRBL reconnected. Home all axes, then start calibration again."

    def connect_scale(self) -> None:
        self.scale.connect()
        self.state.scale_connected = True
        self._positioning_scale_warning = False
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
            if self._optional_positioning_scale:
                self.state.scale_reading = None
                self._warn_positioning_scale_unavailable(reason)
                return
            if self.state.current_operation:
                self._fault_hold(reason)
            log.error(reason)

    def _fault_hold(self, reason):
        try:
            if self.grbl.connected and not self.state.abort_requested:
                self._abort_generation += 1
                self.state.abort_requested = True
                self.grbl.hold_for_fault()
        except Exception:
            log.exception("Could not request GRBL fault hold")
        finally:
            self.state.invalidate_position(reason + " Fault hold requested; motion/holding torque unconfirmed. No reset requested.")
            self.state.operation_state = OperationState.RECOVERY_REQUIRED

    def home(self, axis: Axis | str | None = None, *, abort_generation: int | None = None) -> None:
        with self._command_lock:
            self._home(axis, abort_generation=abort_generation)

    def _home(self, axis: Axis | str | None = None, *, abort_generation: int | None = None) -> None:
        if not self.grbl.connected:
            raise RecoveryRequired("GRBL is disconnected.")
        selected_axis = Axis(axis) if axis is not None else None
        generation = self._abort_generation if abort_generation is None else abort_generation
        if generation != self._abort_generation:
            raise OperationAborted("Homing request was cancelled by Abort/disconnection.")
        if selected_axis is Axis.CAROUSEL:
            self._require_z_clearance()
            self._await_motion("Z", self.state.positions["Z"])
        axes = (selected_axis,) if selected_axis else (Axis.Z, Axis.CAROUSEL, Axis.PIPETTE_1, Axis.PIPETTE_2)
        # Check the complete sequence BEFORE changing trust or starting Z. An
        # unsafe A/B configuration must not be discovered after Z/X have moved.
        search_rates = ({"X": self.config.carousel_homing_search_deg_s * CAROUSEL_STEPS_PER_DEGREE * 60}
                        if Axis.CAROUSEL in axes else {})
        if Axis.Z in axes:
            search_rates["Z"] = self.config.z_homing_search_rate
        try:
            self.grbl.validate_homing(tuple(item.value for item in axes), search_rates)
        except (ConnectionFailure, RecoveryRequired, OperationAborted, ControllerAlarm) as exc:
            self.state.grbl_connected = self.grbl.connected
            self.state.invalidate_position(str(exc))
            self.state.operation_state = OperationState.RECOVERY_REQUIRED
            raise
        if generation != self._abort_generation:
            raise OperationAborted("Homing request was cancelled during preflight by Abort/disconnection.")
        label = selected_axis.value if selected_axis else "all"
        self.state.current_operation = f"home {label}"
        self.state.operation_state = OperationState.BUSY
        self.state.reported_position.invalidate()
        if selected_axis is None:
            self.state.invalidate_position("Homing in progress.")
        else:
            self.state.homed_axes.discard(selected_axis.value)
            self.state.positions[selected_axis.value] = None
            self.state.homed = self.state.position_trusted = False
            if selected_axis is Axis.CAROUSEL:
                self.state.carousel_referenced = False
                self.state.carousel_steps = None
        try:
            # Use separate cycles so the temporary carousel speed never reaches Z/A/B.
            for index, item in enumerate(axes, 1):
                if generation != self._abort_generation:
                    raise OperationAborted("Homing was cancelled by Abort/disconnection.")
                self.state.homing_progress = f"Homing {item.value} ({index}/{len(axes)}) — waiting for controller completion"
                self.state.homing_axis_started_at = time.monotonic()
                log.info("HOMING_AXIS_START %s (%d/%d)", item.value, index, len(axes))
                if item is Axis.CAROUSEL:
                    self.grbl.home("X", search_rate=self.config.carousel_homing_search_deg_s * CAROUSEL_STEPS_PER_DEGREE * 60)
                elif item is Axis.Z:
                    self.grbl.home("Z", search_rate=self.config.z_homing_search_rate)
                else:
                    self.grbl.home(item.value)
                if generation != self._abort_generation:
                    raise OperationAborted("Homing was cancelled by Abort/disconnection.")
                self._verify_homing_origin(item)
                if generation != self._abort_generation:
                    raise OperationAborted("Homing was cancelled during origin verification.")
                log.info("HOMING_AXIS_COMPLETE %s", item.value)
            self.state.homing_progress = "Homing completed — all requested axes acknowledged completion."
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
            failed_axis = self.state.homing_progress or "Homing"
            self.state.homing_progress = f"{failed_axis.split(' — ')[0]} failed: {exc}"
            log.error("HOMING_FAILED %s", self.state.homing_progress)
            self.state.invalidate_position(str(exc))
            self.state.operation_state = OperationState.RECOVERY_REQUIRED
            raise
        finally:
            self.state.homing_axis_started_at = None
            self.state.current_operation = None

    def _verify_homing_origin(self, axis):
        from autosampler.position_tracking import PositionTracking
        tracker = PositionTracking()
        tracker.offset = dict(self.grbl.work_offsets)
        report = self.grbl.status()
        tracker.ingest(report)
        resolution = {Axis.CAROUSEL: 1.0, Axis.Z: self.config.z.steps_per_mm,
                      Axis.PIPETTE_1: self.config.pipette_1.steps_per_mm,
                      Axis.PIPETTE_2: self.config.pipette_2.steps_per_mm}[axis]
        actual = tracker.work.get(axis.value)
        if tracker.controller_state != "Idle" or actual is None or not math.isclose(actual, 0.0, abs_tol=1 / resolution, rel_tol=0):
            raise RecoveryRequired(f"HOMING_ORIGIN_MISMATCH {axis.value}: expected work zero within one step, received {report}. Verify firmware origin and WCO; no offset changed.")

    def unlock_for_calibration(self, *, abort_generation: int | None = None) -> str:
        """Send GRBL $X while retaining unknown coordinates and limiting use to manual jogs."""
        if not self.grbl.connected:
            raise RecoveryRequired("GRBL is disconnected.")
        generation = self._abort_generation if abort_generation is None else abort_generation
        if generation != self._abort_generation:
            raise OperationAborted("Calibration unlock request was cancelled by Abort/disconnection.")
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
        self.overshoot_result = None
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

    def send_manual_command(self, command: str, on_response=None, abort_generation=None) -> str:
        """Send one operator-entered line without calibration or homing prerequisites."""
        log.info("Manual GRBL > %s", command)
        def report_response(response):
            self._observe_report(response)
            log.info("Manual GRBL < %s", response)
            if on_response is not None:
                on_response(response)

        try:
            return self._send_manual_command(command, report_response, abort_generation)
        except Exception as exc:
            log.error("Manual command: %s: %s", type(exc).__name__, exc)
            raise

    def _send_manual_command(self, command: str, on_response, abort_generation) -> str:
        if not isinstance(command, str) or not command.strip():
            raise ValidationError("Enter a command.")
        if any(ord(character) < 32 or ord(character) > 126 for character in command):
            raise ValidationError("Enter one ASCII command line without control characters.")
        with self._command_lock:
            if not self.grbl.connected:
                raise RecoveryRequired("GRBL is disconnected.")
            if self.state.abort_requested or (abort_generation is not None and abort_generation != self._abort_generation):
                raise OperationAborted("Abort is active; use Home before sending manual commands.")
            if self.state.current_operation:
                raise ValidationError("Wait for the current operation to finish.")
            if "~" in command:
                report = self.grbl.status()
                controller_state = report[1:].split("|", 1)[0].rstrip(">") if report.startswith("<") else "Unknown"
                if controller_state not in ("Idle", "Alarm"):
                    raise RecoveryRequired("Cycle start is blocked while interrupted motion may remain buffered; use deliberate Recover/reset.")
            generation = self._abort_generation
            self.state.invalidate_position("Manual command sent; Home is required before automated operation.", invalidate_measurement=False)
            # Raw movement leaves machine-coordinate measurement references intact.
            # Offset/modal edits may change the work frame without moving any motor.
            normalized = command.strip().upper()
            if normalized.startswith("$13"):
                self.state.reported_position.report_units_supported = False
            if normalized.startswith("$RST"):
                self.state.reported_position.report_units_supported = False
            if normalized.startswith(("$H", "$RST", "$13")):
                self.state.reported_position.invalidate()
            elif re.search(r"(?:G\s*(?:10|20|21|5[4-9](?:\.\d+)?|92(?:\.\d+)?))(?=\D|$)", normalized):
                self.state.reported_position.invalidate_work()
            self.state.current_operation = "manual command"
            self.state.operation_state = OperationState.BUSY
            try:
                response = self.grbl.send_command(command, on_response)
                if self.state.abort_requested or generation != self._abort_generation:
                    raise OperationAborted("Manual command was cancelled by Abort/disconnection.")
                if re.fullmatch(r"\$13\s*=\s*0(?:\.0*)?", normalized):
                    self.state.reported_position.report_units_supported = True
                return response
            except Exception as exc:
                if isinstance(exc, (ConnectionFailure, RecoveryRequired, OperationAborted, ControllerAlarm)):
                    self.state.invalidate_position(str(exc))
                self.state.last_error = str(exc)
                raise
            finally:
                self.state.grbl_connected = self.grbl.connected
                self.state.current_operation = None
                self.state.operation_state = OperationState.RECOVERY_REQUIRED

    def refresh_grbl_status(self) -> str:
        """Read GRBL's realtime report and retain its currently triggered limit axes."""
        generation = self._abort_generation
        try:
            report = self.grbl.status()
        except Exception as exc:
            self.state.grbl_status_report = None
            self.state.grbl_status_updated_at = None
            if isinstance(exc, (ConnectionFailure, ControllerAlarm, RecoveryRequired)):
                self.state.grbl_connected = False if isinstance(exc, ConnectionFailure) else self.grbl.connected
                self.state.invalidate_position(str(exc))
                self.state.operation_state = OperationState.RECOVERY_REQUIRED
            raise
        if generation != self._abort_generation:
            return report
        self._observe_report(report)
        if not (report.startswith("<") and report.endswith(">")):
            self.state.grbl_status_report = None
            self.state.grbl_status_updated_at = None
            return report
        match = re.search(r"(?:^|[|<])Pn:([^|>]*)", report)
        active = set(match.group(1).upper()) if match else set()
        self.state.limit_pins_pressed = {
            axis.value for axis in Axis if axis.value in active
        }
        self.state.grbl_status_report = report
        self.state.grbl_status_updated_at = time.monotonic()
        return report

    def _observe_report(self, report):
        if report.startswith("$13="):
            if (report.strip() == "$13=0") != self.state.reported_position.report_units_supported:
                self.state.reported_position.invalidate()
            self.state.reported_position.report_units_supported = report.strip() == "$13=0"
        if report.lower().startswith("grbl") or report.startswith(("<Alarm", "<Home", "ALARM:")):
            self.state.invalidate_position("Controller reset/alarm; Home is required.")
            self.state.operation_state = OperationState.RECOVERY_REQUIRED
        self.state.reported_position.ingest(report)

    def set_measurement_reference(self, coordinate: str, axis: str | None = None):
        with self._command_lock:
            if coordinate not in ("machine", "work"):
                raise ValidationError("Unknown coordinate system.")
            if self.state.current_operation:
                raise ValidationError("Wait for the current operation to finish.")
            axes = [Axis(axis).value] if axis else [a.value for a in Axis]
            tracking = self.state.reported_position
            if tracking.controller_state != "Idle":
                raise ValidationError("Wait for the controller to report Idle before setting a reference.")
            tracking.set_reference(coordinate, axes, self.state.grbl_connected)

    def record_travel_comparison(self, coordinate, axis, physical_distance, confirmed=False):
        import math
        from datetime import datetime, timezone
        with self._command_lock:
            axis = Axis(axis)
            tracking = self.state.reported_position
            if coordinate not in ("machine", "work"):
                raise ValidationError("Unknown coordinate system.")
            if (not confirmed or not math.isfinite(physical_distance)
                    or tracking.availability(self.state.grbl_connected) != "Live"
                    or tracking.controller_state != "Idle" or self.state.current_operation):
                raise ValidationError("Confirm a finite physical measurement with fresh, idle controller feedback.")
            displacement = tracking.displacement(coordinate, axis.value)
            if displacement is None:
                raise ValidationError("Set a measurement reference and complete a move first.")
            if axis is Axis.CAROUSEL:
                displacement = steps_to_degrees(displacement)
            record = dict(axis=axis.value, coordinate=coordinate, reported_displacement=displacement,
                          physical_distance=physical_distance, unit="degrees" if axis is Axis.CAROUSEL else "mm",
                          timestamp=datetime.now(timezone.utc).isoformat())
            tracking.comparisons.append(record)
            log.info("OPERATOR_TRAVEL_COMPARISON %s", record)
            return record

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

    def _require_z_clearance(self):
        z = self.config.z
        actual = self.state.positions["Z"]
        if ("Z" not in self.state.homed_axes or actual is None or z.safe_position_mm is None
                or (actual - z.safe_position_mm) * z.work_direction > 1e-9):
            raise RecoveryRequired("Home Z and move it to collision-free clearance before moving or homing X.")

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
            if axis is Axis.CAROUSEL:
                self._require_z_clearance()
                self._await_motion("Z", self.state.positions["Z"])
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
        self.state.reported_position.invalidate_work()
        self.grbl.set_axis_position(Axis.CAROUSEL.value, 0.0)
        self.state.carousel_steps = 0
        self.state.positions[Axis.CAROUSEL.value] = 0.0
        self.state.carousel_referenced = True
        from datetime import datetime, timezone
        self.state.last_reference_event = datetime.now(timezone.utc)
        log.info("Carousel index/reference event: X corrected to zero")

    def go_to_spot(self, location_id: str, pipette: PipetteId) -> int:
        with self._command_lock:
            previous = self._optional_positioning_scale
            self._optional_positioning_scale = self.state.current_operation is None and not self._overshoot_running
            try:
                return self._go_to_spot(location_id, pipette)
            finally:
                self._optional_positioning_scale = previous

    def _go_to_spot(self, location_id: str, pipette: PipetteId) -> int:
        with self._operation("go_to_spot", require_calibration=False):
            if location_id in CAROUSEL_OFFSETS_DEG:
                self.inventory.get(location_id)
            issues = self.positioning_issues(location_id, pipette)
            if issues:
                raise MissingCalibration("Positioning locked: " + "; ".join(issues))
            angle = carousel_angle(self.config, location_id, 1 if pipette is PipetteId.P1 else 2)
            if angle is None:
                raise MissingCalibration(f"Carousel position for {location_id} using {pipette.value} has not been calibrated.")
            target = degrees_to_steps(angle)
            extra_steps = degrees_to_steps(self.config.carousel_overshoot_deg)
            secondary_steps = degrees_to_steps(self.config.carousel_secondary_overshoot_deg)
            has_overshoot = bool(extra_steps or secondary_steps)
            monitor_scale = self.config.carousel_stability_timeout_s > 0 and not self._overshoot_running
            wait_for_stages = has_overshoot or self._overshoot_running or monitor_scale
            if monitor_scale:
                try:
                    self.scale.check_communication()
                except (ConnectionFailure, ScaleError) as exc:
                    self._warn_positioning_scale_unavailable(str(exc))
                    monitor_scale = False
            start = self.state.carousel_steps
            if has_overshoot and start is None:
                raise RecoveryRequired("Carousel start position is unknown; Home is required for overshoot.")
            overshoot_target = target
            reverse_target = target
            if has_overshoot and target != start:
                direction = 1 if target > start else -1
                overshoot_target += direction * extra_steps
                reverse_target -= direction * secondary_steps
            stages = [("outbound", overshoot_target)]
            if reverse_target != overshoot_target:
                stages.append(("secondary" if secondary_steps else "return", reverse_target))
            if stages[-1][1] != target:
                stages.append(("return", target))
            generation = self._abort_generation
            last_direction = None
            completed_target = start
            def cancelled():
                if self._overshoot_running:
                    self.scale.check_communication()
                return (generation != self._abort_generation or self.state.abort_requested
                        or not self.grbl.connected or not self.state.position_trusted
                        or self._overshoot_running and not self.scale.connected)
            def check_motion():
                if cancelled():
                    raise OperationAborted("Carousel positioning cancelled by Abort/disconnection.")
                if self.grbl.recovery_required:
                    raise RecoveryRequired("Controller recovery is required; carousel positioning stopped.")
            try:
                check_motion()
                self._move_z_safe()
                if wait_for_stages:
                    self.grbl.wait_for_axis_position(Axis.Z.value, self.config.z.safe_position_mm, cancelled=cancelled)
                check_motion()
                self.state.vessel_under_pipette = None
                for phase, stage_target in stages:
                    check_motion()
                    if self._overshoot_running:
                        self.overshoot_progress = f"{location_id} → {pipette.value}: {phase} to {stage_target} steps; final target {target} steps"
                    log.info("CAROUSEL_LEG phase=%s spot=%s pipette=%s start_steps=%s target_steps=%d final_steps=%d A_deg=%g B_deg=%g",
                             phase, location_id, pipette.value, start, stage_target, target,
                             self.config.carousel_overshoot_deg, self.config.carousel_secondary_overshoot_deg)
                    self.grbl.move_absolute(Axis.CAROUSEL.value, float(stage_target))
                    if wait_for_stages:
                        self.grbl.wait_for_axis_position(Axis.CAROUSEL.value, stage_target, cancelled=cancelled)
                    check_motion()
                    if completed_target is not None and stage_target != completed_target:
                        last_direction = 1 if stage_target > completed_target else -1
                    completed_target = stage_target
                check_motion()
                if monitor_scale:
                    self._check_positioning_stability(target, last_direction, check_motion, cancelled)
                check_motion()
            except Exception:
                # The first leg may still be moving. Never queue a return or reset
                # in response to an uncertain motion result.
                self._fault_hold("Carousel positioning failed/interrupted; Home is required.")
                raise
            self.state.carousel_steps = target
            self.state.positions[Axis.CAROUSEL.value] = float(target)
            self.state.active_pipette = pipette
            self.state.vessel_under_pipette = location_id
            log.info("GO_TO_SPOT location=%s pipette=%s angle=%g steps=%d", location_id, pipette.value, angle, target)
            return target

    def _warn_positioning_scale_unavailable(self, reason):
        if not self._positioning_scale_warning:
            self._positioning_scale_warning = True
            log.warning("CAROUSEL_SCALE_UNAVAILABLE: %s. Positioning continues without the scale stability check or corrective jostle.", reason)

    def _check_positioning_stability(self, target, last_direction, check_motion, cancelled):
        timeout = self.config.carousel_stability_timeout_s
        received = False
        def fresh_reading(reading):
            nonlocal received
            received = True
            self._positioning_scale_warning = False
        check_motion()
        try:
            self.scale.check_communication()
            log.info("CAROUSEL_STABILITY_WAIT target_steps=%d timeout_s=%g", target, timeout)
            elapsed = self.scale.measure_stabilization(0.0, timeout, cancelled, on_reading=fresh_reading)
        except ScaleUnstable:
            # An unstable deadline is the only condition that permits a jostle.
            # A communication error or disconnection must never trigger motion.
            check_motion()
            if not received:
                self._warn_positioning_scale_unavailable("No fresh scale readings within the stability timeout")
                return
            try:
                self.scale.check_communication()
            except (ConnectionFailure, ScaleError) as exc:
                self._warn_positioning_scale_unavailable(str(exc))
                return
        except (ConnectionFailure, ScaleError) as exc:
            check_motion()
            self._warn_positioning_scale_unavailable(str(exc))
            return
        else:
            self._positioning_scale_warning = False
            log.info("CAROUSEL_STABLE target_steps=%d stabilization_s=%g", target, elapsed)
            return
        steps = degrees_to_steps(self.config.carousel_jostle_deg)
        if not steps or last_direction is None:
            log.info("CAROUSEL_JOSTLE_SKIPPED target_steps=%d reason=%s", target,
                     "disabled" if not steps else "no carousel displacement in this call")
            return
        away = target - last_direction * steps
        for phase, stage_target in (("away", away), ("return", target)):
            check_motion()
            log.info("CAROUSEL_JOSTLE phase=%s target_steps=%d intended_steps=%d angle_deg=%g last_direction=%d",
                     phase, stage_target, target, self.config.carousel_jostle_deg, last_direction)
            self.grbl.move_absolute(Axis.CAROUSEL.value, float(stage_target))
            self.grbl.wait_for_axis_position(Axis.CAROUSEL.value, stage_target, cancelled=cancelled)
            check_motion()

    def calibrate_carousel_overshoot(self, options):
        """Sweep through normal positioning; never publish temporary angles to disk."""
        with self._command_lock:
            return self._calibrate_carousel_overshoot(options)

    def _calibrate_carousel_overshoot(self, options):
        from autosampler.overshoot_calibration import OvershootTrial, rank_trials

        self.overshoot_result = None
        pairs = options.candidate_pairs()
        dwell = options.stability_hold_s
        for spot in options.spots:
            if self.inventory.get(spot).vessel_type is not VesselType.VIAL_15ML:
                raise ValidationError("Overshoot calibration requires vial spots.")
            for pipette in (PipetteId.P1, PipetteId.P2):
                issues = self.positioning_issues(spot, pipette)
                if issues:
                    raise MissingCalibration("; ".join(issues))
            if degrees_to_steps(carousel_angle(self.config, spot, 1)) == degrees_to_steps(carousel_angle(self.config, spot, 2)):
                raise ValidationError(f"{spot}: P1 and P2 must have distinct carousel targets.")
        with self._operation("overshoot calibration", require_calibration=False):
            previous = self.config.carousel_overshoot_deg
            previous_secondary = self.config.carousel_secondary_overshoot_deg
            generation = self._abort_generation
            trials = []
            self._overshoot_running = True
            def cancelled():
                return (generation != self._abort_generation or self.state.abort_requested
                        or not self.grbl.connected or not self.state.position_trusted)
            def check():
                self.scale.check_communication()
                if cancelled():
                    raise OperationAborted("Overshoot calibration stopped.")
                if self.grbl.recovery_required:
                    raise RecoveryRequired("Controller requires recovery during calibration.")
            def position(spot, pipette):
                check()
                self.go_to_spot(spot, pipette)
                # Zero overshoot normally waits only for command acceptance.
                def motion_cancelled():
                    check()
                    return False
                self.grbl.wait_for_motion_complete(cancelled=motion_cancelled)
                check()
            total = len(pairs) * len(options.spots) * options.repetitions * 2
            try:
                check()
                log.info("OVERSHOOT_START deadline_s=%g stability_hold_s=%g spots=%s pairs_A_B=%s repetitions=%d",
                         options.deadline_s, dwell, options.spots, pairs, options.repetitions)
                for spot in options.spots:
                    for angle, secondary in pairs:
                        self.config.carousel_overshoot_deg = angle
                        self.config.carousel_secondary_overshoot_deg = secondary
                        self.overshoot_progress = f"Positioning {spot} at P1; testing A={angle:g}°, B={secondary:g}°"
                        position(spot, PipetteId.P1)
                        for repetition in range(options.repetitions):
                            for pipette, direction in ((PipetteId.P2, "P1→P2"), (PipetteId.P1, "P2→P1")):
                                self.overshoot_progress = f"Trial {len(trials)+1}/{total}: {spot} {direction}, A={angle:g}°, B={secondary:g}°, repeat {repetition+1}"
                                elapsed, outcome = None, "error"
                                try:
                                    position(spot, pipette)
                                    self.overshoot_progress = (f"Trial {len(trials)+1}/{total}: {spot} {direction}, A={angle:g}°, B={secondary:g}°; "
                                                               f"motion complete — waiting for scale (hold {dwell:g}s, deadline {options.deadline_s:g}s)")
                                    log.info("OVERSHOOT_SCALE_WAIT spot=%s direction=%s A_deg=%g B_deg=%g hold_s=%g deadline_s=%g",
                                             spot, direction, angle, secondary, dwell, options.deadline_s)
                                    elapsed = self.scale.measure_stabilization(dwell, options.deadline_s, cancelled)
                                    check()
                                    outcome = "success"
                                except ScaleUnstable:
                                    check()
                                    outcome = "timeout"
                                except OperationAborted:
                                    outcome = "cancelled"
                                    raise
                                finally:
                                    trial = OvershootTrial(spot, direction, angle, elapsed, outcome, secondary)
                                    trials.append(trial)
                                    log.info("OVERSHOOT_TRIAL spot=%s direction=%s A_deg=%g B_deg=%g stabilization_s=%s outcome=%s deadline_s=%g",
                                             spot, direction, angle, secondary, elapsed, outcome, options.deadline_s)
                check()
                result = rank_trials(options, trials)
                self.overshoot_result = result
                self.overshoot_progress = "Completed; previous setting restored. Apply and save explicitly." if result.recommendation_deg is not None else "No reliable candidate across all spots and directions; previous setting restored."
                log.info("OVERSHOOT_RESULT %s", result)
                return result
            except Exception:
                self.overshoot_progress = "Stopped or failed; previous setting restored. Recover GRBL connection, then Home all axes before restarting."
                self._fault_hold("Overshoot calibration interrupted; Home is required.")
                raise
            finally:
                self.config.carousel_overshoot_deg = previous
                self.config.carousel_secondary_overshoot_deg = previous_secondary
                self._overshoot_running = False

    def apply_overshoot_recommendation(self):
        from copy import deepcopy
        with self._command_lock:
            result = self.overshoot_result
            if result is None or result.recommendation_pair is None:
                raise ValidationError("No reliable overshoot recommendation is available.")
            if self.state.current_operation:
                raise ValidationError("Wait for motion to finish before recording calibration.")
            if not self.calibration_file:
                raise ValidationError("No calibration settings file is configured.")
            candidate = deepcopy(self.config)
            values = dict(zip(("carousel_overshoot_deg", "carousel_secondary_overshoot_deg"), result.recommendation_pair))
            for key, value in values.items():
                spec = next(m for m in measurements(()) if m.key == key)
                set_value(candidate, key, spec.validate(value))
            errors = geometry_errors(candidate)
            if errors:
                raise ValidationError("; ".join(errors))
            # Save both values atomically before publishing either in memory.
            save_calibration(candidate, self.calibration_file, self.inventory.vessels)
            for key, value in values.items():
                set_value(self.config, key, value)
            log.info("OPERATOR_CONFIRMED_OVERSHOOT_PAIR %s", values)
            return result.recommendation_pair

    def stop_overshoot_calibration(self):
        """Out-of-band fault hold; do not reset gravity-loaded axes."""
        self._abort_generation += 1
        self.state.abort_requested = True
        self.overshoot_result = None
        try:
            if self.grbl.connected:
                self.grbl.hold_for_fault()
        finally:
            self.state.invalidate_position("Overshoot calibration stopped; fault hold requested. Home/recovery required.")
            self.state.operation_state = OperationState.RECOVERY_REQUIRED

    def _move_z_safe(self) -> None:
        position = self.config.z.safe_position_mm
        if position is None:
            raise MissingCalibration("Z safe position has not been calibrated.")
        self._move_z(position, self.config.z.motion_speed_mm_min)

    def _move_z(self, position, feed=None):
        if self.state.abort_requested:
            raise OperationAborted("Z movement cancelled by Abort.")
        error = self.config.z.target_error(position)
        if error:
            raise ValidationError(error)
        self.grbl.move_absolute(Axis.Z.value, position, feed)
        self.state.positions[Axis.Z.value] = position

    def _pipette_config(self, pipette: PipetteId):
        return self.config.pipette_1 if pipette is PipetteId.P1 else self.config.pipette_2

    def _travel_for_volume(self, volume_ul: float, pipette: PipetteId) -> float:
        cfg = self._pipette_config(pipette)
        if not math.isfinite(volume_ul) or volume_ul < 0 or volume_ul > cfg.max_volume_ul + 1e-9:
            raise ValidationError(f"{cfg.name} cannot handle {volume_ul:g} µL in one cycle.")
        if cfg.contact_position_mm is None:
            raise MissingCalibration(f"{cfg.name} contact position has not been calibrated.")
        if cfg.volume_per_step_ul is None:
            raise MissingCalibration(f"{cfg.name} volume-per-step calibration has not been calibrated.")
        if cfg.max_travel_mm is None:
            raise MissingCalibration(f"{cfg.name} maximum safe travel has not been calibrated.")
        if not math.isfinite(cfg.volume_per_step_ul) or cfg.volume_per_step_ul <= 0:
            raise ValidationError("Volume per step must be finite and positive.")
        if not math.isfinite(cfg.steps_per_mm) or cfg.steps_per_mm <= 0:
            raise ValidationError("Driver steps/mm must be finite and positive.")
        steps = round(volume_ul / cfg.volume_per_step_ul)
        if volume_ul > 0 and steps == 0:
            raise ValidationError(f"{cfg.name} requested volume rounds to zero driver steps.")
        if steps * cfg.volume_per_step_ul > cfg.max_volume_ul + 1e-9:
            raise ValidationError(f"{cfg.name} rounded stroke exceeds pipette volume capacity.")
        travel = steps / cfg.steps_per_mm
        if not math.isfinite(cfg.contact_position_mm):
            raise ValidationError("Pipette contact must be finite.")
        contact = round(cfg.contact_position_mm * cfg.steps_per_mm) / cfg.steps_per_mm
        for position in (contact, contact + cfg.work_direction * travel, cfg.home_position_mm):
            error = cfg.target_error(position)
            if error:
                raise ValidationError(f"{cfg.name} requested travel exceeds maximum safe travel: {error}")
        return travel

    def _move_pipette(self, cfg, position, feed=None):
        if self.state.abort_requested:
            raise OperationAborted(f"{cfg.axis} movement cancelled by Abort.")
        error = cfg.target_error(position)
        if error:
            raise ValidationError(error)
        self.grbl.move_absolute(cfg.axis, position, feed)
        self.state.positions[cfg.axis] = position

    def _await_motion(self, axis, target):
        generation = self._abort_generation
        try:
            self.grbl.wait_for_axis_position(axis, target, cancelled=lambda:
                self.state.abort_requested or generation != self._abort_generation or not self.grbl.connected)
        except Exception:
            self._fault_hold(f"Could not confirm {axis} motion completion.")
            raise

    def _aspirate_motion(self, volume_ul: float, pipette: PipetteId) -> None:
        cfg = self._pipette_config(pipette)
        travel = self._travel_for_volume(volume_ul, pipette)
        contact = round(cfg.contact_position_mm * cfg.steps_per_mm) / cfg.steps_per_mm
        self._move_pipette(cfg, contact)
        self._move_pipette(cfg, contact + cfg.work_direction * travel, cfg.speed_mm_min)
        self._move_pipette(cfg, cfg.home_position_mm, cfg.speed_mm_min)
        self._await_motion(cfg.axis, cfg.home_position_mm)
        self.state.pipette_contents_ul[pipette.value] += round(travel * cfg.steps_per_mm) * cfg.volume_per_step_ul

    def _dispense_motion(self, volume_ul: float, pipette: PipetteId) -> float:
        held = self.state.pipette_contents_ul[pipette.value]
        if volume_ul > held + 1e-9:
            raise ValidationError(f"{pipette.value} contains only {held:g} µL.")
        cfg = self._pipette_config(pipette)
        travel = self._travel_for_volume(volume_ul, pipette)
        actual_ul = round(travel * cfg.steps_per_mm) * cfg.volume_per_step_ul
        if actual_ul > held + 1e-9:
            raise ValidationError(f"{pipette.value} contains insufficient liquid for the rounded driver-step stroke.")
        contact = round(cfg.contact_position_mm * cfg.steps_per_mm) / cfg.steps_per_mm
        self._move_pipette(cfg, contact)
        self._move_pipette(cfg, contact + cfg.work_direction * travel, cfg.speed_mm_min)
        self._move_pipette(cfg, cfg.home_position_mm, cfg.speed_mm_min)
        self._await_motion(cfg.axis, cfg.home_position_mm)
        self.state.pipette_contents_ul[pipette.value] = max(0.0, held - actual_ul)
        return actual_ul

    def empty_pipette(self, pipette: PipetteId) -> float:
        with self._operation("empty_pipette"):
            return self._empty_pipette(pipette)

    def _empty_pipette(self, pipette: PipetteId) -> float:
        cfg = self._pipette_config(pipette)
        if cfg.max_travel_mm is None:
            raise MissingCalibration(f"{cfg.name} maximum safe travel has not been calibrated.")
        if not math.isfinite(cfg.steps_per_mm) or cfg.steps_per_mm <= 0:
            raise ValidationError("Driver steps/mm must be finite and positive.")
        error = cfg.target_error(cfg.home_position_mm)
        if error:
            raise ValidationError(error)
        endpoint = cfg.empty_position_mm
        if cfg.target_error(endpoint):
            raise ValidationError(cfg.target_error(endpoint))
        self._move_pipette(cfg, endpoint, cfg.speed_mm_min)
        self._move_pipette(cfg, cfg.home_position_mm, cfg.speed_mm_min)
        self._move_pipette(cfg, endpoint, cfg.speed_mm_min)
        self._move_pipette(cfg, cfg.home_position_mm, cfg.speed_mm_min)
        emptied = self.state.pipette_contents_ul[pipette.value]
        self._await_motion(cfg.axis, cfg.home_position_mm)
        self.state.pipette_contents_ul[pipette.value] = 0.0
        return emptied

    def _geometry(self, vessel):
        from autosampler.vessels.geometry import CylinderGeometry, ConicalTubeGeometry, ReservedCylinderGeometry

        a = self.config.aspiration
        if vessel.vessel_type is VesselType.VIAL_15ML:
            if a.vial_cone_height_mm == 0:
                return ReservedCylinderGeometry(
                    a.vial_internal_diameter_mm, vessel.capacity_ul, a.vial_volume_below_reference_ul
                )
            return ConicalTubeGeometry(
                a.vial_internal_diameter_mm, a.vial_cone_height_mm,
                a.vial_internal_height_mm, vessel.capacity_ul,
            )
        return CylinderGeometry(a.beaker_internal_diameter_mm, vessel.capacity_ul)

    def aspiration_z(self, vessel, pipette: PipetteId, remaining_ul=None) -> float:
        """Calculate using the vessel family; P1 and P2 tips share a height."""
        if vessel.category is VesselCategory.WASTE:
            raise ValidationError("Cannot aspirate from an untracked Dump vessel.")
        issues = self.calibration_issues()
        if issues:
            raise MissingCalibration("; ".join(issues))
        a = self.config.aspiration
        family = "vial" if vessel.vessel_type is VesselType.VIAL_15ML else "beaker"
        bottom = getattr(a, f"{family}_bottom_mm")
        internal_height = getattr(a, f"{family}_internal_height_mm")
        geometry = self._geometry(vessel)
        liquid_height = geometry.liquid_height_mm(vessel.quantity_ul if remaining_ul is None else remaining_ul)
        if geometry.liquid_height_mm(vessel.quantity_ul) > internal_height or liquid_height > internal_height:
            raise ValidationError("Calculated liquid level exceeds calibrated vessel height.")
        height = liquid_height - self.config.transfer.aspiration_immersion_depth_mm
        if height < a.bottom_clearance_mm or height >= liquid_height:
            raise ValidationError("Insufficient liquid height for immersion and safe bottom clearance.")
        target = bottom + a.z_up_direction * height
        if self.config.z.target_error(target):
            raise ValidationError(self.config.z.target_error(target))
        return target

    @contextmanager
    def _draw_accounting(self, source_id, pipette):
        progress = {"started": False}
        try:
            yield progress
        except Exception:
            if progress["started"]:
                self.inventory.unverified_sources.add(source_id)
                self.inventory.dump_required.add(pipette.value)
                try:
                    self.store.save(self.inventory)
                except Exception:
                    log.exception("Could not persist uncertain liquid state; verify source and empty tip before reuse")
            raise

    def draw(self, source_id: str, requested_ul: float, pipette: PipetteId, buffer_percent: float | None = None) -> float:
        with self._operation("draw"), self._draw_accounting(source_id, pipette) as progress:
            if self.inventory.dump_required:
                raise ValidationError("Pipette liquid is unverified; Dump the affected pipette before drawing again.")
            if source_id in self.inventory.unverified_sources:
                raise ValidationError("Source quantity is unverified; confirm and Apply vessel setup before drawing.")
            source = self.inventory.get(source_id)
            if source.category is VesselCategory.WASTE:
                raise ValidationError("Cannot aspirate from an untracked Dump vessel.")
            source.validate()
            if not math.isfinite(requested_ul) or requested_ul <= 0:
                raise ValidationError("Draw volume must be finite and greater than zero.")
            buffer = self.config.transfer.default_draw_buffer_percent if buffer_percent is None else buffer_percent
            if buffer is None:
                raise MissingCalibration("Default draw buffer percentage has not been calibrated.")
            if not math.isfinite(buffer) or buffer < 0:
                raise ValidationError("Draw buffer must be finite and non-negative.")
            target = requested_ul * (1 + buffer / 100)
            if source.quantity_ul < target:
                raise ValidationError(f"{source.name} contains insufficient liquid for draw plus buffer.")
            if source.vessel_type is VesselType.VIAL_15ML:
                reserve = self.config.aspiration.vial_volume_below_reference_ul
                if reserve is not None and source.quantity_ul - target < reserve:
                    raise ValidationError("Draw plus buffer would consume the vial reserve below the height reference.")
            tolerance = self.config.transfer.tolerance_ul
            attempts = self.config.transfer.max_correction_attempts
            minimum = self.config.transfer.minimum_correction_volume_ul
            immersion = self.config.transfer.aspiration_immersion_depth_mm
            if tolerance is None or attempts is None or minimum is None:
                raise MissingCalibration("Transfer correction limits have not been calibrated.")
            if immersion is None:
                raise MissingCalibration("Aspiration Z geometry has not been calibrated.")
            z_target = self.aspiration_z(source, pipette, source.quantity_ul - target)
            self._travel_for_volume(target, pipette)
            self.go_to_spot(source_id, pipette)
            self._await_motion("X", self.state.positions["X"])
            baseline = self.scale.read_stable(self.config.transfer.scale_stabilization_time_s)
            self.state.scale_reading = baseline
            self._move_z(z_target)
            self._await_motion("Z", z_target)
            correction = target
            measured = 0.0
            for attempt in range(1, attempts + 1):
                if self.state.abort_requested:
                    raise OperationAborted("Draw aborted.")
                if abs(correction) >= minimum:
                    if not progress["started"]:
                        # Write-ahead uncertainty survives interruption or application loss.
                        self.inventory.unverified_sources.add(source_id)
                        self.inventory.dump_required.add(pipette.value)
                        self.store.save(self.inventory)
                        progress["started"] = True
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
                    self._await_motion("Z", self.config.z.safe_position_mm)
                    self.inventory.unverified_sources.discard(source_id)
                    self.inventory.dump_required.discard(pipette.value)
                    self.store.save(self.inventory)
                    return measured
            raise CorrectionFailure(f"Draw did not converge after {attempts} correction attempts (error {correction:g} µL).")

    def dump(self, pipette: PipetteId) -> float:
        with self._operation("dump"):
            waste = self.inventory.require_special(VesselCategory.WASTE)
            self.go_to_spot(waste.location_id, pipette)
            if self.config.z.dispense_position_mm is None:
                raise MissingCalibration("Z dispense position has not been calibrated.")
            self._move_z(self.config.z.dispense_position_mm)
            self._await_motion("Z", self.config.z.dispense_position_mm)
            amount = self.empty_pipette(pipette)
            self.inventory.add_volume(waste.location_id, amount)
            self._move_z_safe()
            self._await_motion("Z", self.config.z.safe_position_mm)
            self.inventory.dump_required.discard(pipette.value)
            self.store.save(self.inventory)
            return amount

    def dispense(self, destination_id: str, amount_ul: float, pipette: PipetteId) -> None:
        with self._operation("dispense"):
            if pipette.value in self.inventory.dump_required:
                raise ValidationError("Pipette liquid is unverified; Dump is required.")
            destination = self.inventory.get(destination_id)
            destination.validate()
            if not math.isfinite(amount_ul) or amount_ul <= 0:
                raise ValidationError("Dispense volume must be finite and greater than zero.")
            cfg = self._pipette_config(pipette)
            travel = self._travel_for_volume(amount_ul, pipette)
            actual_ul = round(travel * cfg.steps_per_mm) * cfg.volume_per_step_ul
            held = self.state.pipette_contents_ul[pipette.value]
            if not math.isfinite(held) or max(actual_ul, amount_ul) > held + 1e-9:
                raise ValidationError(f"{pipette.value} contains insufficient liquid for the requested stroke.")
            if destination.category is not VesselCategory.WASTE and destination.quantity_ul + actual_ul > destination.capacity_ul:
                raise ValidationError(f"Dispense would overfill {destination.name}.")
            self.go_to_spot(destination_id, pipette)
            if self.config.z.dispense_position_mm is None:
                raise MissingCalibration("Z dispense position has not been calibrated.")
            self._move_z(self.config.z.dispense_position_mm)
            self._await_motion("Z", self.config.z.dispense_position_mm)
            actual_ul = self._dispense_motion(amount_ul, pipette)
            self.inventory.add_volume(destination_id, actual_ul)
            self._move_z_safe()
            self._await_motion("Z", self.config.z.safe_position_mm)
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
                self._move_z(self.config.z.dispense_position_mm)
                self._await_motion("Z", self.config.z.dispense_position_mm)
                returned = self.empty_pipette(pipette)
                self.inventory.add_volume(cleaning.location_id, returned)
                self._move_z_safe()
                self._await_motion("Z", self.config.z.safe_position_mm)
            self.store.save(self.inventory)

    def plan_transfer(self, amount_ul: float, prefer_p1_multicycle: bool | None = None, buffer_percent=None) -> TransferPlan:
        buffer = self.config.transfer.default_draw_buffer_percent if buffer_percent is None else buffer_percent
        if buffer is None:
            raise MissingCalibration("Default draw buffer percentage has not been calibrated.")
        return self.planner.plan(amount_ul, prefer_p1_multicycle, buffer)

    def transfer(self, source_id: str, destination_id: str, amount_ul: float,
                 confirm_multicycle: bool | None = None, prefer_p1_multicycle: bool | None = None,
                 buffer_percent: float | None = None) -> TransferPlan:
        self.inventory.validate_transfer(source_id, destination_id, amount_ul)
        plan = self.plan_transfer(amount_ul, prefer_p1_multicycle, buffer_percent)
        if plan.requires_confirmation and confirm_multicycle is not True:
            if confirm_multicycle is False:
                raise ValidationError("Multi-cycle transfer was declined.")
            raise ValidationError("This transfer plan requires operator confirmation.")
        with self._operation("transfer"):
            for cycle in plan.cycles_ul:
                self.draw(source_id, cycle, plan.pipette, buffer_percent=buffer_percent)
                self.dispense(destination_id, cycle, plan.pipette)
            return plan

    def apply_inventory(self, inventory):
        with self._command_lock:
            if self.state.current_operation:
                raise ValidationError("Wait for the current operation to finish.")
            # Editing vessel quantities cannot clear a required physical Dump.
            inventory.dump_required = set(self.inventory.dump_required)
            inventory.pipette_contents_ul = self.state.pipette_contents_ul
            self.store.save(inventory)
            self.inventory = inventory

    def reset_session(self, confirmed: bool = False) -> None:
        if not confirmed:
            raise ValidationError("Session reset requires explicit confirmation.")
        with self._command_lock:
            if self.state.current_operation:
                raise ValidationError("Wait for the current operation to finish.")
            dump_required = set(self.inventory.dump_required)
            self.inventory = self.store.reset()
            self.inventory.dump_required = dump_required
            self.inventory.pipette_contents_ul = self.state.pipette_contents_ul
            self.store.save(self.inventory)
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

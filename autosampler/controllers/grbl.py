from __future__ import annotations

import logging
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Callable

from autosampler.config import CAROUSEL_STEPS_PER_DEGREE, SerialConfig
from autosampler.controllers.interfaces import GrblController
from autosampler.errors import (
    ConnectionFailure,
    ControllerAlarm,
    ControllerError,
    SerialPermissionError,
    SerialTimeout,
    OperationAborted,
    RecoveryRequired,
)
from autosampler.serial_diagnostics import serial_permission_message
from autosampler.remote_serial import open_serial_port

log = logging.getLogger(__name__)


class SerialGrblController(GrblController):
    """Synchronous GRBL transport intended to run only on the machine worker."""

    HOMING_RESPONSE_TIMEOUT_S = 300.0
    # Nominal timing ceiling documented by this Mega 5X AVR firmware, not a
    # guarantee of mechanical safety or a sustainable rate on every build.
    MAX_PREFLIGHT_STEP_RATE_HZ = 30000.0

    def __init__(self, config: SerialConfig, *, homing_recovery_file: Path | None = None):
        self.config = config
        self._serial = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        if homing_recovery_file is None:
            from autosampler.app_paths import ApplicationPaths
            homing_recovery_file = ApplicationPaths.platform_default().config_dir / "homing-recovery.json"
        self._homing_recovery_file = Path(homing_recovery_file)
        self._abort_generation = 0
        self._needs_reconnect = False

    @property
    def connected(self) -> bool:
        return bool(self._serial and self._serial.is_open)

    @property
    def recovery_required(self) -> bool:
        return self._needs_reconnect or self._homing_recovery_file.exists()

    def connect(self) -> None:
        if self.connected:
            if self.recovery_required:
                raise RecoveryRequired("Controller fault is latched; connection retained. Secure gravity-loaded axes before deliberate reset/reconnection.")
            return
        if not self.config.port or not self.config.baud_rate:
            raise ConnectionFailure("GRBL serial port/baud rate is not configured.")
        if self._homing_recovery_file.exists():
            try:
                record = json.loads(self._homing_recovery_file.read_text(encoding="utf-8"))
                if record["port"] != self.config.port:
                    raise ValueError("Reconnect the original serial port recorded in homing-recovery.json.")
            except Exception as exc:
                raise RecoveryRequired(f"Cannot recover homing settings: {exc}") from exc
        try:
            self._serial = open_serial_port(
                self.config.port, baudrate=self.config.baud_rate, timeout=self.config.timeout_s,
                write_timeout=self.config.timeout_s,
            )
            if self._homing_recovery_file.exists():
                # USB/bridge reconnection does not necessarily reset the controller.
                self._serial.write(b"\x18")
            time.sleep(2.0)  # GRBL startup banner/reset interval
            self._serial.reset_input_buffer()
        except ImportError as exc:
            raise ConnectionFailure("pyserial is required for real hardware mode.") from exc
        except Exception as exc:
            if isinstance(exc, PermissionError) or "permission" in str(exc).lower() or "access is denied" in str(exc).lower():
                raise SerialPermissionError(serial_permission_message("GRBL", self.config.port)) from exc
            raise ConnectionFailure(f"Could not connect to GRBL: {exc}") from exc
        self._needs_reconnect = False
        try:
            self._restore_homing_settings()
        except Exception:
            # Preserve the live transport for diagnostics. Closing/reopening it
            # may reset some boards and release gravity-loaded axes again.
            self._needs_reconnect = True
            raise

    def disconnect(self) -> None:
        if self._serial:
            self._serial.close()
        self._serial = None

    def _require(self):
        if not self.connected:
            raise ConnectionFailure("GRBL is disconnected.")
        return self._serial

    def _line(self, command: str, response_timeout_s: float | None = None,
              on_response: Callable[[str], None] | None = None) -> str:
        with self._lock:
            serial_port = self._require()
            generation = self._abort_generation
            if on_response is None:
                log.info("GRBL > %s", command)
            deadline = (
                time.monotonic() + response_timeout_s
                if response_timeout_s is not None
                else None
            )
            try:
                with self._write_lock:
                    if self._needs_reconnect:
                        raise RecoveryRequired("Reconnect GRBL before sending further commands.")
                    if generation != self._abort_generation:
                        raise OperationAborted("Command cancelled by Abort.")
                    serial_port.write((command + "\n").encode("ascii"))
            except (OperationAborted, RecoveryRequired):
                raise
            except Exception as exc:
                self._connection_lost(exc)
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    raise SerialTimeout(f"Timed out waiting for GRBL response to {command!r}.")
                try:
                    raw = serial_port.readline()
                except Exception as exc:
                    self._connection_lost(exc)
                if generation != self._abort_generation:
                    raise OperationAborted("Command cancelled by Abort; reconnect before further commands.")
                if not raw:
                    if deadline is not None and time.monotonic() < deadline:
                        continue
                    raise SerialTimeout(f"Timed out waiting for GRBL response to {command!r}.")
                response = raw.decode("ascii", errors="replace").strip()
                if response.lower().startswith("grbl"):
                    log.error("GRBL restarted during %r: %s", command, response)
                    self._needs_reconnect = True
                    raise RecoveryRequired("Controller restarted; reconnect and Home are required.")
                if on_response is None:
                    log.info("GRBL < %s", response)
                low = response.lower()
                if low.startswith("error"):
                    raise ControllerError(response)
                if low.startswith("alarm"):
                    raise ControllerAlarm(response)
                if on_response is not None:
                    on_response(response)
                if low == "ok":
                    return response

    def status(self) -> str:
        with self._lock:
            port = self._require()
            deadline = time.monotonic() + self.config.timeout_s
            try:
                with self._write_lock:
                    port.write(b"?")
            except Exception as exc:
                self._connection_lost(exc)
            while time.monotonic() < deadline:
                try:
                    raw = port.readline()
                except Exception as exc:
                    self._connection_lost(exc)
                if not raw:
                    break
                report = raw.decode("ascii", errors="replace").strip()
                if report.lower().startswith("grbl"):
                    self._needs_reconnect = True
                    log.error("GRBL restart observed in status: %s", report)
                    return report
                if report.startswith("<") and report.endswith(">"):
                    return report
                if report.startswith("ALARM:"):
                    return report
                log.warning("GRBL status ignored outstanding response: %s", report)
            raise SerialTimeout("Timed out waiting for GRBL status.")

    def _ready_for_commands(self) -> None:
        self._require()
        if self._needs_reconnect or self._homing_recovery_file.exists():
            raise RecoveryRequired("Reconnect GRBL to restore homing settings before sending commands.")

    def wait_for_axis_position(self, axis: str, target: float, cancelled=None, timeout_s: float = 300.0) -> None:
        from autosampler.position_tracking import PositionTracking
        tracker = PositionTracking()
        deadline = time.monotonic() + timeout_s
        while True:
            if cancelled and cancelled():
                raise OperationAborted("Position wait cancelled.")
            with self._lock:
                self._ready_for_commands()
                # Move acknowledgement has already been consumed. Discard old
                # status reports before requesting arrival confirmation so an
                # earlier Idle at this coordinate cannot finish a later leg.
                try:
                    self._require().reset_input_buffer()
                except Exception as exc:
                    self._connection_lost(exc)
                report = self.status()
            if cancelled and cancelled():
                raise OperationAborted("Position wait cancelled.")
            self._ready_for_commands()
            if not tracker.ingest(report) or tracker.controller_state not in ("Idle", "Run", "Jog"):
                raise RecoveryRequired(f"Positioning interrupted: {report}")
            actual = tracker.work.get(axis)
            if tracker.controller_state == "Idle" and actual is not None and math.isclose(actual, target, abs_tol=.002, rel_tol=0):
                log.info("MOTION_CONFIRMED axis=%s target=%g reported_work=%g state=Idle", axis, target, actual)
                return
            if time.monotonic() >= deadline:
                raise SerialTimeout(f"No Idle confirmation at {axis}{target:g}; reported work position={actual}, report={report}")
            time.sleep(.05)

    def _read_settings(self) -> dict[int, float]:
        settings = {}
        def collect(line):
            if line.startswith("$") and "=" in line:
                key, value = line[1:].split("=", 1)
                try:
                    settings[int(key)] = float(value.split()[0])
                except (ValueError, IndexError):
                    pass
        self._line("$$", on_response=collect)
        return settings

    def validate_homing(self, axes: tuple[str, ...], search_rates: dict[str, float] | None = None) -> None:
        self._ready_for_commands()
        settings = self._read_settings()
        problems = []
        for axis in axes:
            if axis not in "XYZAB":
                raise ControllerError(f"Unsupported homing axis {axis!r}.")
            index = "XYZAB".index(axis)
            keys = (24, 25, 100 + index, 110 + index)
            if any(k not in settings or not math.isfinite(settings[k]) or settings[k] <= 0 for k in keys):
                raise ControllerError(f"Cannot validate {axis} homing: positive finite $24, $25, ${100 + index} and ${110 + index} settings are required. No homing started.")
            seek = (search_rates or {}).get(axis, settings[25])
            if not math.isfinite(seek) or seek <= 0:
                raise ControllerError(f"Invalid {axis} homing search rate. No homing started.")
            # Include both homing passes and normal rapid settings. Do not assume
            # that a firmware build's disabled step-rate check protects either.
            peak = max(seek, settings[24], settings[110 + index])
            frequency = settings[100 + index] * peak / 60.0
            if frequency > self.MAX_PREFLIGHT_STEP_RATE_HZ:
                problems.append(f"{axis}: {frequency:.0f} steps/s (${100 + index}={settings[100 + index]:g}, peak rate {peak:g}/min)")
        if problems:
            raise ControllerError(
                "Homing blocked before movement: " + "; ".join(problems) +
                f" exceeds the nominal Mega 5X {self.MAX_PREFLIGHT_STEP_RATE_HZ:g} steps/s ceiling. "
                "Review steps/unit, axis maximum rates and $24/$25 with gravity-loaded axes supported. "
                "No settings were changed and no reset was sent."
            )

    def _save_homing_settings(self, originals: dict[int, float]) -> None:
        path = self._homing_recovery_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump({"port": self.config.port, "settings": originals}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _restore_homing_settings(self) -> None:
        path = self._homing_recovery_file
        if not path.exists():
            return
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["port"] != self.config.port:
                raise ValueError("Recovery record belongs to another serial port; reconnect the original controller.")
            originals = {int(key): float(value) for key, value in record["settings"].items()}
            if not originals or set(originals) - {25, 110, 112} or any(not math.isfinite(v) or v <= 0 for v in originals.values()):
                raise ValueError("Invalid homing recovery settings")
            for key, value in originals.items():
                self._line(f"${key}={value:.9g}")
            restored = self._read_settings()
            if any(not math.isclose(restored.get(k, math.nan), v, rel_tol=1e-6, abs_tol=0.001) for k, v in originals.items()):
                raise ValueError("Controller did not confirm restored homing settings")
            path.unlink()
            log.info("Restored original homing settings: %s", originals)
        except Exception as exc:
            self._needs_reconnect = True
            raise RecoveryRequired(f"Homing settings restoration pending: {exc}") from exc

    def home(self, axis: str | None = None, *, search_rate: float | None = None) -> None:
        with self._lock:
            self._ready_for_commands()
            command = "$H" + (axis.upper() if axis else "")
            if search_rate is None:
                try:
                    self._line(command, response_timeout_s=self.HOMING_RESPONSE_TIMEOUT_S)
                except Exception as exc:
                    self._homing_failed(command, exc)
                return
            if command not in ("$HX", "$HZ") or not math.isfinite(search_rate) or search_rate <= 0:
                raise ControllerError("A positive custom homing search rate is supported only for X and Z.")
            settings = self._read_settings()
            maximum_key = 110 if command == "$HX" else 112
            required = ((22, 23, 24, 25, 27, 110, 130) if command == "$HX"
                        else (22, 24, 25, 27, 112))
            if any(k not in settings or not math.isfinite(settings[k]) for k in required):
                raise ControllerError(f"GRBL did not report the settings needed for {command[2]} homing.")
            if command == "$HX" and (settings[22] != 1 or int(settings[23]) & 1):
                raise ControllerError("Carousel homing requires $22=1 and positive X homing direction (X bit of $23 clear).")
            if settings[22] != 1:
                raise ControllerError("Homing requires $22=1.")
            positive_keys = (24, 25, 27, maximum_key) + ((130,) if command == "$HX" else ())
            if min(settings[k] for k in positive_keys) <= 0:
                raise ControllerError("Homing settings must be positive.")
            if command == "$HX":
                # This project's firmware uses HOMING_AXIS_SEARCH_SCALAR=1.5.
                revolution = 360 * CAROUSEL_STEPS_PER_DEGREE
                if settings[130] * 1.5 < revolution + settings[27]:
                    raise ControllerError(f"X homing search is shorter than one carousel revolution; $130 must be at least {(revolution + settings[27]) / 1.5:.3f} in this firmware.")
            changes = {25: search_rate}
            if settings[maximum_key] < search_rate:
                changes[maximum_key] = search_rate
            originals = {key: settings[key] for key, value in changes.items() if not math.isclose(settings[key], value, abs_tol=0.001)}
            if originals:
                # Persist BEFORE any EEPROM write, including writes with a lost acknowledgement.
                self._save_homing_settings(originals)
            try:
                for key in originals:
                    self._line(f"${key}={changes[key]:.9g}")
                self._line(command, response_timeout_s=self.HOMING_RESPONSE_TIMEOUT_S)
                if originals:
                    self._restore_homing_settings()
            except Exception as exc:
                # Do not restore EEPROM settings or reset while motion is uncertain.
                # Keep the recovery record for a deliberate, supported recovery.
                self._homing_failed(command, exc)

    def _homing_failed(self, command, exc):
        log.error("Homing %s did not complete: %s: %s", command, type(exc).__name__, exc)
        # A reset can release a gravity-loaded axis. Request hold without reset,
        # keep diagnostics connected, and never claim homing was stopped by '!'.
        if self.connected and not self._needs_reconnect:
            try:
                self.hold_for_fault()
            except Exception:
                log.exception("Could not request hold after %s failure", command)
        self._needs_reconnect = True
        raise RecoveryRequired(
            f"Homing {command} failed: {exc}. Remaining axes were not started. "
            "No automatic reset or disconnect was requested. Motion stop and motor holding torque are unconfirmed. "
            "Secure gravity-loaded axes before deliberate Abort/reset or reconnection. "
            "Reconnect GRBL before further commands, then explicitly Home again; calibration unlock cannot recover this failure."
        ) from exc

    def unlock(self) -> str:
        self._ready_for_commands()
        return self._line("$X")

    def send_command(self, command: str, on_response: Callable[[str], None] | None = None) -> str:
        if command == "?":
            response = self.status()
            if on_response is not None:
                on_response(response)
            return response
        self._ready_for_commands()
        homing = command.strip().upper() in ("$H", "$HX", "$HZ", "$HA", "$HB")
        if homing:
            normalized = command.strip().upper()
            self.validate_homing(tuple("XZAB") if normalized == "$H" else (normalized[2],))
        try:
            return self._line(command, response_timeout_s=self.HOMING_RESPONSE_TIMEOUT_S if homing else None,
                              on_response=on_response)
        except Exception as exc:
            if homing:
                self._homing_failed(command.strip().upper(), exc)
            raise

    def move_relative(self, axis: str, amount: float, feed: float | None = None) -> str:
        self._ready_for_commands()
        return self._line(f"G91 G0 {axis}{amount:g}" + (f" F{feed:g}" if feed else ""))

    def move_absolute(self, axis: str, position: float, feed: float | None = None) -> str:
        self._ready_for_commands()
        return self._line(f"G90 G0 {axis}{position:.10g}" + (f" F{feed:g}" if feed else ""))

    def set_axis_position(self, axis: str, position: float) -> str:
        self._ready_for_commands()
        # Set the active work-coordinate offset without moving the machine.
        return self._line(f"G10 L20 P1 {axis}{position:g}")

    def abort(self) -> None:
        # Realtime stop must not wait for a command's response/read lock.
        with self._write_lock:
            port = self._require()
            self._abort_generation += 1
            self._needs_reconnect = True
            port.write(b"!")  # feed hold first
            port.write(b"\x18")  # then soft reset; coordinates become untrusted

    def hold_for_fault(self) -> None:
        with self._write_lock:
            port = self._require()
            self._abort_generation += 1
            self._needs_reconnect = True
            port.write(b"!")
        log.warning("FAULT HOLD requested; no reset sent. Homing may ignore hold; motion/holding torque are not confirmed.")

    def _connection_lost(self, exc):
        port, self._serial = self._serial, None
        try:
            if port:
                port.close()
        except Exception:
            pass
        raise ConnectionFailure(f"GRBL serial device disconnected: {exc}") from exc

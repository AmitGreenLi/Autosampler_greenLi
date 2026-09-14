from __future__ import annotations

import logging
import threading
import time

from autosampler.config import SerialConfig
from autosampler.controllers.interfaces import GrblController
from autosampler.errors import (
    ConnectionFailure,
    ControllerAlarm,
    ControllerError,
    SerialPermissionError,
    SerialTimeout,
)
from autosampler.serial_diagnostics import serial_permission_message

log = logging.getLogger(__name__)


class SerialGrblController(GrblController):
    """Synchronous GRBL transport intended to run only on the machine worker."""

    HOMING_RESPONSE_TIMEOUT_S = 300.0

    def __init__(self, config: SerialConfig):
        self.config = config
        self._serial = None
        self._lock = threading.RLock()

    @property
    def connected(self) -> bool:
        return bool(self._serial and self._serial.is_open)

    def connect(self) -> None:
        if not self.config.port or not self.config.baud_rate:
            raise ConnectionFailure("GRBL serial port/baud rate is not configured.")
        try:
            import serial
            self._serial = serial.Serial(
                self.config.port, self.config.baud_rate, timeout=self.config.timeout_s
            )
            time.sleep(2.0)  # GRBL startup banner/reset interval
            self._serial.reset_input_buffer()
        except ImportError as exc:
            raise ConnectionFailure("pyserial is required for real hardware mode.") from exc
        except Exception as exc:
            if isinstance(exc, PermissionError) or "permission" in str(exc).lower() or "access is denied" in str(exc).lower():
                raise SerialPermissionError(serial_permission_message("GRBL", self.config.port)) from exc
            raise ConnectionFailure(f"Could not connect to GRBL: {exc}") from exc

    def disconnect(self) -> None:
        if self._serial:
            self._serial.close()
        self._serial = None

    def _require(self):
        if not self.connected:
            raise ConnectionFailure("GRBL is disconnected.")
        return self._serial

    def _line(self, command: str, response_timeout_s: float | None = None) -> str:
        with self._lock:
            serial_port = self._require()
            log.info("GRBL > %s", command)
            deadline = (
                time.monotonic() + response_timeout_s
                if response_timeout_s is not None
                else None
            )
            try:
                serial_port.write((command + "\n").encode("ascii"))
            except Exception as exc:
                self._connection_lost(exc)
            while True:
                try:
                    raw = serial_port.readline()
                except Exception as exc:
                    self._connection_lost(exc)
                if not raw:
                    if deadline is not None and time.monotonic() < deadline:
                        continue
                    raise SerialTimeout(f"Timed out waiting for GRBL response to {command!r}.")
                response = raw.decode("ascii", errors="replace").strip()
                log.info("GRBL < %s", response)
                low = response.lower()
                if low == "ok":
                    return response
                if low.startswith("error"):
                    raise ControllerError(response)
                if low.startswith("alarm"):
                    raise ControllerAlarm(response)

    def status(self) -> str:
        with self._lock:
            port = self._require()
            try:
                port.write(b"?")
                raw = port.readline()
            except Exception as exc:
                self._connection_lost(exc)
            if not raw:
                raise SerialTimeout("Timed out waiting for GRBL status.")
            return raw.decode("ascii", errors="replace").strip()

    def home(self, axis: str | None = None) -> None:
        self._line(
            "$H" + (axis.upper() if axis else ""),
            response_timeout_s=self.HOMING_RESPONSE_TIMEOUT_S,
        )

    def unlock(self) -> str:
        return self._line("$X")

    def move_relative(self, axis: str, amount: float, feed: float | None = None) -> str:
        return self._line(f"G91 G0 {axis}{amount:g}" + (f" F{feed:g}" if feed else ""))

    def move_absolute(self, axis: str, position: float, feed: float | None = None) -> str:
        return self._line(f"G90 G0 {axis}{position:g}" + (f" F{feed:g}" if feed else ""))

    def set_axis_position(self, axis: str, position: float) -> str:
        # Set the active work-coordinate offset without moving the machine.
        return self._line(f"G10 L20 P1 {axis}{position:g}")

    def abort(self) -> None:
        with self._lock:
            port = self._require()
            port.write(b"!")  # feed hold first
            port.write(b"\x18")  # then soft reset; coordinates become untrusted

    def _connection_lost(self, exc):
        port, self._serial = self._serial, None
        try:
            if port:
                port.close()
        except Exception:
            pass
        raise ConnectionFailure(f"GRBL serial device disconnected: {exc}") from exc

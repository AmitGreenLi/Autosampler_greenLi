from __future__ import annotations

import re
import threading
import time

from autosampler.config import AandFzScaleConfig
from autosampler.controllers.interfaces import ScaleController
from autosampler.errors import ConnectionFailure, ScaleError, ScaleUnstable, SerialPermissionError
from autosampler.models import ScaleReading
from autosampler.serial_diagnostics import serial_permission_message

# A&D standard/CSV format: condition header, signed numeric value, three-character unit.
_AAND_WEIGHT = re.compile(
    r"^\s*(?P<header>[A-Za-z]{2})\s*,?\s*"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    r"\s*,?\s*(?P<unit>mg|kg|g)\s*$",
    re.IGNORECASE,
)


def parse_aand_weight(text: str) -> ScaleReading:
    """Parse A&D standard/CSV weight data and normalize the result to grams."""
    cleaned = text.strip().replace("\x00", "")
    if cleaned.upper().startswith("EC,"):
        raise ScaleError(f"A&D balance returned {cleaned}.")
    match = _AAND_WEIGHT.fullmatch(cleaned)
    if not match:
        raise ScaleError(f"Unsupported A&D weight response: {text!r}.")
    header = match.group("header").upper()
    if header not in {"ST", "US"}:
        raise ScaleError(f"A&D balance is not reporting a usable weight ({header}).")
    value = float(match.group("value"))
    unit = match.group("unit").lower()
    mass_g = {"mg": value / 1000.0, "g": value, "kg": value * 1000.0}[unit]
    return ScaleReading(mass_g=mass_g, stable=header == "ST")


class SerialScaleController(ScaleController):
    """A&D FZ-500i query-mode controller with one continuous background reader."""

    def __init__(self, config: AandFzScaleConfig, parser=parse_aand_weight):
        self.config = config
        self.parser = parser
        self._serial = None
        self._serial_lock = threading.RLock()
        self._condition = threading.Condition()
        self._latest: ScaleReading | None = None
        self._sequence = 0
        self._last_error: Exception | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def connected(self) -> bool:
        return bool(self._serial and self._serial.is_open and self._thread and self._thread.is_alive())

    @property
    def latest_reading(self) -> ScaleReading | None:
        with self._condition:
            return self._latest

    def connect(self) -> None:
        if not self.config.port or not self.config.baud_rate:
            raise ConnectionFailure("A&D FZ-500i serial port/baud rate is not configured.")
        try:
            import serial
            self._serial = serial.Serial(
                port=self.config.port,
                baudrate=self.config.baud_rate,
                bytesize=self.config.data_bits,
                parity=self.config.parity,
                stopbits=self.config.stop_bits,
                timeout=self.config.timeout_s,
                write_timeout=self.config.timeout_s,
            )
            # Cancel S/SIR query streaming if previously enabled; normal reads use Q.
            self._serial.reset_input_buffer()
            self._serial.write(b"C" + self.config.terminator)
            self._serial.flush()
            time.sleep(0.05)
            self._serial.reset_input_buffer()
        except ImportError as exc:
            raise ConnectionFailure("pyserial is required for the A&D FZ-500i.") from exc
        except Exception as exc:
            self._serial = None
            if isinstance(exc, PermissionError) or "permission" in str(exc).lower() or "access is denied" in str(exc).lower():
                raise SerialPermissionError(
                    serial_permission_message("A&D scale", self.config.port)
                ) from exc
            raise ConnectionFailure(f"Could not connect to A&D FZ-500i: {exc}") from exc
        self._latest = None
        self._sequence = 0
        self._last_error = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader_loop, name="and-fz500i-reader", daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.config.timeout_s + 0.5))
        self._thread = None
        with self._serial_lock:
            port, self._serial = self._serial, None
            if port:
                try:
                    port.close()
                except Exception:
                    pass
        with self._condition:
            self._condition.notify_all()

    def read(self) -> ScaleReading:
        if not self.connected:
            raise ConnectionFailure("A&D FZ-500i is disconnected.")
        with self._condition:
            starting_sequence = self._sequence
            deadline = time.monotonic() + self.config.timeout_s + self.config.poll_interval_s
            while self._sequence <= starting_sequence and self.connected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            if self._sequence > starting_sequence and self._latest is not None:
                return self._latest
            if self._last_error:
                if isinstance(self._last_error, ConnectionFailure):
                    raise self._last_error
                raise ScaleError(str(self._last_error)) from self._last_error
        raise ScaleError("A&D FZ-500i read timed out.")

    def read_stable(self, stabilization_s: float | None, timeout_s: float = 10.0) -> ScaleReading:
        if stabilization_s is None:
            raise ScaleError("Scale stabilization time has not been calibrated.")
        deadline = time.monotonic() + timeout_s
        stable_since = None
        while time.monotonic() < deadline:
            reading = self.read()
            if reading.stable:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= stabilization_s:
                    return reading
            else:
                stable_since = None
        raise ScaleUnstable("A&D FZ-500i did not stabilize before timeout.")

    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            try:
                reading = self._query_once()
            except ConnectionFailure as exc:
                with self._condition:
                    self._last_error = exc
                    self._condition.notify_all()
                self._close_after_loss()
                return
            except ScaleError as exc:
                with self._condition:
                    self._last_error = exc
                    self._latest = None
                    self._condition.notify_all()
            else:
                with self._condition:
                    self._latest = reading
                    self._sequence += 1
                    self._last_error = None
                    self._condition.notify_all()
            self._stop.wait(self.config.poll_interval_s)

    def _query_once(self) -> ScaleReading:
        with self._serial_lock:
            port = self._serial
            if not port or not port.is_open:
                raise ConnectionFailure("A&D FZ-500i is disconnected.")
            try:
                port.reset_input_buffer()
                port.write(self.config.query_command + self.config.terminator)
                port.flush()
                raw = port.readline()
            except Exception as exc:
                raise ConnectionFailure(f"A&D FZ-500i serial device disconnected: {exc}") from exc
        if not raw:
            raise ScaleError("A&D FZ-500i query timed out.")
        try:
            text = raw.decode("ascii", errors="strict").strip("\r\n")
        except UnicodeDecodeError as exc:
            raise ScaleError(f"A&D FZ-500i returned non-ASCII data: {raw!r}.") from exc
        if text == "\x06":
            raise ScaleError("A&D FZ-500i returned an acknowledgement without weight data.")
        return self.parser(text)

    def _close_after_loss(self) -> None:
        with self._serial_lock:
            port, self._serial = self._serial, None
            try:
                if port:
                    port.close()
            except Exception:
                pass


AandFz500iScaleController = SerialScaleController

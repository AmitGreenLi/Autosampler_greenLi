from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from autosampler.models import ScaleReading


class GrblController(ABC):
    @property
    def recovery_required(self) -> bool:
        return False

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def status(self) -> str: ...

    def validate_homing(self, axes: tuple[str, ...], search_rates: dict[str, float] | None = None) -> None:
        """Preflight before a sequence; hardware controllers may reject unsafe settings."""
        return None

    def read_settings(self) -> dict[int, float]:
        """Read firmware settings without moving or changing controller settings."""
        values = {}
        def collect(line):
            if line.startswith("$") and "=" in line:
                key, value = line[1:].split("=", 1)
                try:
                    values[int(key)] = float(value.split()[0])
                except (ValueError, IndexError):
                    pass
        self.send_command("$$", on_response=collect)
        return values

    def wait_for_axis_position(self, axis: str, target: float, cancelled=None, timeout_s: float = 300.0) -> None:
        """Simulators may use the ordinary completion barrier; serial verifies coordinates."""
        self.wait_for_motion_complete(cancelled=cancelled, timeout_s=timeout_s)

    def wait_for_motion_complete(self, cancelled=None, timeout_s: float = 300.0) -> None:
        """An ordinary move's ok is only acceptance; require realtime Idle."""
        import time
        from autosampler.errors import OperationAborted, RecoveryRequired, SerialTimeout

        deadline = time.monotonic() + timeout_s
        while True:
            if cancelled and cancelled():
                raise OperationAborted("Motion wait cancelled by Abort/disconnection.")
            if self.recovery_required or not self.connected:
                raise RecoveryRequired("Controller is unavailable or requires recovery during motion wait.")
            report = self.status()
            if cancelled and cancelled():
                raise OperationAborted("Motion wait cancelled by Abort/disconnection.")
            if self.recovery_required:
                raise RecoveryRequired("Controller requires recovery during motion wait.")
            state = report[1:].split("|", 1)[0].rstrip(">") if report.startswith("<") and report.endswith(">") else "Unknown"
            if state == "Idle":
                return
            if state not in ("Run", "Jog"):
                raise RecoveryRequired(f"Motion did not complete normally: {report}")
            if time.monotonic() >= deadline:
                raise SerialTimeout("Timed out waiting for motion to complete.")
            time.sleep(0.05)

    @abstractmethod
    def home(self, axis: str | None = None, *, search_rate: float | None = None) -> None: ...

    @abstractmethod
    def unlock(self) -> str: ...

    @abstractmethod
    def send_command(self, command: str, on_response: Callable[[str], None] | None = None) -> str: ...

    @abstractmethod
    def move_relative(self, axis: str, amount: float, feed: float | None = None) -> str: ...

    @abstractmethod
    def move_absolute(self, axis: str, position: float, feed: float | None = None) -> str: ...

    @abstractmethod
    def set_axis_position(self, axis: str, position: float) -> str: ...

    @abstractmethod
    def abort(self) -> None: ...

    @abstractmethod
    def hold_for_fault(self) -> None:
        """Request hold and block commands without resetting or closing the port."""
        ...


class ScaleController(ABC):
    def check_communication(self) -> None:
        from autosampler.errors import ConnectionFailure
        if not self.connected:
            raise ConnectionFailure("Scale is disconnected.")

    def read_fresh(self, timeout_s: float, cancelled=None) -> ScaleReading:
        """Return a newly acquired sample. Streaming controllers override this barrier."""
        return self.read()

    def measure_stabilization(self, stabilization_s: float, timeout_s: float, cancelled=None, on_reading=None) -> float:
        """Time fresh stable flags sustained for the normal stabilization duration."""
        import time
        from autosampler.errors import OperationAborted, ScaleUnstable

        started = time.monotonic()
        deadline = started + timeout_s
        stable_since = None
        while True:
            if cancelled and cancelled():
                raise OperationAborted("Scale monitoring cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ScaleUnstable("Scale did not stabilize before the deadline.")
            reading = self.read_fresh(remaining, cancelled)
            now = time.monotonic()
            if cancelled and cancelled():
                raise OperationAborted("Scale monitoring cancelled.")
            if now > deadline:
                raise ScaleUnstable("Scale did not stabilize before the deadline.")
            if on_reading:
                on_reading(reading)
            if reading.stable:
                stable_since = now if stable_since is None else stable_since
                if now - stable_since >= stabilization_s:
                    return now - started
            else:
                stable_since = None

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @property
    @abstractmethod
    def latest_reading(self) -> ScaleReading | None: ...

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def read(self) -> ScaleReading: ...

    @abstractmethod
    def read_stable(self, stabilization_s: float | None, timeout_s: float = 10.0) -> ScaleReading: ...

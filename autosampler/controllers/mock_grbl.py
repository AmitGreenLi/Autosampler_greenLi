from __future__ import annotations

from typing import Callable

from autosampler.controllers.interfaces import GrblController
from autosampler.errors import ConnectionFailure, ControllerAlarm
from autosampler.models import Axis


class MockGrblController(GrblController):
    """Deterministic simulator. It never opens a serial port."""

    def __init__(self) -> None:
        self._connected = False
        self.positions = {axis.value: 0.0 for axis in Axis}
        self._work_offsets = {axis.value: 0.0 for axis in Axis}
        self.commands: list[str] = []
        self.alarm: str | None = None
        self.fail_home = False
        self.limit_pins: set[str] = set()
        self.homing_search_rates: list[tuple[str | None, float | None]] = []
        self.settings = {112: 100.0, 122: 10.0, 113: 100.0, 123: 10.0, 114: 100.0, 124: 10.0}

    @property
    def work_offsets(self):
        return self._work_offsets

    @work_offsets.setter
    def work_offsets(self, value):
        self._work_offsets = value

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True
        self.commands.append("CONNECT")

    def disconnect(self) -> None:
        self._connected = False
        self.commands.append("DISCONNECT")

    def _ready(self) -> None:
        if not self.connected:
            raise ConnectionFailure("GRBL is disconnected.")
        if self.alarm:
            raise ControllerAlarm(self.alarm)

    def status(self) -> str:
        self._ready()
        coords = ",".join(f"{a}:{v + self.work_offsets[a]:g}" for a, v in self.positions.items())
        offsets = ",".join(f"{a}:{v:g}" for a, v in self.work_offsets.items())
        pins = f"|Pn:{''.join(sorted(self.limit_pins))}" if self.limit_pins else ""
        return f"<Idle|MPos:{coords}|WCO:{offsets}{pins}>"

    def home(self, axis: str | None = None, *, search_rate: float | None = None) -> None:
        if not self.connected:
            raise ConnectionFailure("GRBL is disconnected.")
        command = "$H" + (axis.upper() if axis else "")
        self.commands.append(command)
        self.homing_search_rates.append((axis, search_rate))
        if self.fail_home:
            raise ControllerAlarm("Simulated homing failure.")
        if axis:
            self.positions[axis.upper()] = 0.0
            self.work_offsets[axis.upper()] = 0.0
        else:
            self.positions = {item.value: 0.0 for item in Axis}
            self.work_offsets = {item.value: 0.0 for item in Axis}
        self.alarm = None

    def unlock(self) -> str:
        if not self.connected:
            raise ConnectionFailure("GRBL is disconnected.")
        self.commands.append("$X")
        self.alarm = None
        return "ok"

    def send_command(self, command: str, on_response: Callable[[str], None] | None = None) -> str:
        self._ready()
        self.commands.append(command)
        if command == "$$":
            for key, value in self.settings.items():
                if on_response is not None:
                    on_response(f"${key}={value:g}")
            return "ok"
        response = self.status() if command == "?" else "ok (simulation; raw command not executed)"
        if on_response is not None:
            on_response(response)
        return response

    def move_relative(self, axis: str, amount: float, feed: float | None = None) -> str:
        self._ready()
        command = f"G91 G0 {axis}{amount:g}" + (f" F{feed:g}" if feed else "")
        self.commands.append(command)
        self.positions[axis] += float(amount)
        return "ok"

    def move_absolute(self, axis: str, position: float, feed: float | None = None) -> str:
        self._ready()
        command = f"G90 G0 {axis}{position:.10g}" + (f" F{feed:g}" if feed else "")
        self.commands.append(command)
        self.positions[axis] = float(position)
        return "ok"

    def set_axis_position(self, axis: str, position: float) -> str:
        self._ready()
        command = f"G10 L20 P1 {axis}{position:g}"
        self.commands.append(command)
        self.work_offsets[axis] += self.positions[axis] - float(position)
        self.positions[axis] = float(position)
        return "ok"

    def abort(self) -> None:
        if self.connected:
            self.commands.extend(["!", "CTRL-X"])
        self.alarm = "Abort/reset: homing required"

    def hold_for_fault(self) -> None:
        if self.connected:
            self.commands.append("!")
        self.alarm = "Fault hold: position and motion unconfirmed"

from __future__ import annotations

from autosampler.controllers.interfaces import GrblController
from autosampler.errors import ConnectionFailure, ControllerAlarm
from autosampler.models import Axis


class MockGrblController(GrblController):
    """Deterministic simulator. It never opens a serial port."""

    def __init__(self) -> None:
        self._connected = False
        self.positions = {axis.value: 0.0 for axis in Axis}
        self.commands: list[str] = []
        self.alarm: str | None = None
        self.fail_home = False
        self.limit_pins: set[str] = set()

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
        coords = ",".join(f"{a}:{v:g}" for a, v in self.positions.items())
        pins = f"|Pn:{''.join(sorted(self.limit_pins))}" if self.limit_pins else ""
        return f"<Idle|MPos:{coords}{pins}>"

    def home(self, axis: str | None = None) -> None:
        if not self.connected:
            raise ConnectionFailure("GRBL is disconnected.")
        command = "$H" + (axis.upper() if axis else "")
        self.commands.append(command)
        if self.fail_home:
            raise ControllerAlarm("Simulated homing failure.")
        if axis:
            self.positions[axis.upper()] = 0.0
        else:
            self.positions = {item.value: 0.0 for item in Axis}
        self.alarm = None

    def unlock(self) -> str:
        if not self.connected:
            raise ConnectionFailure("GRBL is disconnected.")
        self.commands.append("$X")
        self.alarm = None
        return "ok"

    def move_relative(self, axis: str, amount: float, feed: float | None = None) -> str:
        self._ready()
        command = f"G91 G0 {axis}{amount:g}" + (f" F{feed:g}" if feed else "")
        self.commands.append(command)
        self.positions[axis] += float(amount)
        return "ok"

    def move_absolute(self, axis: str, position: float, feed: float | None = None) -> str:
        self._ready()
        command = f"G90 G0 {axis}{position:g}" + (f" F{feed:g}" if feed else "")
        self.commands.append(command)
        self.positions[axis] = float(position)
        return "ok"

    def set_axis_position(self, axis: str, position: float) -> str:
        self._ready()
        command = f"G10 L20 P1 {axis}{position:g}"
        self.commands.append(command)
        self.positions[axis] = float(position)
        return "ok"

    def abort(self) -> None:
        if self.connected:
            self.commands.extend(["!", "CTRL-X"])
        self.alarm = "Abort/reset: homing required"

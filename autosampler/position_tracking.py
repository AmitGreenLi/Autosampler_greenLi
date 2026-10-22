"""GUI measurement state, deliberately independent of motion authorization."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from autosampler.errors import ValidationError


@dataclass
class PositionTracking:
    # GRBL Mega 5X reports positional vectors in X,Y,Z,A,B order (Y is unused).
    axis_order: str = "XYZAB"
    machine: dict[str, float] = field(default_factory=dict)
    work: dict[str, float] = field(default_factory=dict)
    offset: dict[str, float] = field(default_factory=dict)
    references: dict[tuple[str, str], float] = field(default_factory=dict)
    updated_at: float | None = None
    controller_state: str = "Unknown"
    report_units_supported: bool = True
    comparisons: list[dict] = field(default_factory=list)
    STALE_SECONDS = 2.0

    def invalidate(self):
        self.machine = {}
        self.work = {}
        self.offset = {}
        self.references.clear()
        self.updated_at = None
        self.controller_state = "Unknown"

    def invalidate_work(self):
        self.offset = {}
        self.work = {}
        self.references = {k: v for k, v in self.references.items() if k[0] != "work"}

    def _vector(self, text):
        try:
            parts = text.split(",")
            if any(":" in part for part in parts):
                result = {part.split(":")[0]: float(part.split(":")[1]) for part in parts}
                if set(result) - set(self.axis_order):
                    return {}
            else:
                if len(parts) not in (3, len(self.axis_order)):
                    return {}
                result = dict(zip(self.axis_order, map(float, parts)))
            return result if all(math.isfinite(v) for v in result.values()) else {}
        except (ValueError, IndexError):
            return {}

    def ingest(self, report: str):
        if not (report.startswith("<") and report.endswith(">")):
            return False
        fields = report[1:-1].split("|")
        self.controller_state = fields[0]
        data = dict(f.split(":", 1) for f in fields[1:] if ":" in f)
        machine = self._vector(data.get("MPos", ""))
        work = self._vector(data.get("WPos", ""))
        if "WCO" in data:
            offset = self._vector(data["WCO"])
            if offset != self.offset:
                self.invalidate_work()
            self.offset = offset
        if machine and work:
            offset = {a: machine[a] - work[a] for a in machine.keys() & work.keys()}
            if offset != self.offset:
                self.invalidate_work()
            self.offset = offset
        if machine and not work:
            work = {a: v - self.offset[a] for a, v in machine.items() if a in self.offset}
        elif work and not machine:
            machine = {a: v + self.offset[a] for a, v in work.items() if a in self.offset}
        self.machine, self.work = machine, work
        self.updated_at = time.monotonic() if machine or work else None
        return True

    def availability(self, connected, now=None):
        if not connected:
            return "Disconnected"
        if not self.report_units_supported:
            return "Unknown ($13=0 required)"
        if self.updated_at is None:
            return "Unknown"
        if (time.monotonic() if now is None else now) - self.updated_at > self.STALE_SECONDS:
            return "Stale"
        return "Live"

    def set_reference(self, coordinate, axes, connected):
        if self.availability(connected) != "Live":
            raise ValidationError("A fresh controller position is required to set a measurement reference.")
        values = getattr(self, coordinate)
        if any(axis not in values for axis in axes):
            raise ValidationError("Controller position is unknown for the selected axes / coordinate system.")
        for axis in axes:
            self.references[coordinate, axis] = values[axis]

    def displacement(self, coordinate, axis):
        value = getattr(self, coordinate).get(axis)
        reference = self.references.get((coordinate, axis))
        return None if value is None or reference is None else value - reference

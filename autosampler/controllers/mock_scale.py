from __future__ import annotations

import random

from autosampler.controllers.interfaces import ScaleController
from autosampler.errors import ConnectionFailure, ScaleError, ScaleUnstable
from autosampler.models import ScaleReading


class MockScaleController(ScaleController):
    def __init__(self, mass_g: float = 0.0, noise_g: float = 0.0):
        self._connected = False
        self.mass_g = mass_g
        self.noise_g = noise_g
        self.stable = True
        self.invalid = False
        self._queued: list[ScaleReading] = []
        self._latest: ScaleReading | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def latest_reading(self) -> ScaleReading | None:
        if not self.connected:
            return None
        return self._latest or ScaleReading(self.mass_g, self.stable)

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def queue(self, *readings: ScaleReading) -> None:
        self._queued.extend(readings)

    def read(self) -> ScaleReading:
        if not self.connected:
            raise ConnectionFailure("Scale is disconnected.")
        if self.invalid:
            raise ScaleError("Scale returned an invalid reading.")
        if self._queued:
            reading = self._queued.pop(0)
        else:
            reading = ScaleReading(self.mass_g + random.uniform(-self.noise_g, self.noise_g), self.stable)
        self._latest = reading
        return reading

    def read_stable(self, stabilization_s: float | None, timeout_s: float = 10.0) -> ScaleReading:
        reading = self.read()
        if not reading.stable:
            raise ScaleUnstable("Scale did not produce a stable reading.")
        return reading

from __future__ import annotations

from abc import ABC, abstractmethod

from autosampler.models import ScaleReading


class GrblController(ABC):
    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def status(self) -> str: ...

    @abstractmethod
    def home(self, axis: str | None = None) -> None: ...

    @abstractmethod
    def unlock(self) -> str: ...

    @abstractmethod
    def move_relative(self, axis: str, amount: float, feed: float | None = None) -> str: ...

    @abstractmethod
    def move_absolute(self, axis: str, position: float, feed: float | None = None) -> str: ...

    @abstractmethod
    def set_axis_position(self, axis: str, position: float) -> str: ...

    @abstractmethod
    def abort(self) -> None: ...


class ScaleController(ABC):
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

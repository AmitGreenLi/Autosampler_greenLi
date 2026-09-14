from __future__ import annotations

from abc import ABC, abstractmethod


class CarouselReferenceSensor(ABC):
    """Source-neutral reference interface; a future GPIO adapter can implement it."""

    @abstractmethod
    def consume_reference_event(self) -> bool: ...


class ManualReferenceSensor(CarouselReferenceSensor):
    def __init__(self):
        self._pending = False

    def record_event(self) -> None:
        self._pending = True

    def consume_reference_event(self) -> bool:
        pending, self._pending = self._pending, False
        return pending

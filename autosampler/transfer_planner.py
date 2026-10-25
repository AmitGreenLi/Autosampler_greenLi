from __future__ import annotations

from dataclasses import dataclass
import math

from autosampler.errors import ValidationError
from autosampler.models import PipetteId


@dataclass(frozen=True)
class TransferPlan:
    requested_ul: float
    pipette: PipetteId
    cycles_ul: tuple[float, ...]
    requires_confirmation: bool
    cumulative_actual_ul: float = 0.0

    @property
    def cumulative_planned_ul(self) -> float:
        return sum(self.cycles_ul)

    @property
    def remaining_ul(self) -> float:
        return max(0.0, self.requested_ul - self.cumulative_actual_ul)


class TransferPlanner:
    def __init__(self, p1_capacity_ul=200.0, p2_capacity_ul=10_000.0, p1_threshold_ul=1100.0):
        self.p1_capacity_ul = p1_capacity_ul
        self.p2_capacity_ul = p2_capacity_ul
        self.p1_threshold_ul = p1_threshold_ul

    @staticmethod
    def _cycles(total: float, capacity: float) -> tuple[float, ...]:
        full = int(total // capacity)
        remainder = total - full * capacity
        cycles = [capacity] * full
        if remainder > 1e-9:
            cycles.append(remainder)
        return tuple(cycles)

    def plan(self, amount_ul: float, prefer_p1_multicycle: bool | None = None, buffer_percent: float = 0.0) -> TransferPlan:
        if not math.isfinite(amount_ul) or amount_ul <= 0:
            raise ValidationError("Transfer amount must be greater than zero.")
        if not math.isfinite(buffer_percent) or buffer_percent < 0:
            raise ValidationError("Draw buffer must be finite and non-negative.")
        use_p1 = amount_ul <= self.p1_capacity_ul or (amount_ul < self.p1_threshold_ul and prefer_p1_multicycle is not False)
        pipette = PipetteId.P1 if use_p1 else PipetteId.P2
        capacity = (self.p1_capacity_ul if use_p1 else self.p2_capacity_ul) / (1 + buffer_percent / 100)
        cycles = self._cycles(amount_ul, capacity)
        return TransferPlan(amount_ul, pipette, cycles, len(cycles) > 1)

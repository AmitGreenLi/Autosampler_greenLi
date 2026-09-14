from __future__ import annotations

from dataclasses import dataclass

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

    def plan(self, amount_ul: float, prefer_p1_multicycle: bool | None = None) -> TransferPlan:
        if amount_ul <= 0:
            raise ValidationError("Transfer amount must be greater than zero.")
        if amount_ul <= self.p1_capacity_ul:
            return TransferPlan(amount_ul, PipetteId.P1, (float(amount_ul),), False)
        if amount_ul < self.p1_threshold_ul:
            if prefer_p1_multicycle is False:
                return TransferPlan(amount_ul, PipetteId.P2, (float(amount_ul),), False)
            return TransferPlan(amount_ul, PipetteId.P1, self._cycles(amount_ul, self.p1_capacity_ul), True)
        if amount_ul <= self.p2_capacity_ul:
            return TransferPlan(amount_ul, PipetteId.P2, (float(amount_ul),), False)
        return TransferPlan(amount_ul, PipetteId.P2, self._cycles(amount_ul, self.p2_capacity_ul), True)

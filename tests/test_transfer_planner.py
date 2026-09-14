import pytest

from autosampler.models import PipetteId
from autosampler.transfer_planner import TransferPlanner


@pytest.mark.parametrize("amount", [1, 199, 200])
def test_p1_single_cycle_boundaries(amount):
    plan = TransferPlanner().plan(amount)
    assert plan.pipette is PipetteId.P1
    assert plan.cycles_ul == (float(amount),)
    assert not plan.requires_confirmation


@pytest.mark.parametrize("amount,cycles", [(201, (200, 1)), (1099, (200, 200, 200, 200, 200, 99))])
def test_p1_multicycle_requires_confirmation(amount, cycles):
    plan = TransferPlanner().plan(amount)
    assert plan.pipette is PipetteId.P1
    assert plan.cycles_ul == cycles
    assert plan.requires_confirmation
    assert plan.cumulative_planned_ul == amount


def test_declining_p1_multicycle_selects_p2():
    plan = TransferPlanner().plan(750, prefer_p1_multicycle=False)
    assert plan.pipette is PipetteId.P2
    assert plan.cycles_ul == (750.0,)


@pytest.mark.parametrize("amount", [1100, 10_000])
def test_p2_single_cycle_boundaries(amount):
    plan = TransferPlanner().plan(amount)
    assert plan.pipette is PipetteId.P2
    assert not plan.requires_confirmation


def test_over_10ml_is_p2_multicycle():
    plan = TransferPlanner().plan(20_001)
    assert plan.cycles_ul == (10_000, 10_000, 1)
    assert plan.requires_confirmation


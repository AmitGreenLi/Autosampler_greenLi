"""Reusable functional command entry points around :class:`AutosamplerMachine`."""

from autosampler.models import Axis, PipetteId


def move_axis(machine, axis: Axis | str, amount: float, unit: str):
    return machine.move_axis(axis, amount, unit)


def go_to_spot(machine, location: str, pipette: PipetteId):
    return machine.go_to_spot(location, pipette)


def draw(machine, source: str, amount_ul: float, pipette: PipetteId, buffer_percent=None):
    return machine.draw(source, amount_ul, pipette, buffer_percent)


def dispense(machine, destination: str, amount_ul: float, pipette: PipetteId):
    return machine.dispense(destination, amount_ul, pipette)


def dump(machine, pipette: PipetteId):
    return machine.dump(pipette)


def clean(machine, pipette: PipetteId, cycles=None):
    return machine.clean(pipette, cycles)


def transfer(machine, source: str, destination: str, amount_ul: float, **options):
    return machine.transfer(source, destination, amount_ul, **options)

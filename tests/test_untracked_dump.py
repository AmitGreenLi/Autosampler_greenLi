import pytest

from autosampler.errors import ValidationError
from autosampler.models import PipetteId
from autosampler.vessels import Vessel, VesselCategory, VesselType


def test_old_dump_data_is_normalized_to_untracked():
    dump = Vessel.from_dict({
        "location_id": "B2", "number": 26, "name": "Old waste", "category": "waste",
        "vessel_type": "beaker_100ml", "quantity_ul": 99999, "density_g_ml": 1,
        "material_name": "Old material",
    })
    assert dump.quantity_ul is None
    assert dump.density_g_ml is None
    assert dump.material_name == ""


def test_dump_has_no_accumulated_volume_and_pipette_is_emptied(configured_machine):
    m = configured_machine
    m.state.pipette_contents_ul["P1"] = 120
    assert m.dump(PipetteId.P1) == 120
    assert m.state.pipette_contents_ul["P1"] == 0
    m.inventory.add_volume("waste", 1_000_000)
    dump = m.store.load().get("waste")
    assert dump.quantity_ul is None
    assert dump.density_g_ml is None


@pytest.mark.parametrize("command", ["draw", "transfer"])
def test_mixed_dump_cannot_be_aspiration_source(configured_machine, command):
    m = configured_machine
    before = list(m.grbl.commands)
    with pytest.raises(ValidationError, match="untracked Dump"):
        if command == "draw":
            m.draw("waste", 100, PipetteId.P1)
        else:
            m.transfer("waste", "dest", 100)
    assert m.grbl.commands == before


def test_dispense_to_dump_accepts_untracked_destination(configured_machine):
    m = configured_machine
    m.state.pipette_contents_ul["P1"] = 120
    m.dispense("waste", 100, PipetteId.P1)
    assert m.state.pipette_contents_ul["P1"] == 0
    assert m.inventory.get("waste").quantity_ul is None

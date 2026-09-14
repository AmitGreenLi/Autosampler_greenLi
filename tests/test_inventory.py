import pytest

from autosampler.errors import ValidationError
from autosampler.vessels import Inventory, Vessel, VesselCategory, VesselType


def test_capacity_and_available_volume_validation():
    source = Vessel("s", 1, "S", VesselCategory.INPUT, VesselType.VIAL_15ML, 100)
    dest = Vessel("d", 2, "D", VesselCategory.OUTPUT, VesselType.VIAL_15ML, 14_950)
    inventory = Inventory([source, dest])
    with pytest.raises(ValidationError, match="overfill"):
        inventory.validate_transfer("s", "d", 100)
    with pytest.raises(ValidationError, match="insufficient"):
        inventory.validate_transfer("s", "d", 101)


def test_invalid_density_rejected():
    with pytest.raises(ValidationError, match="density"):
        Inventory().add(Vessel("s", 1, "S", VesselCategory.INPUT, VesselType.VIAL_15ML, density_g_ml=0))


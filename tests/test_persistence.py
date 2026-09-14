import json

import pytest

from autosampler.errors import AutosamplerError
from autosampler.persistence import StateStore
from autosampler.vessels import Inventory, Vessel, VesselCategory, VesselType


def inventory():
    return Inventory([Vessel("V1", 1, "Sample", VesselCategory.INPUT, VesselType.VIAL_15ML, 1234, 0.997, "Water")])


def test_atomic_save_and_reload(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save(inventory())
    loaded = store.load().get("V1")
    assert loaded.quantity_ul == 1234
    assert loaded.density_g_ml == 0.997
    assert "position" not in (tmp_path / "state.json").read_text()


def test_missing_state_is_empty(tmp_path):
    assert not StateStore(tmp_path / "missing.json").load().vessels


def test_corrupt_state_reports_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not-json")
    with pytest.raises(AutosamplerError, match="Could not load"):
        StateStore(path).load()


def test_reset_retains_backup(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save(inventory())
    assert not store.reset().vessels
    assert (tmp_path / "state.json.bak").exists()


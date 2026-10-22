from copy import deepcopy

import pytest

from autosampler.calibration import FIRMWARE_SETTINGS, measurements, load_calibration
from autosampler.config import MachineConfig
from autosampler.errors import ValidationError, RecoveryRequired, OperationAborted


def test_read_firmware_updates_all_values_without_motion_or_trust(configured_machine, tmp_path):
    m = configured_machine
    m.calibration_file = tmp_path / "calibration.json"
    m.grbl.settings = {number: float(number) for number in FIRMWARE_SETTINGS.values()}
    before = list(m.grbl.commands)
    trust = m.state.position_trusted
    values = m.read_firmware_calibration()
    assert m.grbl.commands[len(before):] == ["$$"]
    assert m.state.position_trusted == trust
    assert values == {key: float(number) for key, number in FIRMWARE_SETTINGS.items()}
    restored = MachineConfig()
    load_calibration(restored, m.calibration_file, m.inventory.vessels)
    assert restored.z.motion_speed_mm_min == 112
    assert restored.pipette_2.acceleration_mm_s2 == 124
    assert not set(FIRMWARE_SETTINGS).intersection(spec.key for spec in measurements(m.inventory.vessels))


@pytest.mark.parametrize("bad", [None, 0, -1, float("nan"), float("inf")])
def test_failed_read_does_not_partially_update(configured_machine, bad):
    m = configured_machine
    before = deepcopy(m.config)
    if bad is None:
        del m.grbl.settings[124]
    else:
        m.grbl.settings[124] = bad
    with pytest.raises(ValidationError):
        m.read_firmware_calibration()
    assert m.config == before


def test_read_requires_connection_and_abort_cancels_import(configured_machine, monkeypatch):
    m = configured_machine
    before = deepcopy(m.config)
    original = m.grbl.read_settings
    def abort_during_read():
        values = original()
        m.abort()
        return values
    monkeypatch.setattr(m.grbl, "read_settings", abort_during_read)
    with pytest.raises(OperationAborted):
        m.read_firmware_calibration()
    assert m.config == before
    m.disconnect_grbl()
    with pytest.raises(RecoveryRequired):
        m.read_firmware_calibration()

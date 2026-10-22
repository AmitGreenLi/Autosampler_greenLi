from types import SimpleNamespace

import pytest

from autosampler.devices import (
    DeviceIdentity,
    DevicePreferenceStore,
    DeviceRole,
    SerialDeviceInfo,
    device_is_present,
    discover_serial_devices,
    resolve_identity,
)
from autosampler.errors import AmbiguousDevice, DeviceNotFound


def port(device, **metadata):
    values = dict(name=None, description=None, hwid=None, vid=None, pid=None,
                  serial_number=None, manufacturer=None, product=None, location=None)
    values.update(metadata)
    return SimpleNamespace(device=device, **values)


def test_discovery_with_no_devices():
    assert discover_serial_devices(lambda: []) == []


def test_one_unsaved_device_is_not_silently_selected():
    devices = discover_serial_devices(lambda: [port("COM4", description="Arduino")])
    resolution = resolve_identity(None, devices)
    assert resolution.selected is None
    assert not resolution.confident
    assert resolution.candidates == tuple(devices)


def test_serial_number_recognizes_same_device_on_changed_os_path():
    identity = DeviceIdentity(serial_number="ABC", vid=0x2341, pid=0x0042, last_known_port="COM4")
    moved = SerialDeviceInfo("/dev/ttyACM0", vid=0x2341, pid=0x0042, serial_number="ABC")
    resolution = resolve_identity(identity, [moved])
    assert resolution.selected == moved
    assert resolution.confident


def test_unique_serial_number_remains_primary_if_usb_pid_changes():
    identity = DeviceIdentity(serial_number="ABC", vid=1, pid=2)
    updated = SerialDeviceInfo("COM9", serial_number="ABC", vid=1, pid=3)
    assert resolve_identity(identity, [updated]).selected == updated


def test_vid_pid_product_match_without_serial_number():
    identity = DeviceIdentity(vid=0x1234, pid=0x5678, product="Scale")
    scale = SerialDeviceInfo("/dev/ttyUSB1", vid=0x1234, pid=0x5678, product="Scale")
    assert resolve_identity(identity, [scale]).selected == scale


def test_serialless_ch341_reselected_by_saved_usb_identity():
    selected = SerialDeviceInfo("/dev/ttyUSB0", vid=0x1A86, pid=0x7523, location="1-1.2")
    identity = DeviceIdentity.from_device(selected)
    assert resolve_identity(identity, [selected]).selected == selected

    moved_path = SerialDeviceInfo("/dev/ttyUSB1", vid=0x1A86, pid=0x7523, location="1-1.2")
    assert resolve_identity(identity, [moved_path]).selected == moved_path


def test_serialless_identical_adapters_are_matched_by_usb_location():
    grbl = SerialDeviceInfo("/dev/ttyUSB1", vid=0x1A86, pid=0x7523, location="1-1.2")
    scale = SerialDeviceInfo("/dev/ttyUSB0", vid=0x1A86, pid=0x7523, location="1-1.4")
    assert resolve_identity(DeviceIdentity.from_device(grbl), [scale, grbl]).selected == grbl
    assert resolve_identity(DeviceIdentity.from_device(scale), [scale, grbl]).selected == scale
    assert resolve_identity(DeviceIdentity.from_device(grbl), [scale]).selected is None


def test_vid_pid_without_location_is_not_enough_to_select_a_role():
    identity = DeviceIdentity(vid=0x1A86, pid=0x7523)
    device = SerialDeviceInfo("/dev/ttyUSB0", vid=0x1A86, pid=0x7523)
    assert resolve_identity(identity, [device]).selected is None


def test_ambiguous_identical_usb_metadata_requires_manual_selection():
    identity = DeviceIdentity(vid=1, pid=2, product="Adapter")
    devices = [
        SerialDeviceInfo("COM3", vid=1, pid=2, product="Adapter"),
        SerialDeviceInfo("COM5", vid=1, pid=2, product="Adapter"),
    ]
    resolution = resolve_identity(identity, devices)
    assert not resolution.confident and len(resolution.candidates) == 2
    with pytest.raises(AmbiguousDevice):
        resolution.require_unique()


def test_missing_metadata_and_last_port_are_not_confident_identity():
    identity = DeviceIdentity(last_known_port="COM4")
    resolution = resolve_identity(identity, [SerialDeviceInfo("COM4")])
    assert not resolution.confident
    with pytest.raises(DeviceNotFound):
        resolution.require_unique()


def test_manual_selection_persists_saved_identity(tmp_path):
    path = tmp_path / "devices.json"
    selected = SerialDeviceInfo("COM7", vid=10, pid=20, serial_number="S1", manufacturer="Maker")
    store = DevicePreferenceStore(path)
    store.associate(DeviceRole.GRBL, selected)
    loaded = DevicePreferenceStore(path).identity(DeviceRole.GRBL)
    assert loaded.serial_number == "S1"
    assert loaded.last_known_port == "COM7"


def test_serialless_usb_location_persists(tmp_path):
    store = DevicePreferenceStore(tmp_path / "devices.json")
    store.associate(
        DeviceRole.GRBL,
        SerialDeviceInfo("/dev/ttyUSB0", vid=0x1A86, pid=0x7523, location="1-1.2"),
    )
    loaded = DevicePreferenceStore(store.path).identity(DeviceRole.GRBL)
    assert loaded.location == "1-1.2"


def test_display_name_contains_useful_usb_metadata():
    device = SerialDeviceInfo("/dev/ttyACM0", product="Mega", vid=0x2341, pid=0x0042)
    assert "/dev/ttyACM0 — Mega — VID:PID 2341:0042" == device.display_name


def test_presence_check_does_not_depend_on_enumeration_order():
    devices = [SerialDeviceInfo("COM7"), SerialDeviceInfo("COM3")]
    assert device_is_present("COM3", reversed(devices))
    assert not device_is_present("COM4", devices)

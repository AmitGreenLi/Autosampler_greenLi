from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from autosampler.errors import AmbiguousDevice, DeviceNotFound


class DeviceRole(str, Enum):
    GRBL = "grbl"
    SCALE = "scale"


@dataclass(frozen=True)
class SerialDeviceInfo:
    device: str
    name: str | None = None
    description: str | None = None
    hwid: str | None = None
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    location: str | None = None

    @classmethod
    def from_port(cls, port) -> "SerialDeviceInfo":
        return cls(**{field: getattr(port, field, None) for field in cls.__dataclass_fields__})

    @property
    def display_name(self) -> str:
        label = self.product or self.description or self.name or "Serial device"
        usb = ""
        if self.vid is not None and self.pid is not None:
            usb = f" — VID:PID {self.vid:04X}:{self.pid:04X}"
        serial = f" — S/N {self.serial_number}" if self.serial_number else ""
        return f"{self.device} — {label}{usb}{serial}"


@dataclass(frozen=True)
class DeviceIdentity:
    serial_number: str | None = None
    vid: int | None = None
    pid: int | None = None
    manufacturer: str | None = None
    product: str | None = None
    last_known_port: str | None = None

    @classmethod
    def from_device(cls, device: SerialDeviceInfo) -> "DeviceIdentity":
        return cls(device.serial_number, device.vid, device.pid, device.manufacturer, device.product, device.device)

    def score(self, device: SerialDeviceInfo) -> int:
        """A positive score is identity evidence; port alone is never confident."""
        if self.serial_number:
            if device.serial_number != self.serial_number:
                return 0
            if self.vid is not None and self.pid is not None:
                return 100 if (device.vid, device.pid) == (self.vid, self.pid) else 90
            return 90
        if self.vid is not None and self.pid is not None:
            if (device.vid, device.pid) != (self.vid, self.pid):
                return 0
            expected = ((self.product, device.product), (self.manufacturer, device.manufacturer))
            for saved, found in expected:
                if saved and found and saved != found:
                    return 0
            metadata = sum(bool(saved and found == saved) for saved, found in expected)
            return 70 + metadata * 5 if any(saved for saved, _ in expected) else 60
        return 10 if self.last_known_port == device.device else 0


@dataclass(frozen=True)
class DeviceResolution:
    selected: SerialDeviceInfo | None
    candidates: tuple[SerialDeviceInfo, ...]
    confident: bool
    reason: str

    def require_unique(self) -> SerialDeviceInfo:
        if self.selected and self.confident:
            return self.selected
        if len(self.candidates) > 1:
            raise AmbiguousDevice("Multiple serial devices match the saved identity; select one manually.")
        raise DeviceNotFound("No confidently matching serial device was found; select one manually.")


def resolve_identity(identity: DeviceIdentity | None, devices: Iterable[SerialDeviceInfo]) -> DeviceResolution:
    devices = tuple(devices)
    if identity is None:
        return DeviceResolution(None, devices, False, "no saved identity")
    scored = [(identity.score(device), device) for device in devices]
    best = max((score for score, _ in scored), default=0)
    matches = tuple(device for score, device in scored if score == best and score > 0)
    if best >= 70 and len(matches) == 1:
        return DeviceResolution(matches[0], matches, True, "unique USB identity match")
    if matches:
        return DeviceResolution(None, matches, False, "ambiguous or port-only match")
    return DeviceResolution(None, (), False, "saved device is not present")


def discover_serial_devices(enumerator: Callable | None = None) -> list[SerialDeviceInfo]:
    if enumerator is None:
        try:
            from serial.tools.list_ports import comports
        except ImportError:
            return []
        enumerator = comports
    devices = [SerialDeviceInfo.from_port(port) for port in enumerator()]
    # On Linux a /dev/serial/by-id alias may refer to an already-listed tty.
    unique: dict[tuple, SerialDeviceInfo] = {}
    for device in devices:
        real_path = os.path.realpath(device.device) if device.device.startswith("/dev/") else device.device
        key = (device.serial_number, device.vid, device.pid) if device.serial_number else (
            device.location, device.vid, device.pid, real_path
        )
        existing = unique.get(key)
        if existing is None or ("/serial/by-id/" in device.device and "/serial/by-id/" not in existing.device):
            unique[key] = device
    return sorted(unique.values(), key=lambda item: item.display_name.casefold())


def device_is_present(device_path: str | None, devices: Iterable[SerialDeviceInfo]) -> bool:
    if not device_path:
        return False
    expected = os.path.realpath(device_path) if device_path.startswith("/dev/") else device_path
    return any(
        (os.path.realpath(item.device) if item.device.startswith("/dev/") else item.device) == expected
        for item in devices
    )


class DevicePreferenceStore:
    VERSION = 1

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.identities: dict[DeviceRole, DeviceIdentity] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != self.VERSION:
            raise ValueError("Unsupported device-preference version.")
        self.identities = {
            DeviceRole(role): DeviceIdentity(**identity)
            for role, identity in payload.get("roles", {}).items()
        }

    def identity(self, role: DeviceRole) -> DeviceIdentity | None:
        return self.identities.get(role)

    def associate(self, role: DeviceRole, device: SerialDeviceInfo) -> None:
        self.identities[role] = DeviceIdentity.from_device(device)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": self.VERSION, "roles": {role.value: asdict(identity) for role, identity in self.identities.items()}}
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

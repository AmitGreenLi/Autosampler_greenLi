from __future__ import annotations

import platform
import sys
from importlib.metadata import PackageNotFoundError, version

from autosampler.devices import DeviceRole, discover_serial_devices


def _version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "not installed"


def environment_diagnostics(paths, device_preferences, devices=None) -> dict:
    devices = discover_serial_devices() if devices is None else list(devices)
    return {
        "os": platform.system(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python": sys.version.split()[0],
        "pyside6": _version("PySide6"),
        "pyserial": _version("pyserial"),
        "application_data_directory": str(paths.data_dir),
        "log_directory": str(paths.log_dir),
        "grbl_mapping": _identity_dict(device_preferences.identity(DeviceRole.GRBL)),
        "scale_mapping": _identity_dict(device_preferences.identity(DeviceRole.SCALE)),
        "serial_devices": [device.display_name for device in devices],
    }


def format_diagnostics(report: dict) -> str:
    lines = []
    for key, value in report.items():
        if isinstance(value, list):
            value = "\n  " + "\n  ".join(value) if value else "none"
        lines.append(f"{key.replace('_', ' ').title()}: {value}")
    return "\n".join(lines)


def _identity_dict(identity):
    if identity is None:
        return "not assigned"
    return {
        "serial_number": identity.serial_number,
        "vid": identity.vid,
        "pid": identity.pid,
        "manufacturer": identity.manufacturer,
        "product": identity.product,
        "last_known_port": identity.last_known_port,
    }

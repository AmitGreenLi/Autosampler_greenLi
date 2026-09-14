from __future__ import annotations

import sys


def serial_permission_message(role: str, port: str, platform_name: str | None = None) -> str:
    """Return actionable, platform-specific serial permission guidance."""
    platform_name = platform_name or sys.platform
    prefix = f"Permission denied opening {role} device {port}."
    if platform_name.startswith("win"):
        return (
            f"{prefix} Windows usually reports this when another application has the COM port open. "
            "Close Arduino Serial Monitor, VS Code serial terminals/extensions, WinCT, PuTTY, or another "
            "autosampler instance; then unplug/reconnect the USB adapter and try again."
        )
    if platform_name.startswith("linux"):
        return (
            f"{prefix} Check that no other program is using the device and that the application user "
            "has serial-device permissions (commonly membership in the dialout group)."
        )
    return f"{prefix} Close other serial applications and verify this user's device permissions."

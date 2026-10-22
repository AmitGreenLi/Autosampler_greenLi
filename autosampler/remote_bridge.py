"""Run on the glovebox Pi over SSH to expose its USB serial ports.

Only the authenticated SSH caller can use this process. One process owns at
most one serial port and exits when the SSH session closes.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import asdict

from autosampler.devices import discover_serial_devices


def _reply(payload: dict) -> None:
    sys.stdout.buffer.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
    sys.stdout.buffer.flush()


def _handle(request: dict, opened):
    operation = request.get("op")
    if operation == "list":
        return {"devices": [asdict(device) for device in discover_serial_devices()]}, opened, True
    if operation == "open":
        if opened is not None:
            raise ValueError("A serial port is already open in this SSH session.")
        path = request.get("path")
        devices = discover_serial_devices()
        if not isinstance(path, str) or not any(
            os.path.realpath(device.device) == os.path.realpath(path) for device in devices
        ):
            raise ValueError("Select a serial device currently attached to the glovebox Pi.")
        import serial

        opened = serial.Serial(
            port=path,
            baudrate=int(request["baudrate"]),
            bytesize=int(request.get("bytesize", 8)),
            parity=str(request.get("parity", "N")),
            stopbits=int(request.get("stopbits", 1)),
            timeout=float(request.get("timeout", 2.0)),
            write_timeout=float(request.get("write_timeout", 2.0)),
            exclusive=True,
        )
        return {}, opened, False
    if opened is None:
        raise ValueError("Open a serial port first.")
    if operation == "write":
        data = base64.b64decode(request["data"], validate=True)
        return {"count": opened.write(data)}, opened, False
    if operation == "readline":
        return {"data": base64.b64encode(opened.readline()).decode("ascii")}, opened, False
    if operation == "reset_input_buffer":
        opened.reset_input_buffer()
        return {}, opened, False
    if operation == "flush":
        opened.flush()
        return {}, opened, False
    if operation == "close":
        opened.close()
        return {}, None, True
    raise ValueError(f"Unknown serial operation: {operation!r}.")


def main() -> int:
    opened = None
    try:
        for line in sys.stdin.buffer:
            try:
                request = json.loads(line)
                response, opened, finished = _handle(request, opened)
                _reply({"ok": True, **response})
                if finished:
                    return 0
            except Exception as exc:
                _reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        if opened is not None:
            opened.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

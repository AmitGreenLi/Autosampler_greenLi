"""Authenticated USB-network serial transport for the glovebox Pi."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import quote, unquote, urlsplit

from autosampler.devices import SerialDeviceInfo
from autosampler.errors import ConnectionFailure


DEFAULT_GLOVEBOX_TARGET = "dcm@10.12.194.1"
_TARGET = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.-]*$")
_REMOTE_COMMAND = "cd ~/autosampler && .venv/bin/python -m autosampler.remote_bridge"


def validate_target(target: str) -> str:
    target = target.strip()
    if not _TARGET.fullmatch(target):
        raise ValueError("Enter the glovebox SSH target as user@host or user@IP.")
    return target


def remote_uri(target: str, path: str) -> str:
    target = validate_target(target)
    if not path.startswith("/dev/"):
        raise ValueError("Choose a serial device under /dev/ on the glovebox Pi.")
    return f"ssh://{target}{quote(path, safe='/')}"


def split_remote_uri(uri: str) -> tuple[str, str]:
    parsed = urlsplit(uri)
    if parsed.scheme != "ssh" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Invalid glovebox serial device address.")
    target = validate_target(parsed.netloc)
    path = unquote(parsed.path)
    if not path.startswith("/dev/"):
        raise ValueError("Invalid glovebox serial device path.")
    return target, path


def _ssh_command(target: str) -> list[str]:
    return [
        "ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=3", "-o", "ServerAliveInterval=2",
        "-o", "ServerAliveCountMax=2", validate_target(target), _REMOTE_COMMAND,
    ]


def _request_bytes(request: dict) -> bytes:
    return json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"


def _decode_reply(raw: bytes) -> dict:
    if not raw:
        raise ConnectionFailure("Glovebox SSH bridge closed the connection.")
    try:
        reply = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectionFailure("Glovebox SSH bridge returned an invalid response.") from exc
    if not isinstance(reply, dict):
        raise ConnectionFailure("Glovebox SSH bridge returned an invalid response.")
    if not reply.get("ok"):
        raise ConnectionFailure(str(reply.get("error", "Glovebox serial request failed.")))
    return reply


def discover_remote_devices(target: str) -> list[SerialDeviceInfo]:
    """List all USB serial devices on the inner Pi without opening them."""
    target = validate_target(target)
    try:
        result = subprocess.run(
            _ssh_command(target), input=_request_bytes({"op": "list"}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConnectionFailure(f"Could not reach the glovebox Pi over SSH: {exc}") from exc
    if result.returncode or not result.stdout:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ConnectionFailure(
            f"Could not query the glovebox Pi over SSH: {detail or 'bridge unavailable'}. "
            "Check SSH keys and install the bridge on the inner Pi."
        )
    payload = _decode_reply(result.stdout.splitlines()[0])
    return [
        SerialDeviceInfo(**{**device, "device": remote_uri(target, device["device"])})
        for device in payload.get("devices", [])
    ]


class RemoteSerialPort:
    """The subset of pySerial used by the existing GRBL and scale controllers."""

    def __init__(self, uri: str, baudrate: int, timeout: float = 2.0,
                 bytesize: int = 8, parity: str = "N", stopbits: int = 1,
                 write_timeout: float | None = None):
        target, path = split_remote_uri(uri)
        self._lock = threading.RLock()
        self._write_pending = threading.Event()
        self.timeout = timeout
        self._open = False
        try:
            self._process = subprocess.Popen(
                _ssh_command(target), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ConnectionFailure(f"Could not start SSH to the glovebox Pi: {exc}") from exc
        try:
            self._exchange({
                "op": "open", "path": path, "baudrate": baudrate,
                "timeout": min(timeout, 0.05), "bytesize": bytesize, "parity": parity,
                "stopbits": stopbits,
                "write_timeout": timeout if write_timeout is None else write_timeout,
            })
            self._open = True
        except Exception:
            self.close()
            raise

    @property
    def is_open(self) -> bool:
        return self._open and self._process.poll() is None

    def _exchange(self, request: dict) -> dict:
        with self._lock:
            try:
                self._process.stdin.write(_request_bytes(request))
                self._process.stdin.flush()
                raw = self._process.stdout.readline()
            except (OSError, ValueError) as exc:
                raise ConnectionFailure(f"Glovebox SSH serial connection lost: {exc}") from exc
            if not raw:
                detail = ""
                if self._process.poll() is not None:
                    detail = self._process.stderr.read().decode("utf-8", errors="replace").strip()
                raise ConnectionFailure(
                    f"Glovebox SSH serial connection closed{': ' + detail if detail else ''}."
                )
            return _decode_reply(raw)

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise ConnectionFailure("Glovebox serial port is closed.")
        self._write_pending.set()
        try:
            reply = self._exchange({"op": "write", "data": base64.b64encode(data).decode("ascii")})
        finally:
            self._write_pending.clear()
        return int(reply["count"])

    def readline(self) -> bytes:
        if not self.is_open:
            raise ConnectionFailure("Glovebox serial port is closed.")
        deadline = time.monotonic() + self.timeout
        result = bytearray()
        while True:
            while self._write_pending.is_set():
                time.sleep(0.001)
            reply = self._exchange({"op": "readline"})
            result.extend(base64.b64decode(reply["data"], validate=True))
            if result.endswith(b"\n") or time.monotonic() >= deadline:
                return bytes(result)
            time.sleep(0)  # Let realtime writers acquire the exchange lock between slices.

    def reset_input_buffer(self) -> None:
        self._exchange({"op": "reset_input_buffer"})

    def flush(self) -> None:
        self._exchange({"op": "flush"})

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if process is None:
            return
        self._open = False
        if process.poll() is None:
            try:
                self._exchange({"op": "close"})
            except (ConnectionFailure, OSError, ValueError):
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()


def open_serial_port(port: str, **kwargs):
    if port.startswith("ssh://"):
        return RemoteSerialPort(port, **kwargs)
    import serial

    if os.name == "posix":
        kwargs.setdefault("exclusive", True)
    return serial.Serial(port=port, **kwargs)

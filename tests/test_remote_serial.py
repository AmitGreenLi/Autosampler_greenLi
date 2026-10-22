import base64
import io
import json
import subprocess
from types import SimpleNamespace

import pytest

from autosampler.devices import DeviceIdentity, SerialDeviceInfo, resolve_identity
from autosampler.remote_bridge import _handle
from autosampler.remote_serial import (
    RemoteSerialPort, discover_remote_devices, remote_uri, split_remote_uri,
)


def test_remote_uri_and_identity_do_not_cross_hosts():
    uri = remote_uri("dcm@10.12.194.1", "/dev/ttyUSB0")
    assert split_remote_uri(uri) == ("dcm@10.12.194.1", "/dev/ttyUSB0")
    selected = SerialDeviceInfo(uri, vid=0x1A86, pid=0x7523, location="1-1.2")
    identity = DeviceIdentity.from_device(selected)
    assert resolve_identity(identity, [selected]).selected == selected
    other = SerialDeviceInfo(remote_uri("dcm@10.12.194.3", "/dev/ttyUSB0"),
                             vid=0x1A86, pid=0x7523, location="1-1.2")
    assert resolve_identity(identity, [other]).selected is None
    with pytest.raises(ValueError):
        remote_uri("-oProxyCommand=bad", "/dev/ttyUSB0")


def test_remote_discovery_lists_inner_pi_serial_devices(monkeypatch):
    payload = {"ok": True, "devices": [{
        "device": "/dev/ttyUSB0", "name": None, "description": "USB2.0-Serial",
        "hwid": None, "vid": 0x1A86, "pid": 0x7523, "serial_number": None,
        "manufacturer": None, "product": None, "location": "1-1.2",
    }]}
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["request"] = json.loads(kwargs["input"])
        return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode() + b"\n", b"")

    monkeypatch.setattr("autosampler.remote_serial.subprocess.run", run)
    devices = discover_remote_devices("dcm@10.12.194.1")
    assert captured["request"] == {"op": "list"}
    assert "BatchMode=yes" in captured["command"]
    assert devices[0].device == "ssh://dcm@10.12.194.1/dev/ttyUSB0"
    assert devices[0].location == "1-1.2"


def test_inner_bridge_opens_and_uses_selected_serial_port(monkeypatch):
    class FakePort:
        is_open = True

        def __init__(self, **kwargs):
            self.arguments = kwargs
            self.writes = []

        def write(self, data):
            self.writes.append(data)
            return len(data)

        def readline(self):
            return b"ok\r\n"

        def reset_input_buffer(self):
            pass

        def flush(self):
            pass

        def close(self):
            self.is_open = False

    created = []

    def open_port(**kwargs):
        port = FakePort(**kwargs)
        created.append(port)
        return port

    monkeypatch.setattr("autosampler.remote_bridge.discover_serial_devices", lambda: [
        SerialDeviceInfo("/dev/ttyUSB0", vid=0x1A86, pid=0x7523)
    ])
    monkeypatch.setitem(__import__("sys").modules, "serial", SimpleNamespace(Serial=open_port))
    response, opened, finished = _handle({"op": "open", "path": "/dev/ttyUSB0", "baudrate": 115200}, None)
    assert response == {} and not finished
    assert opened.arguments["exclusive"] is True
    response, opened, _ = _handle({"op": "write", "data": base64.b64encode(b"?\n").decode()}, opened)
    assert response["count"] == 2 and created[0].writes == [b"?\n"]
    response, opened, _ = _handle({"op": "readline"}, opened)
    assert base64.b64decode(response["data"]) == b"ok\r\n"
    _, opened, finished = _handle({"op": "close"}, opened)
    assert opened is None and finished and not created[0].is_open


def test_outer_remote_serial_uses_same_read_write_interface(monkeypatch):
    replies = b"".join(json.dumps(reply).encode() + b"\n" for reply in (
        {"ok": True}, {"ok": True, "count": 1},
        {"ok": True, "data": base64.b64encode(b"<Idle>\r\n").decode()},
        {"ok": True}, {"ok": True}, {"ok": True},
    ))

    class RecordingBytesIO(io.BytesIO):
        def close(self):
            self.saved = self.getvalue()
            super().close()

    class FakeProcess:
        def __init__(self):
            self.stdin = RecordingBytesIO()
            self.stdout = io.BytesIO(replies)
            self.stderr = io.BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            self.returncode = 0

        def terminate(self):
            self.returncode = -15

    process = FakeProcess()
    monkeypatch.setattr("autosampler.remote_serial.subprocess.Popen", lambda *a, **k: process)
    port = RemoteSerialPort("ssh://dcm@10.12.194.1/dev/ttyUSB0", baudrate=115200)
    assert port.is_open
    assert port.write(b"?") == 1
    assert port.readline() == b"<Idle>\r\n"
    port.reset_input_buffer()
    port.flush()
    port.close()
    assert not port.is_open
    requests = [json.loads(line) for line in process.stdin.saved.splitlines()]
    assert [request["op"] for request in requests] == [
        "open", "write", "readline", "reset_input_buffer", "flush", "close",
    ]

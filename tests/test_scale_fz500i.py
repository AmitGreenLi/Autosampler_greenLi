import time

import pytest

from autosampler.config import AandFzScaleConfig, MachineConfig
from autosampler.controllers.scale import SerialScaleController, parse_aand_weight
from autosampler.errors import ScaleError


@pytest.mark.parametrize(
    "text,mass_g,stable",
    [
        ("ST,+00012.345 g", 12.345, True),
        ("US,-00000.125 g", -0.125, False),
        ("ST,+00123.000,mg", 0.123, True),
        ("ST,+00000.500 kg", 500.0, True),
    ],
)
def test_parse_aand_standard_and_csv_formats(text, mass_g, stable):
    reading = parse_aand_weight(text)
    assert reading.mass_g == pytest.approx(mass_g)
    assert reading.stable is stable


@pytest.mark.parametrize("text", ["OL,+99999999E+19 g", "EC, E02", "12.345", "garbage"])
def test_parse_rejects_overload_errors_and_wrong_format(text):
    with pytest.raises(ScaleError):
        parse_aand_weight(text)


def test_machine_config_uses_fz500i_factory_serial_profile():
    scale = MachineConfig().scale
    assert scale.baud_rate == 2400
    assert scale.data_bits == 7
    assert scale.parity == "E"
    assert scale.stop_bits == 1
    assert scale.terminator == b"\r\n"
    assert scale.query_command == b"Q"


def test_controller_queries_continuously_without_gui_thread(monkeypatch):
    class FakePort:
        def __init__(self):
            self.is_open = True
            self.writes = []

        def reset_input_buffer(self): pass
        def flush(self): pass
        def write(self, data): self.writes.append(data)
        def readline(self): return b"ST,+00012.345 g\r\n"
        def close(self): self.is_open = False

    fake = FakePort()
    serial_arguments = {}

    def open_serial(**kwargs):
        serial_arguments.update(kwargs)
        return fake

    import serial
    monkeypatch.setattr(serial, "Serial", open_serial)
    config = AandFzScaleConfig("COM7", 2400, timeout_s=0.1, poll_interval_s=0.01)
    controller = SerialScaleController(config)
    controller.connect()
    deadline = time.monotonic() + 0.5
    while controller.latest_reading is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert controller.latest_reading.mass_g == pytest.approx(12.345)
    assert serial_arguments["bytesize"] == 7
    assert serial_arguments["parity"] == "E"
    assert fake.writes[0] == b"C\r\n"
    assert b"Q\r\n" in fake.writes
    controller.disconnect()
    assert not controller.connected

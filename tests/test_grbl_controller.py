import pytest

from autosampler.config import SerialConfig
from autosampler.controllers.grbl import SerialGrblController
from autosampler.errors import SerialTimeout


class ScriptedSerial:
    def __init__(self, responses):
        self.is_open = True
        self.responses = iter(responses)
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def readline(self):
        return next(self.responses, b"")

    def close(self):
        self.is_open = False


def controller_with_responses(*responses):
    controller = SerialGrblController(SerialConfig("COM-test", 115200))
    controller._serial = ScriptedSerial(responses)
    return controller


def test_homing_waits_through_serial_read_intervals_for_final_ok():
    controller = controller_with_responses(b"", b"", b"ok\r\n")
    controller.home("X")
    assert controller._serial.writes == [b"$HX\n"]


def test_ordinary_command_retains_short_serial_timeout():
    controller = controller_with_responses(b"")
    with pytest.raises(SerialTimeout):
        controller.unlock()

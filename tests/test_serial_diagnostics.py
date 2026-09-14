from autosampler.serial_diagnostics import serial_permission_message


def test_windows_serial_permission_message_explains_busy_port():
    message = serial_permission_message("GRBL", "COM4", "win32")
    assert "another application" in message
    assert "Arduino Serial Monitor" in message
    assert "dialout" not in message


def test_linux_serial_permission_message_mentions_dialout():
    message = serial_permission_message("GRBL", "/dev/ttyACM0", "linux")
    assert "dialout" in message
    assert "COM port" not in message

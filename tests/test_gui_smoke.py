import pytest


def test_homing_blocks_duplicate_requests_and_abort_does_not_start_b(tmp_path, monkeypatch):
    import threading
    import time
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from autosampler.app_paths import ApplicationPaths
    from autosampler.gui import AutosamplerWindow
    from autosampler.main import build_runtime

    app = QApplication.instance() or QApplication([])
    m, paths, preferences = build_runtime(True, ApplicationPaths.under(tmp_path))
    w = AutosamplerWindow(m, paths, preferences, mock_mode=True)
    w.limit_timer.stop()
    entered, release = threading.Event(), threading.Event()
    original_home = m.grbl.home
    def home(axis=None, **kwargs):
        if axis == "A":
            entered.set()
            assert release.wait(3)
        return original_home(axis, **kwargs)
    monkeypatch.setattr(m.grbl, "home", home)
    try:
        m.connect_grbl()
        w._render_state(m.state)
        w.home_selected()
        # Reservation is immediate, even before the worker starts.
        assert not w.home_button.isEnabled()
        assert not w.calibration_unlock.isEnabled()
        assert not w.move_button.isEnabled()
        assert not w.manual_send_button.isEnabled()
        assert entered.wait(1)
        w._render_state(m.state)
        assert "Homing A (3/4)" in w.homing_progress_label.text()
        assert "elapsed" in w.connection_homing_progress.text()
        assert w.abort_button.isEnabled()
        assert not w._submit("home again", m.home)
        assert not w._submit("calibration unlock", m.unlock_for_calibration)
        w._poll_limit_status()
        assert w.worker.commands.empty()
        w.abort_button.click()
        release.set()
        deadline = time.monotonic() + 2
        while w._command_inflight and time.monotonic() < deadline:
            QTest.qWait(10)
            w.bridge.drain()
        assert w._command_inflight is None
        assert "$HB" not in m.grbl.commands
        assert m.grbl.commands.count("$HZ") == 1
        assert "$X" not in m.grbl.commands
        assert not m.state.position_trusted
    finally:
        release.set()
        w.close()
        app.processEvents()


def test_reconnect_required_disables_home_unlock_and_jogs(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from autosampler.app_paths import ApplicationPaths
    from autosampler.controllers.mock_grbl import MockGrblController
    from autosampler.gui import AutosamplerWindow
    from autosampler.main import build_runtime
    app = QApplication.instance() or QApplication([])
    m, paths, preferences = build_runtime(True, ApplicationPaths.under(tmp_path))
    w = AutosamplerWindow(m, paths, preferences, mock_mode=True)
    w.limit_timer.stop()
    try:
        m.connect_grbl()
        monkeypatch.setattr(MockGrblController, "recovery_required", property(lambda self: True))
        w._render_state(m.state)
        assert not w.home_button.isEnabled()
        assert not w.calibration_unlock.isEnabled()
        assert not w.move_button.isEnabled()
        assert w.grbl_connection_button.isEnabled()
        assert w.abort_button.isEnabled()
        assert "Secure gravity-loaded axes" in w.connection_homing_progress.text()
        assert "diagnostic status" in w.connection_homing_progress.text()
        assert "RESET" in w.abort_button.toolTip()
        submitted = []
        w._submit = lambda name, fn, *args: submitted.append(name)
        w._poll_limit_status()
        assert w._limit_poll_pending
        assert submitted == ["limit status"]
    finally:
        w.close()
        app.processEvents()


def test_position_panel_feedback_references_coordinates_and_states(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from autosampler.app_paths import ApplicationPaths
    from autosampler.gui import AutosamplerWindow
    from autosampler.main import build_runtime

    app = QApplication.instance() or QApplication([])
    m, paths, preferences = build_runtime(True, ApplicationPaths.under(tmp_path))
    window = AutosamplerWindow(m, paths, preferences, mock_mode=True)
    window.limit_timer.stop()
    try:
        assert "Disconnected" in window.position_status.text()
        assert not window.all_references_button.isEnabled()
        m.connect_grbl()
        m.refresh_grbl_status()
        window._render_state(m.state)
        assert window.position_rows["Z"][2].text() == "UNHOMED"
        assert window.all_references_button.isEnabled()
        submitted = []
        window._submit = lambda name, fn, *args: submitted.append((name, fn, args))
        window.position_reference_buttons["Z"].click()
        assert submitted[-1] == ("measurement reference", m.set_measurement_reference, ("machine", "Z"))
        m.set_measurement_reference("machine", "Z")
        m.grbl.positions["Z"] = -4
        m.refresh_grbl_status()
        window._render_state(m.state)
        assert window.position_rows["Z"][0].text() == "-4.000"
        assert window.position_rows["Z"][1].text() == "-4.000"
        window.position_coordinate.setCurrentIndex(1)
        assert window.position_rows["Z"][1].text() == "—"
        m.state.reported_position.updated_at -= 3
        window._render_state(m.state)
        assert "Stale" in window.position_status.text()
        assert "stale" in window.position_rows["Z"][0].text()
        assert not window.all_references_button.isEnabled()
        m.home("Z")
        m.refresh_grbl_status()
        window._render_state(m.state)
        assert window.position_rows["Z"][2].text() == "Homed"
        assert window.position_rows["A"][2].text() == "UNHOMED"
        assert window.position_rows["Z"][1].text() == "—"
        m.disconnect_grbl()
        window._render_state(m.state)
        assert "Disconnected" in window.position_status.text()
        assert not window.all_references_button.isEnabled()
    finally:
        window.close()
        app.processEvents()


def test_manual_terminal_send_enter_responses_and_safety(tmp_path, monkeypatch, caplog):
    import logging
    import time

    caplog.set_level(logging.INFO)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from autosampler.app_paths import ApplicationPaths
    from autosampler.errors import ControllerError
    from autosampler.gui import AutosamplerWindow
    from autosampler.main import build_runtime

    application = QApplication.instance() or QApplication([])
    machine, paths, preferences = build_runtime(True, ApplicationPaths.under(tmp_path))
    window = AutosamplerWindow(machine, paths, preferences, mock_mode=True)
    window.limit_timer.stop()

    def finish_command():
        deadline = time.monotonic() + 2
        while window._manual_command_pending and time.monotonic() < deadline:
            QTest.qWait(10)
            window.bridge.drain()
        assert not window._manual_command_pending
        application.processEvents()

    try:
        assert not window.manual_command_input.isEnabled()
        assert not window.manual_send_button.isEnabled()
        machine.connect_grbl()
        window._render_state(machine.state)
        assert machine.calibration_issues()
        assert window.manual_command_input.isEnabled()
        assert not window.manual_send_button.isEnabled()
        command = "  G1 X2 F10  "
        window.manual_command_input.setText(command)
        window.manual_send_button.click()
        assert not window.manual_send_button.isEnabled()
        finish_command()
        assert machine.grbl.commands[-1] == command
        assert not hasattr(window, "manual_command_log")
        assert window.log_output.toPlainText().count(f"Manual GRBL > {command}") == 1
        assert window.log_output.toPlainText().count("Manual GRBL < ok (simulation") == 1

        def settings(command, on_response):
            assert command == "$$"
            on_response("$100=1")
            on_response("ok")
            return "ok"

        monkeypatch.setattr(machine.grbl, "send_command", settings)
        window.manual_command_input.setText("$$")
        QTest.keyClick(window.manual_command_input, Qt.Key.Key_Return)
        finish_command()
        for message in ("Manual GRBL > $$", "Manual GRBL < $100=1", "Manual GRBL < ok"):
            assert sum(message == record.getMessage() for record in caplog.records) == 1
            assert message in window.log_output.toPlainText()

        def fail(command, on_response):
            raise ControllerError("error:20")

        monkeypatch.setattr(machine.grbl, "send_command", fail)
        window.manual_command_input.setText("G1")
        window.manual_send_button.click()
        finish_command()
        assert window.log_output.toPlainText().count("error:20") == 1
        assert window.manual_command_input.isEnabled()
        window.abort_button.click()
        assert machine.state.abort_requested
        assert not window.manual_command_input.isEnabled()
        assert not window.manual_send_button.isEnabled()
        machine.home()
        window._render_state(machine.state)
        assert window.manual_command_input.isEnabled()
        machine.disconnect_grbl()
        window._render_state(machine.state)
        assert not window.manual_command_input.isEnabled()
    finally:
        window.close()
        application.processEvents()


def test_pyside_window_constructs_with_mock_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from autosampler.app_paths import ApplicationPaths
    from autosampler.gui import AutosamplerWindow
    from autosampler.main import build_runtime

    application = QApplication.instance() or QApplication([])
    paths = ApplicationPaths.under(tmp_path)
    machine, paths, preferences = build_runtime(True, paths)
    window = AutosamplerWindow(machine, paths, preferences, mock_mode=True)
    assert not window.windowIcon().isNull()
    assert window.tabs.count() == 4
    assert window.tabs.tabText(0) == "Connections & Safety"
    assert window.tabs.cornerWidget(Qt.Corner.TopRightCorner) is window.abort_button.parentWidget()
    assert window.abort_button.height() == window.tabs.tabBar().sizeHint().height()
    assert window.main_splitter.handleWidth() >= 18
    assert "Mock GRBL" == window.grbl_devices.currentText()
    assert window.calibration_unlock.text() == "Calibration unlock ($X)"
    assert window.home_button.text() == "Home selected"
    assert window.home_axis.count() == 5
    assert window.home_axis.itemText(0) == "All axes"
    assert "Commanded distance from X home: unknown" in window.home_distance_readout.text()
    submitted = []
    window._submit = lambda name, function, *args: submitted.append((name, function, args))
    assert window.grbl_connection_button.text() == "Connect GRBL"
    assert window.scale_connection_button.text() == "Connect Scale"
    window.grbl_connection_button.click()
    assert submitted[-1] == ("connect GRBL", machine.connect_grbl, ())
    window.scale_connection_button.click()
    assert submitted[-1] == ("connect scale", machine.connect_scale, ())
    assert not window.home_button.isEnabled()
    machine.connect_grbl()
    window._render_state(machine.state)
    for index, axis in enumerate(("X", "Z", "A", "B"), start=1):
        window.home_axis.setCurrentIndex(index)
        window.home_button.click()
        assert submitted[-1] == (f"home {axis}", machine.home, (axis,))
    machine.connect_grbl()
    window._render_state(machine.state)
    assert window.grbl_connection_button.text() == "Disconnect GRBL"
    window.grbl_connection_button.click()
    assert submitted[-1] == ("disconnect GRBL", machine.disconnect_grbl, ())
    machine.grbl.limit_pins = {"Z", "B"}
    machine.refresh_grbl_status()
    window._render_state(machine.state)
    for label in (*window.limit_indicators.values(), window.scale_readout):
        assert label.styleSheet().count("{") == label.styleSheet().count("}")
    assert window.limit_indicators["Z"].text() == "Z  PRESSED"
    assert window.limit_indicators["B"].text() == "B  PRESSED"
    assert window.limit_indicators["X"].text() == "X  open"
    machine.home("Z")
    machine.config.z.safe_position_mm = 0.0
    machine.home("X")
    machine.move_axis("X", 15, "degrees")
    window._render_state(machine.state)
    assert "+78 logical full steps" in window.home_distance_readout.text()
    machine.connect_scale()
    window._render_state(machine.state)
    assert window.scale_connection_button.text() == "Disconnect Scale"
    window.scale_connection_button.click()
    assert submitted[-1] == ("disconnect scale", machine.disconnect_scale, ())
    machine.scale.mass_g = 12.345
    window.bridge.drain()
    assert "+12.345 g" in window.scale_readout.text()
    window.close()
    application.processEvents()


def test_hardware_window_preselects_a_lone_grbl_port(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from autosampler.app_paths import ApplicationPaths
    from autosampler.devices import SerialDeviceInfo
    from autosampler.gui import AutosamplerWindow
    from autosampler.main import build_runtime

    application = QApplication.instance() or QApplication([])
    device = SerialDeviceInfo("/dev/ttyUSB0", vid=0x1A86, pid=0x7523, location="1-1.2")
    monkeypatch.setattr("autosampler.gui.app.discover_serial_devices", lambda: [device])
    machine, paths, preferences = build_runtime(True, ApplicationPaths.under(tmp_path))
    window = AutosamplerWindow(machine, paths, preferences, mock_mode=False)
    assert window.grbl_devices.currentData() == device
    assert window.scale_devices.currentData() is None
    assert not machine.state.grbl_connected
    second = SerialDeviceInfo("/dev/ttyUSB1", vid=0x1A86, pid=0x7523, location="1-1.4")
    monkeypatch.setattr("autosampler.gui.app.discover_serial_devices", lambda: [device, second])
    window.refresh_devices()
    assert window.grbl_devices.currentData() is None
    assert window.scale_devices.currentData() is None
    window.close()
    application.processEvents()


def test_glovebox_source_keeps_local_role_assignment(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from autosampler.app_paths import ApplicationPaths
    from autosampler.devices import DeviceRole, SerialDeviceInfo
    from autosampler.gui import AutosamplerWindow
    from autosampler.main import build_runtime
    from autosampler.remote_serial import remote_uri

    application = QApplication.instance() or QApplication([])
    local = SerialDeviceInfo("COM4", vid=0x2341, pid=0x0042, serial_number="LOCAL")
    remote = SerialDeviceInfo(remote_uri("dcm@10.12.194.1", "/dev/ttyUSB0"),
                              vid=0x1A86, pid=0x7523, location="1-1.2")
    monkeypatch.setattr("autosampler.gui.app.discover_serial_devices", lambda: [local])
    machine, paths, preferences = build_runtime(True, ApplicationPaths.under(tmp_path))
    preferences.associate(DeviceRole.GRBL, local)
    window = AutosamplerWindow(machine, paths, preferences, mock_mode=False)
    requests = []
    window.remote_discovery.start = lambda purpose, target: requests.append((purpose, target))
    window.device_source.setCurrentIndex(1)
    assert requests[-1] == ("refresh", "dcm@10.12.194.1")
    window._remote_discovery_completed("refresh", "dcm@10.12.194.1", [remote])
    window._submit = lambda name, function, *args: None
    assert window.grbl_devices.currentData() == remote
    assert window.scale_devices.currentData() is None
    window.connect_grbl()
    assert machine.config.grbl.port == remote.device
    assert preferences.identity(DeviceRole.GRBL).last_known_port == "COM4"
    assert preferences.identity(DeviceRole.GRBL_REMOTE).last_known_port == remote.device
    window.close()
    application.processEvents()

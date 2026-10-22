import pytest


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

import pytest


@pytest.fixture
def vessel_window(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from autosampler.app_paths import ApplicationPaths
    from autosampler.gui import AutosamplerWindow
    from autosampler.main import build_runtime

    application = QApplication.instance() or QApplication([])
    paths = ApplicationPaths.under(tmp_path)
    machine, paths, preferences = build_runtime(True, paths)
    window = AutosamplerWindow(machine, paths, preferences, mock_mode=True)
    yield window, machine
    window.close()
    application.processEvents()


def fill_row(window, location, name, category, density, quantity):
    row = window.vessel_rows[location]
    row["name"].setText(name)
    row["category"].setCurrentText(category)
    row["density"].setText(str(density))
    row["quantity"].setText(str(quantity))


@pytest.mark.parametrize("liquid_ready", [False, True])
def test_entire_command_menu_preserves_presses_across_background_refreshes(vessel_window, configured_machine, liquid_ready):
    from copy import deepcopy
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window, machine = vessel_window
    machine.config = deepcopy(configured_machine.config)
    if not liquid_ready:
        machine.config.transfer.tolerance_ul = None
    fill_row(window, "V01", "Sample", "input", 1, 5000)
    fill_row(window, "V02", "Product", "output", 1, 0)
    fill_row(window, "B1", "Wash", "wash", 1, 50000)
    window.vessel_rows["B2"]["category"].setCurrentText("dump")
    window.save_vessels()
    machine.connect_grbl()
    machine.home()
    window._render_state(machine.state)
    names = ["go to spot", "draw", "dispense", "dump", "clean", "transfer"]
    submitted = []
    window._submit = lambda name, function, *args: submitted.append(name)
    for index, button in enumerate(window.operation_buttons):
        if not liquid_ready and index != 0:
            assert not button.isEnabled()
            continue
        assert button.isEnabled()
        QTest.mousePress(button, Qt.MouseButton.LeftButton)
        assert button.isDown()
        for _ in range(5):
            window._command_completed("limit status", "<Idle>")
            window._render_state(machine.state)
            assert button.isDown()
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
        assert submitted.count(names[index]) == 1
    assert [name for name in submitted if name != "limit status"] == (names if liquid_ready else names[:1])


def test_real_safety_change_cancels_pending_menu_press(vessel_window, configured_machine):
    from copy import deepcopy
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window, machine = vessel_window
    machine.config = deepcopy(configured_machine.config)
    fill_row(window, "V01", "Sample", "input", 1, 5000)
    fill_row(window, "B1", "Wash", "wash", 1, 50000)
    window.vessel_rows["B2"]["category"].setCurrentText("dump")
    window.save_vessels()
    machine.connect_grbl()
    machine.home()
    window._render_state(machine.state)
    submitted = []
    window._submit = lambda name, function, *args: submitted.append(name)
    for button in window.operation_buttons:
        machine.state.mark_homed()
        window._render_state(machine.state)
        assert button.isEnabled()
        QTest.mousePress(button, Qt.MouseButton.LeftButton)
        machine.state.invalidate_position("Safety state changed")
        window._render_state(machine.state)
        assert not button.isEnabled()
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
    assert submitted == []


def test_menu_refresh_preserves_input_selection_and_values(vessel_window):
    window, machine = vessel_window
    fill_row(window, "V01", "Sample", "input", 1, 5000)
    fill_row(window, "V02", "Product", "output", 1, 0)
    window.save_vessels()
    window.operation_fields["source"].setCurrentIndex(1)
    window.operation_fields["destination"].setCurrentIndex(0)
    window.operation_fields["buffer_percent"].setText("2")
    amount = window.operation_fields["amount"]
    amount.setText("1234.5")
    amount.setSelection(1, 3)
    for _ in range(10):
        window._render_state(machine.state)
    assert amount.text() == "1234.5"
    assert amount.selectedText() == "234"
    assert window.operation_fields["buffer_percent"].text() == "2"
    assert window.operation_fields["source"].currentData() == "V02"
    assert window.operation_fields["destination"].currentData() == "V01"


def test_calibration_form_accepts_negative_z_coordinate(vessel_window):
    window, m = vessel_window
    m.record_calibration("z.max_travel_mm", 250, True)
    window._submit = lambda name, function, *args: function(*args)
    errors = []
    window.show_error = lambda title, error: errors.append(str(error))
    window.measurement_combo.setCurrentIndex(window.measurement_combo.findData("z.safe_position_mm"))
    window.measurement_value.setText("-125")
    window.measurement_confirm.setChecked(True)
    window.record_measurement()
    window._command_completed("record calibration", None)
    assert errors == []
    assert m.config.z.safe_position_mm == -125
    assert window.measurement_value.text() == "-125.0"


def test_editing_saved_vessels_survives_polling_and_command_refresh(vessel_window):
    window, machine = vessel_window
    fill_row(window, "V24", "Original", "input", 1, 2000)
    window.save_vessels()
    window.refresh_vessels()
    row = window.vessel_rows["V24"]
    # Simulate each keystroke arriving between background status replies.
    for name in ("O", "Op", "Operator edited"):
        row["name"].setText(name)
        window._limit_poll_pending = True
        window._command_completed("limit status", "<Idle>")
        assert not window._limit_poll_pending
        window._command_completed("read firmware calibration", {})
        assert row["name"].text() == name
    row["category"].setCurrentText("wash")
    row["density"].setText("0.98")
    row["quantity"].clear()
    window.refresh_vessels()
    assert row["quantity"].text() == ""
    assert row["density"].text() == "0.98"
    assert row["category"].currentText() == "wash"
    assert machine.inventory.get("V24").name == "Original"
    row["quantity"].setText("3000")
    window.save_vessels()
    window.refresh_vessels()
    loaded = machine.store.load().get("V24")
    assert loaded.name == "Operator edited"
    assert loaded.quantity_ul == 3000
    assert loaded.density_g_ml == 0.98
    assert loaded.category.value == "cleaning"


def test_clean_table_still_reflects_liquid_quantity_changes(vessel_window):
    window, machine = vessel_window
    fill_row(window, "V24", "Source", "input", 1, 2000)
    window.save_vessels()
    machine.inventory.remove("V24", 100)
    window._command_completed("draw", 100)
    assert window.vessel_rows["V24"]["quantity"].text() == "1900"


def test_go_to_source_available_before_liquid_calibration(vessel_window):
    window, m = vessel_window
    fill_row(window, "V01", "Alignment test", "input", 1, 1000)
    window.save_vessels()
    m.connect_grbl()
    m.home()
    m.config.z.max_travel_mm = 150
    m.config.z.safe_position_mm = -20
    m.config.z.motion_speed_mm_min = 100
    m.config.pipette_1_positions_deg["V24"] = 276
    window._render_state(m.state)
    assert window.operation_buttons[0].isEnabled()
    assert not any(button.isEnabled() for button in window.operation_buttons[1:])
    assert "positioning tests" in window.operation_status.text()
    submitted = []
    window._submit = lambda name, function, *args: submitted.append((name, args))
    window.operation_buttons[0].click()
    assert submitted == [("go to spot", ("V01", window._pipette()))]
    m.abort()
    window._render_state(m.state)
    assert not window.operation_buttons[0].isEnabled()


def test_positioning_chooses_configured_source_and_explains_actual_lock(vessel_window):
    window, m = vessel_window
    fill_row(window, "V24", "Position test", "wash", 1, 0)
    fill_row(window, "B2", "Waste", "dump", 1, 0)
    window.save_vessels()
    assert window.operation_fields["source"].currentData() == "V24"
    assert "Home all axes" in window.operation_status.text()
    m.connect_grbl()
    m.home()
    m.config.z.max_travel_mm = 250
    m.config.z.safe_position_mm = -125
    m.config.z.motion_speed_mm_min = 1000
    m.config.pipette_1_positions_deg["V24"] = -192
    window._render_state(m.state)
    assert window.operation_buttons[0].isEnabled()
    assert not any(b.isEnabled() for b in window.operation_buttons[1:])
    assert "Go to source is available" in window.operation_status.text()
    source = window.operation_fields["source"]
    assert source.findData("V01") == -1
    source.setCurrentIndex(-1)
    window._render_state(m.state)
    assert source.currentData() is None
    assert not window.operation_buttons[0].isEnabled()
    assert "Select a physical source location" in window.operation_status.text()
    source.setCurrentIndex(source.findData("V24"))
    m.config.z.safe_position_mm = None
    window._render_state(m.state)
    assert "Z safe position must be calibrated" in window.operation_status.text()


def test_draw_buffer_prefill_override_and_reset(vessel_window):
    from PySide6.QtTest import QTest

    window, machine = vessel_window
    machine.config.transfer.default_draw_buffer_percent = 5
    window._render_state(machine.state)
    field = window.operation_fields["buffer_percent"]
    assert field.text() == "5"
    field.selectAll()
    QTest.keyClicks(field, "12.5")
    machine.config.transfer.default_draw_buffer_percent = 8
    window._render_state(machine.state)
    assert field.text() == "12.5"
    assert window._draw_buffer() == 12.5
    window.use_default_buffer_button.click()
    assert field.text() == "8"
    assert machine.config.transfer.default_draw_buffer_percent == 8


def test_gui_draw_and_transfer_pass_override(vessel_window):
    window, machine = vessel_window
    window._require_command_ready = lambda *locations: locations
    submitted, errors = [], []
    window._submit = lambda name, function, *args: submitted.append((name, args))
    window.show_error = lambda title, error: errors.append(str(error))
    window.operation_fields["buffer_percent"].setText("7.5")
    window.draw()
    assert submitted[-1][0] == "draw"
    assert submitted[-1][1][-1] == 7.5
    window.transfer()
    assert submitted[-1][0] == "transfer"
    assert submitted[-1][1][-1] == 7.5
    before = len(submitted)
    for invalid in ("", "-1", "nan", "inf", "abc"):
        window.operation_fields["buffer_percent"].setText(invalid)
        window.draw()
        window.transfer()
    assert len(submitted) == before
    assert len(errors) == 10


def test_vessel_mutation_buttons_disabled_while_command_pending(vessel_window):
    window, machine = vessel_window
    window._command_inflight = "draw"
    window._render_state(machine.state)
    assert not window.apply_vessels_button.isEnabled()
    assert not window.reset_session_button.isEnabled()
    window._command_inflight = None


def test_limit_indicator_requires_fresh_idle_report(vessel_window, monkeypatch):
    window, machine = vessel_window
    window.limit_timer.stop()
    machine.connect_grbl()
    machine.refresh_grbl_status()
    window._render_state(machine.state)
    assert "open" in window.limit_indicators["X"].text()
    machine.state.grbl_status_updated_at -= 3
    window._render_state(machine.state)
    assert "UNKNOWN" in window.limit_indicators["X"].text()
    for report in ("ALARM:1", "Grbl 1.1h", "<Home|Pn:X>", "<Alarm|Pn:X>", "<garbage>"):
        monkeypatch.setattr(machine.grbl, "status", lambda: report)
        machine.refresh_grbl_status()
        window._render_state(machine.state)
        assert "UNKNOWN" in window.limit_indicators["X"].text()


def test_declining_buffer_induced_extra_cycle_does_not_switch_pipette(vessel_window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    window, machine = vessel_window
    window._require_command_ready = lambda *locations: locations
    window.operation_fields["amount"].setText("200")
    window.operation_fields["buffer_percent"].setText("5")
    submitted = []
    window._submit = lambda *args: submitted.append(args)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    window.transfer()
    assert not submitted


def test_fixed_physical_rack_and_hidden_types(vessel_window):
    window, _machine = vessel_window
    assert window.vessel_table.rowCount() == 26
    assert [window.vessel_table.item(row, 0).text() for row in range(26)] == [
        *(f"V{number:02d}" for number in range(1, 25)), "B1", "B2"
    ]
    assert window.vessel_table.columnCount() == 5
    headers = [window.vessel_table.horizontalHeaderItem(column).text() for column in range(5)]
    assert headers == ["Location", "Name", "Category", "Density (g/mL)", "Initial quantity (µL)"]
    assert [window.vessel_rows["V01"]["category"].itemText(i) for i in range(5)] == [
        "", "input", "output", "wash", "dump"
    ]


def test_complete_rows_persist_with_inferred_capacity_and_special_mapping(vessel_window):
    from autosampler.vessels.models import VesselCategory, VesselType

    window, machine = vessel_window
    fill_row(window, "V01", "Sample", "input", 0.998, 12000)
    fill_row(window, "B1", "Rinse", "wash", 1, 90000)
    fill_row(window, "B2", "Waste", "dump", 1, 0)
    window.save_vessels()
    assert set(machine.inventory.vessels) == {"V01", "B1", "B2"}
    assert machine.inventory.get("V01").vessel_type is VesselType.VIAL_15ML
    assert machine.inventory.get("V01").capacity_ul == 15000
    assert machine.inventory.get("B1").vessel_type is VesselType.BEAKER_100ML
    assert machine.inventory.get("B1").capacity_ul == 100000
    assert machine.inventory.get("B1").category is VesselCategory.CLEANING
    assert machine.inventory.get("B2").category is VesselCategory.WASTE
    assert window._vessel_setup_issue() is None


def test_partial_rows_are_nonexistent_and_report_missing_or_empty(vessel_window):
    window, machine = vessel_window
    fill_row(window, "B1", "Rinse", "wash", 1, 50000)
    fill_row(window, "B2", "Waste", "dump", 1, 0)
    row = window.vessel_rows["V01"]
    row["name"].setText("Sample")
    row["category"].setCurrentText("input")
    window.save_vessels()
    assert "V01" not in machine.inventory.vessels
    with pytest.raises(ValueError, match="missing: density, initial quantity"):
        window._require_configured_slot("V1")
    row["category"].setCurrentIndex(0)
    with pytest.raises(ValueError, match="empty slot"):
        window._require_configured_slot("V01")


def test_special_categories_must_be_unique_and_valid_save_is_atomic(vessel_window):
    window, machine = vessel_window
    errors = []
    window.show_error = lambda title, error: errors.append(str(error))
    fill_row(window, "B1", "Rinse", "wash", 1, 50000)
    fill_row(window, "B2", "Waste", "dump", 1, 0)
    window.save_vessels()
    before = machine.inventory
    fill_row(window, "V01", "Too full", "wash", 1, 16000)
    window.save_vessels()
    assert errors and "exceeds" in errors[-1]
    assert machine.inventory is before
    window.vessel_rows["V01"]["quantity"].setText("100")
    window.save_vessels()
    assert "assigned Wash: 2, Dump: 1; complete Wash: 2, Dump: 1" in window._vessel_setup_issue()


def test_warning_recognizes_visible_but_incomplete_special_assignments(vessel_window):
    window, _machine = vessel_window
    window.vessel_rows["V01"]["category"].setCurrentText("wash")
    window.vessel_rows["V02"]["category"].setCurrentText("wash")
    window.vessel_rows["B2"]["category"].setCurrentText("dump")
    issue = window._vessel_setup_issue()
    assert "assigned Wash: 2, Dump: 1" in issue
    assert "V01 Wash missing density, initial quantity" in issue
    assert "B2 Dump missing" not in issue
    assert window.vessel_rows["B2"]["name"].text() == "Dump"


def test_special_category_defaults_names_and_preserves_custom_names(vessel_window):
    window, _machine = vessel_window
    row = window.vessel_rows["V01"]
    row["category"].setCurrentText("wash")
    assert row["name"].text() == "Wash"
    row["category"].setCurrentText("dump")
    assert row["name"].text() == "Dump"
    row["name"].setText("Mixed solvents")
    row["category"].setCurrentText("wash")
    assert row["name"].text() == "Mixed solvents"
    row["name"].clear()
    assert row["name"].text() == ""  # editable until the category is selected again
    row["category"].setCurrentText("dump")
    assert row["name"].text() == "Dump"


def test_dump_needs_only_name_and_category_and_reenables_fields_when_changed(vessel_window):
    window, machine = vessel_window
    fill_row(window, "B1", "Wash", "wash", 1, 50000)
    row = window.vessel_rows["B2"]
    row["name"].setText("Mixed remains")
    row["category"].setCurrentText("dump")
    assert not window._missing_vessel_fields("B2")
    assert not row["density"].isEnabled()
    assert not row["quantity"].isEnabled()
    window.save_vessels()
    assert window._vessel_setup_issue() is None
    assert window._require_configured_slot("B2") == "B2"
    dump = machine.store.load().get("B2")
    assert dump.quantity_ul is None
    assert dump.density_g_ml is None
    window.refresh_vessels()
    assert row["density"].text() == ""
    assert row["quantity"].text() == ""
    row["category"].setCurrentText("output")
    assert row["density"].isEnabled()
    assert row["quantity"].isEnabled()
    assert window._missing_vessel_fields("B2") == ["density", "initial quantity"]


def test_command_prompts_for_incomplete_or_empty_referenced_slot(vessel_window):
    window, _machine = vessel_window
    errors = []
    window.show_error = lambda title, error: errors.append(str(error))
    row = window.vessel_rows["V01"]
    row["name"].setText("Sample")
    row["category"].setCurrentText("input")
    assert window.operation_fields["source"].findData("V01") == -1
    assert window.operation_fields["source"].findData("V02") == -1
    with pytest.raises(ValueError, match="missing: density, initial quantity"):
        window._require_configured_slot("V01")
    with pytest.raises(ValueError, match="empty slot"):
        window._require_configured_slot("V02")


def test_vessel_dropdowns_only_offer_complete_applied_slots(vessel_window):
    window, machine = vessel_window
    source = window.operation_fields["source"]
    destination = window.operation_fields["destination"]
    assert not source.isEditable() and not destination.isEditable()
    assert source.count() == destination.count() == 0
    fill_row(window, "V01", "Sample", "input", 1, 2000)
    fill_row(window, "V02", "Product", "output", 1, 0)
    fill_row(window, "B1", "Wash", "wash", 1, 50000)
    window.vessel_rows["B2"]["category"].setCurrentText("dump")
    window.vessel_rows["V03"]["name"].setText("Incomplete")
    assert source.count() == destination.count() == 0  # apply required
    window.save_vessels()
    assert [source.itemData(i) for i in range(source.count())] == ["V01", "V02", "B1"]
    assert [destination.itemData(i) for i in range(destination.count())] == ["V01", "V02", "B1", "B2"]
    assert source.itemText(0) == "V01 — Sample"
    assert source.currentData() == "V01"
    assert destination.currentData() == "V02"
    source.setCurrentIndex(source.findData("B1"))
    window._render_state(machine.state)
    assert source.currentData() == "B1"
    window.vessel_rows["B1"]["name"].setText("Rinse")
    window.save_vessels()
    assert source.currentData() == "B1"
    assert source.currentText() == "B1 — Rinse"
    window.vessel_rows["B1"]["density"].clear()
    window.save_vessels()
    assert source.findData("B1") == -1
    assert destination.findData("B1") == -1
    assert source.currentData() == "V01"

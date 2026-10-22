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
    assert "V01 Wash missing name, density, initial quantity" in issue
    assert "B2 Dump missing name, density, initial quantity" in issue


def test_command_prompts_for_incomplete_or_empty_referenced_slot(vessel_window):
    window, _machine = vessel_window
    errors = []
    window.show_error = lambda title, error: errors.append(str(error))
    row = window.vessel_rows["V01"]
    row["name"].setText("Sample")
    row["category"].setCurrentText("input")
    window.operation_fields["source"].setText("V01")
    window.go_to_source()
    assert "missing: density, initial quantity" in errors[-1]
    window.operation_fields["source"].setText("V02")
    window.go_to_source()
    assert "empty slot" in errors[-1]

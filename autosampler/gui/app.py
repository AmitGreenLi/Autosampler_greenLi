from __future__ import annotations

import logging
import queue
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot, Qt
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QTabWidget,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from autosampler.devices import DeviceRole, SerialDeviceInfo, device_is_present, discover_serial_devices, resolve_identity
from autosampler.diagnostics import environment_diagnostics, format_diagnostics
from autosampler.models import Axis, OperationState, PipetteId
from autosampler.vessels.inventory import Inventory
from autosampler.vessels.models import Vessel, VesselCategory, VesselType
from autosampler.worker import MachineWorker
from autosampler.calibration import measurements, get_value

APP_ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "app_icon.png"


def application_icon():
    return QIcon(str(APP_ICON_PATH))


class QtLogEmitter(QObject):
    message = Signal(str)


class QtLogHandler(logging.Handler):
    def __init__(self, emitter):
        super().__init__()
        self.emitter = emitter

    def emit(self, record):
        self.emitter.message.emit(self.format(record))


class QtWorkerBridge(QObject):
    """Converts framework-neutral worker queues into main-thread Qt signals."""

    command_started = Signal(str)
    command_completed = Signal(str, object)
    command_failed = Signal(str, object)
    state_changed = Signal(object)

    def __init__(self, worker, machine, parent=None):
        super().__init__(parent)
        self.worker, self.machine = worker, machine
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.drain)

    def start(self): self.timer.start()
    def stop(self): self.timer.stop()

    @Slot()
    def drain(self):
        while True:
            try:
                event = self.worker.events.get_nowait()
            except queue.Empty:
                break
            if event.kind == "started":
                self.command_started.emit(event.name)
            elif event.kind == "completed":
                self.command_completed.emit(event.name, event.value)
            else:
                self.command_failed.emit(event.name, event.value)
        self.machine.refresh_scale_state()
        self.state_changed.emit(self.machine.state)


class DiagnosticsDialog(QDialog):
    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Application Diagnostics")
        self.resize(720, 500)
        layout = QVBoxLayout(self)
        output = QPlainTextEdit(text)
        output.setReadOnly(True)
        layout.addWidget(output)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class AutosamplerWindow(QMainWindow):
    AXIS_LABELS = {f"{axis.friendly_name} ({axis.value})": axis for axis in Axis}
    VESSEL_LOCATIONS = tuple(f"V{number:02d}" for number in range(1, 25)) + ("B1", "B2")
    UI_CATEGORIES = {
        "input": VesselCategory.INPUT,
        "output": VesselCategory.OUTPUT,
        "wash": VesselCategory.CLEANING,
        "dump": VesselCategory.WASTE,
    }

    def __init__(self, machine, paths, device_preferences, mock_mode=True):
        super().__init__()
        self.machine = machine
        self.paths = paths
        self.device_preferences = device_preferences
        self.mock_mode = mock_mode
        self._unlock_pending = False
        self._limit_poll_pending = False
        self._vessel_table_dirty = False
        self.serial_devices = []
        self.worker = MachineWorker()
        self.worker.start()
        self.bridge = QtWorkerBridge(self.worker, machine, self)
        self.bridge.command_completed.connect(self._command_completed)
        self.bridge.command_failed.connect(self._command_failed)
        self.bridge.state_changed.connect(self._render_state)
        self.bridge.start()
        self.log_emitter = QtLogEmitter(self)
        self.log_handler = QtLogHandler(self.log_emitter)
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(self.log_handler)
        self.setWindowTitle("DIY CNC Autosampler")
        self.setWindowIcon(application_icon())
        self.resize(1100, 760)
        self.setMinimumSize(720, 520)
        self.setStyleSheet("QPushButton { min-height: 34px; padding: 3px 10px; } QLineEdit, QComboBox { min-height: 30px; }")
        self._closing = False
        self._build_ui()
        self.log_emitter.message.connect(self.log_output.appendPlainText)
        self.refresh_devices()
        self.presence_timer = QTimer(self)
        self.presence_timer.setInterval(2000)
        self.presence_timer.timeout.connect(self._monitor_device_presence)
        if not self.mock_mode:
            self.presence_timer.start()
        self.limit_timer = QTimer(self)
        self.limit_timer.setInterval(250)
        self.limit_timer.timeout.connect(self._poll_limit_status)
        self.limit_timer.start()
        self._render_state(self.machine.state)

    def _build_ui(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)
        self.tabs = QTabWidget()
        safety_corner = QWidget()
        safety_layout = QHBoxLayout(safety_corner)
        safety_layout.setContentsMargins(4, 0, 0, 0)
        safety_layout.setSpacing(6)
        safety_layout.addWidget(QLabel("Emergency stop"))
        self.abort_button = QPushButton("ABORT")
        self.abort_button.setToolTip("Immediately stop motion and require Home before continuing.")
        self.abort_button.clicked.connect(self.abort)
        safety_layout.addWidget(self.abort_button)
        self.tabs.setCornerWidget(safety_corner, Qt.Corner.TopRightCorner)
        self.tabs.addTab(self._scrollable(self._connection_panel()), "Connections & Safety")
        self.tabs.addTab(self._scrollable(self._calibration_panel()), "Manual Calibration")
        self.tabs.addTab(self._scrollable(self._vessel_panel()), "Vessels")
        self.tabs.addTab(self._scrollable(self._operations_panel()), "Liquid Commands")
        tab_height = self.tabs.tabBar().sizeHint().height()
        self.abort_button.setStyleSheet(
            "QPushButton { background: #b71c1c; color: white; font-weight: bold; "
            "border: none; border-radius: 4px; "
            f"min-width: 120px; min-height: {tab_height}px; max-height: {tab_height}px; padding: 0 10px; }}"
        )
        self.abort_button.setFixedHeight(tab_height)
        safety_corner.setFixedHeight(tab_height)

        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(QLabel("Operation log — drag the divider to resize"))
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(2000)
        log_layout.addWidget(self.log_output)

        self.main_splitter = QSplitter(Qt.Orientation.Vertical)
        self.main_splitter.setHandleWidth(18)
        self.main_splitter.setChildrenCollapsible(True)
        self.main_splitter.addWidget(self.tabs)
        self.main_splitter.addWidget(log_panel)
        self.main_splitter.setStretchFactor(0, 4)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes((560, 150))
        self.main_splitter.setToolTip("Drag the wide divider to resize the operation log.")
        outer.addWidget(self.main_splitter, 1)
        self.setCentralWidget(central)

    @staticmethod
    def _scrollable(content):
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(content)
        return area

    def _connection_panel(self):
        group = QGroupBox("Connections and safety")
        grid = QGridLayout(group)
        self.grbl_devices, self.scale_devices = QComboBox(), QComboBox()
        self.grbl_devices.setEditable(True)
        self.scale_devices.setEditable(True)
        grid.addWidget(QLabel("GRBL device"), 0, 0)
        grid.addWidget(self.grbl_devices, 0, 1, 1, 3)
        grid.addWidget(QLabel("Scale device"), 1, 0)
        grid.addWidget(self.scale_devices, 1, 1, 1, 3)
        refresh = QPushButton("Refresh devices")
        refresh.clicked.connect(self.refresh_devices)
        grid.addWidget(refresh, 0, 4, 2, 1)
        controls = QHBoxLayout()
        self.grbl_connection_button = QPushButton("Connect GRBL")
        self.grbl_connection_button.clicked.connect(self.toggle_grbl_connection)
        controls.addWidget(self.grbl_connection_button)
        self.scale_connection_button = QPushButton("Connect Scale")
        self.scale_connection_button.clicked.connect(self.toggle_scale_connection)
        controls.addWidget(self.scale_connection_button)
        self.home_axis = QComboBox()
        self.home_axis.addItem("All axes", None)
        for label, axis in self.AXIS_LABELS.items():
            self.home_axis.addItem(label, axis.value)
        self.home_axis.setToolTip("Choose the complete $H cycle or one GRBL-Mega-5X axis homing command.")
        controls.addWidget(self.home_axis)
        self.home_button = QPushButton("Home selected")
        self.home_button.clicked.connect(self.home_selected)
        controls.addWidget(self.home_button)
        self.calibration_unlock = QCheckBox("Calibration unlock ($X)")
        self.calibration_unlock.setToolTip(
            "Clears the GRBL alarm for relative calibration jogging only. Coordinates remain untrusted."
        )
        self.calibration_unlock.toggled.connect(self.toggle_calibration_unlock)
        controls.addWidget(self.calibration_unlock)
        diagnostics = QPushButton("Diagnostics")
        diagnostics.clicked.connect(self.show_diagnostics)
        controls.addWidget(diagnostics)
        grid.addLayout(controls, 2, 0, 1, 5)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.scale_readout = QLabel("Scale: disconnected")
        self.scale_readout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scale_readout.setMinimumHeight(58)
        grid.addWidget(self.scale_readout, 3, 0, 1, 5)
        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("Limit switches"))
        self.limit_indicators = {}
        for axis in Axis:
            indicator = QLabel()
            indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
            indicator.setMinimumWidth(105)
            indicator.setToolTip(
                f"GRBL {axis.value}-axis limit state from the realtime Pn: report. "
                "GRBL does not distinguish minimum from maximum here."
            )
            self.limit_indicators[axis.value] = indicator
            limit_row.addWidget(indicator)
        limit_row.addStretch(1)
        grid.addLayout(limit_row, 4, 0, 1, 5)
        grid.addWidget(self.status_label, 5, 0, 1, 5)
        return group

    def _calibration_panel(self):
        panel, layout = QWidget(), QGridLayout()
        panel.setLayout(layout)
        self.axis_combo, self.unit_combo, self.move_amount = QComboBox(), QComboBox(), QLineEdit("1")
        self.axis_combo.addItems(self.AXIS_LABELS)
        self.axis_combo.currentTextChanged.connect(self._update_units)
        self.unit_combo.addItems(("degrees", "steps"))
        for column, text in enumerate(("Axis", "Unit/mode", "Amount (signed)")):
            layout.addWidget(QLabel(text), 0, column)
        layout.addWidget(self.axis_combo, 1, 0)
        layout.addWidget(self.unit_combo, 1, 1)
        layout.addWidget(self.move_amount, 1, 2)
        move = QPushButton("Move")
        move.clicked.connect(self.move_axis)
        layout.addWidget(move, 1, 3)
        jog = QHBoxLayout()
        for value in (-10, -1, -0.1, 0.1, 1, 10):
            button = QPushButton(f"{value:+g}")
            button.clicked.connect(lambda _checked=False, v=value: self.jog(v))
            jog.addWidget(button)
        layout.addLayout(jog, 2, 0, 1, 4)
        self.move_result = QLabel("Requested angle and integer-step result will appear here.")
        self.move_result.setWordWrap(True)
        layout.addWidget(self.move_result, 3, 0, 1, 4)
        self.home_distance_readout = QLabel()
        self.home_distance_readout.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.home_distance_readout.setWordWrap(True)
        self.home_distance_readout.setToolTip(
            "Signed commanded position since the selected axis was last homed. "
            "This is open-loop step counting and cannot detect a stalled or skipped motor step."
        )
        self.home_distance_readout.setStyleSheet(
            "QLabel { background: #e3f2fd; border: 1px solid #90caf9; border-radius: 4px; "
            "color: #0d47a1; font-size: 18px; font-weight: bold; padding: 10px; }"
        )
        layout.addWidget(self.home_distance_readout, 4, 0, 1, 4)
        group = QGroupBox("Guided calibration — operator measurements")
        form = QVBoxLayout(group)
        instructions = QLabel(
            "1. Home the axis, then use small manual jogs only after checking collision-free travel.\n"
            "2. Physically measure and verify the selected value; commanded steps cannot detect stalls.\n"
            "3. Enter the measurement and confirm it. Repeat for Z, A, B, transfer settings, and each carousel location.\n"
            "Carousel angles are measured from X home separately for P1 and P2. Add vessels on the Vessels tab first.\n"
            "Recording sends no motion or firmware settings. Speed/acceleration values must match verified firmware settings."
        )
        instructions.setWordWrap(True)
        form.addWidget(instructions)
        self.measurement_combo = QComboBox()
        self.measurement_combo.currentIndexChanged.connect(self._show_measurement)
        form.addWidget(self.measurement_combo)
        self.measurement_value = QLineEdit()
        self.measurement_value.setPlaceholderText("Enter physically verified measurement in the displayed units")
        form.addWidget(self.measurement_value)
        self.measurement_confirm = QCheckBox("I physically verified this measurement and its reference / units")
        form.addWidget(self.measurement_confirm)
        self.measurement_value.textChanged.connect(lambda: self.measurement_confirm.setChecked(False))
        self.record_measurement_button = QPushButton("Record confirmed measurement")
        self.record_measurement_button.clicked.connect(self.record_measurement)
        form.addWidget(self.record_measurement_button)
        next_button = QPushButton("Next missing measurement")
        next_button.clicked.connect(self._next_measurement)
        form.addWidget(next_button)
        self.calibration_status = QLabel()
        self.calibration_status.setWordWrap(True)
        form.addWidget(self.calibration_status)
        layout.addWidget(group, 5, 0, 1, 4)
        layout.setRowStretch(6, 1)
        return panel

    def _refresh_measurements(self):
        specs = measurements(self.machine.inventory.vessels)
        keys = [self.measurement_combo.itemData(i) for i in range(self.measurement_combo.count())]
        if keys == [m.key for m in specs]:
            return
        selected = self.measurement_combo.currentData()
        self.measurement_combo.blockSignals(True)
        self.measurement_combo.clear()
        for index, spec in enumerate(specs, 1):
            self.measurement_combo.addItem(f"{index}/{len(specs)} · {spec.label}", spec.key)
        self.measurement_combo.setCurrentIndex(max(0, self.measurement_combo.findData(selected)))
        self.measurement_combo.blockSignals(False)
        self._show_measurement()

    def _show_measurement(self):
        key = self.measurement_combo.currentData()
        value = get_value(self.machine.config, key) if key else None
        self.measurement_value.setText("" if value is None else str(value))
        self.measurement_confirm.setChecked(False)

    def _next_measurement(self):
        for index in range(self.measurement_combo.count()):
            if get_value(self.machine.config, self.measurement_combo.itemData(index)) is None:
                self.measurement_combo.setCurrentIndex(index)
                return

    def record_measurement(self):
        try:
            if not self.measurement_confirm.isChecked():
                raise ValueError("Physically verify the measurement and check the confirmation box first.")
            key, value = self.measurement_combo.currentData(), float(self.measurement_value.text())
            self.measurement_confirm.setChecked(False)
            self._submit("record calibration", self.machine.record_calibration, key, value, True)
        except Exception as exc:
            self.show_error("Calibration", exc)

    def _vessel_panel(self):
        panel, layout = QWidget(), QVBoxLayout()
        panel.setLayout(layout)
        explanation = QLabel(
            "Configure any of the 24 fixed 15 mL vial positions and two fixed 100 mL beaker positions. "
            "A row is available to commands only when all four values are entered. Exactly one Wash and one Dump are required."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.vessel_table = QTableWidget(len(self.VESSEL_LOCATIONS), 5)
        self.vessel_table.setHorizontalHeaderLabels(
            ("Location", "Name", "Category", "Density (g/mL)", "Initial quantity (µL)")
        )
        self.vessel_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.vessel_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.vessel_table.verticalHeader().setVisible(False)
        self.vessel_rows = {}
        for row, location in enumerate(self.VESSEL_LOCATIONS):
            location_item = QTableWidgetItem(location)
            location_item.setFlags(location_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.vessel_table.setItem(row, 0, location_item)
            name, density, quantity = QLineEdit(), QLineEdit(), QLineEdit()
            name.setPlaceholderText("Required")
            density.setPlaceholderText("e.g. 1.0")
            quantity.setPlaceholderText("0 to 15000" if location.startswith("V") else "0 to 100000")
            category = QComboBox()
            category.addItems(("", "input", "output", "wash", "dump"))
            self.vessel_table.setCellWidget(row, 1, name)
            self.vessel_table.setCellWidget(row, 2, category)
            self.vessel_table.setCellWidget(row, 3, density)
            self.vessel_table.setCellWidget(row, 4, quantity)
            self.vessel_rows[location] = {
                "name": name, "category": category, "density": density, "quantity": quantity,
            }
            for field in (name, density, quantity):
                field.textChanged.connect(self._vessel_table_changed)
            category.currentIndexChanged.connect(self._vessel_table_changed)
        layout.addWidget(self.vessel_table, 1)
        buttons = QHBoxLayout()
        save, reset = QPushButton("Apply vessel setup"), QPushButton("New Session / Reset")
        save.clicked.connect(self.save_vessels)
        reset.clicked.connect(self.reset_session)
        buttons.addWidget(save)
        buttons.addWidget(reset)
        self.vessel_status = QLabel()
        self.vessel_status.setWordWrap(True)
        buttons.addWidget(self.vessel_status, 1)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.refresh_vessels()
        return panel

    def _operations_panel(self):
        panel, layout = QWidget(), QVBoxLayout()
        panel.setLayout(layout)
        group, form = QGroupBox("Operation inputs"), QFormLayout()
        group.setLayout(form)
        self.operation_fields = {"source": QLineEdit("V01"), "destination": QLineEdit("V02"), "amount": QLineEdit("100")}
        self.pipette_combo = QComboBox()
        self.pipette_combo.addItems(("P1", "P2"))
        form.addRow("Source location", self.operation_fields["source"])
        form.addRow("Destination location", self.operation_fields["destination"])
        form.addRow("Amount µL", self.operation_fields["amount"])
        form.addRow("Pipette", self.pipette_combo)
        layout.addWidget(group)
        self.operation_status = QLabel()
        self.operation_status.setWordWrap(True)
        layout.addWidget(self.operation_status)
        buttons = QGridLayout()
        self.operation_buttons = []
        for index, (text, callback) in enumerate((("Go to source", self.go_to_source), ("Draw", self.draw), ("Dispense", self.dispense), ("Dump", self.dump), ("Clean", self.clean), ("Transfer", self.transfer))):
            button = QPushButton(text)
            button.clicked.connect(callback)
            self.operation_buttons.append(button)
            buttons.addWidget(button, index // 3, index % 3)
        layout.addLayout(buttons)
        layout.addStretch()
        return panel

    @Slot()
    def refresh_devices(self):
        if self.mock_mode:
            self.serial_devices = []
            for combo, text in ((self.grbl_devices, "Mock GRBL"), (self.scale_devices, "Mock scale")):
                combo.clear(); combo.addItem(text, None); combo.setEnabled(False)
            return
        self.serial_devices = discover_serial_devices()
        for role, combo in ((DeviceRole.GRBL, self.grbl_devices), (DeviceRole.SCALE, self.scale_devices)):
            combo.setEnabled(True)
            combo.clear(); combo.addItem("Select a serial device…", None)
            for device in self.serial_devices:
                combo.addItem(device.display_name, device)
            resolution = resolve_identity(self.device_preferences.identity(role), self.serial_devices)
            combo.setCurrentIndex(0)
            if resolution.confident and resolution.selected:
                index = next((i for i in range(combo.count()) if combo.itemData(i) == resolution.selected), 0)
                combo.setCurrentIndex(index)

    @staticmethod
    def _selected_device(combo, role):
        device = combo.currentData()
        if device is None:
            manual_path = combo.currentText().strip()
            if manual_path and not manual_path.startswith("Select a serial device"):
                return SerialDeviceInfo(manual_path)
            raise ValueError(f"Select or enter the {role} serial device first. The application will not guess.")
        return device

    @Slot()
    def _monitor_device_presence(self):
        devices = discover_serial_devices()
        if self.machine.state.grbl_connected and not device_is_present(self.machine.config.grbl.port, devices):
            self.machine.notify_grbl_disconnected()
        if self.machine.state.scale_connected and not device_is_present(self.machine.config.scale.port, devices):
            self.machine.notify_scale_disconnected()

    @Slot()
    def _poll_limit_status(self):
        if not self.machine.state.grbl_connected or self._limit_poll_pending:
            return
        self._limit_poll_pending = True
        self._submit("limit status", self.machine.refresh_grbl_status)

    @Slot()
    def connect_grbl(self):
        try:
            if not self.mock_mode:
                device = self._selected_device(self.grbl_devices, "GRBL")
                self.machine.config.grbl.port = device.device
                self.device_preferences.associate(DeviceRole.GRBL, device)
            self._submit("connect GRBL", self.machine.connect_grbl)
        except Exception as exc: self.show_error("GRBL selection", exc)

    @Slot()
    def connect_scale(self):
        try:
            if not self.mock_mode:
                device = self._selected_device(self.scale_devices, "scale")
                self.machine.config.scale.port = device.device
                self.device_preferences.associate(DeviceRole.SCALE, device)
            self._submit("connect scale", self.machine.connect_scale)
        except Exception as exc: self.show_error("Scale selection", exc)

    @Slot()
    def toggle_grbl_connection(self):
        if self.machine.state.grbl_connected:
            self._submit("disconnect GRBL", self.machine.disconnect_grbl)
        else:
            self.connect_grbl()

    @Slot()
    def toggle_scale_connection(self):
        if self.machine.state.scale_connected:
            self._submit("disconnect scale", self.machine.disconnect_scale)
        else:
            self.connect_scale()

    @Slot()
    def abort(self):
        try: self.machine.abort()  # bypass FIFO so Abort is never queued
        except Exception as exc: self.show_error("Abort", exc)
        self._render_state(self.machine.state)

    @Slot()
    def home_selected(self):
        axis = self.home_axis.currentData()
        self._submit(f"home {axis or 'all axes'}", self.machine.home, axis)

    @Slot(bool)
    def toggle_calibration_unlock(self, enabled):
        if enabled:
            answer = QMessageBox.warning(
                self,
                "Calibration unlock",
                "Send GRBL $X and allow relative manual jogging without homing?\n\n"
                "Machine coordinates will remain UNKNOWN. Automated commands stay disabled. "
                "Confirm the carriage can move safely before continuing.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._set_unlock_checkbox(False)
                return
            self._unlock_pending = True
            self.calibration_unlock.setEnabled(False)
            self._submit("calibration unlock", self.machine.unlock_for_calibration)
        else:
            self._submit("disable calibration unlock", self.machine.disable_calibration_bypass)

    def _set_unlock_checkbox(self, checked):
        self.calibration_unlock.blockSignals(True)
        self.calibration_unlock.setChecked(checked)
        self.calibration_unlock.blockSignals(False)

    def _update_units(self):
        axis = self.AXIS_LABELS[self.axis_combo.currentText()]
        values = ("degrees", "steps") if axis is Axis.CAROUSEL else ("mm", "steps")
        self.unit_combo.clear(); self.unit_combo.addItems(values)
        self._render_home_distance()

    def _render_home_distance(self):
        axis = self.AXIS_LABELS[self.axis_combo.currentText()]
        steps = self.machine.steps_from_home(axis)
        if steps is None:
            detail = "unknown — home this axis first"
        elif axis is Axis.CAROUSEL:
            degrees = self.machine.state.carousel_angle_deg
            detail = f"{steps:+d} logical full steps ({degrees:+.3f}°)"
        else:
            position_mm = self.machine.state.positions[axis.value]
            detail = f"{steps:+d} driver steps ({position_mm:+.3f} mm)"
        self.home_distance_readout.setText(
            f"Commanded distance from {axis.value} home: {detail}"
        )

    @Slot()
    def move_axis(self):
        try:
            self._submit("move axis", self.machine.move_axis, self.AXIS_LABELS[self.axis_combo.currentText()], float(self.move_amount.text()), self.unit_combo.currentText())
        except Exception as exc: self.show_error("Invalid movement", exc)

    def jog(self, amount): self.move_amount.setText(str(amount)); self.move_axis()

    def _vessel_table_changed(self, *_):
        self._vessel_table_dirty = True
        self._update_vessel_status()
        if hasattr(self, "operation_buttons"):
            self._render_state(self.machine.state)

    def _missing_vessel_fields(self, location):
        row = self.vessel_rows[location]
        values = {
            "name": row["name"].text().strip(),
            "category": row["category"].currentText().strip(),
            "density": row["density"].text().strip(),
            "initial quantity": row["quantity"].text().strip(),
        }
        return [label for label, value in values.items() if not value]

    def _vessels_from_table(self):
        vessels = []
        for number, location in enumerate(self.VESSEL_LOCATIONS, 1):
            if self._missing_vessel_fields(location):
                continue
            row = self.vessel_rows[location]
            vessel_type = VesselType.VIAL_15ML if location.startswith("V") else VesselType.BEAKER_100ML
            vessel = Vessel(
                location, number, row["name"].text().strip(),
                self.UI_CATEGORIES[row["category"].currentText()], vessel_type,
                float(row["quantity"].text()), float(row["density"].text()),
                row["name"].text().strip(),
            )
            vessel.validate()
            vessels.append(vessel)
        return vessels

    @Slot()
    def save_vessels(self):
        try:
            inventory = Inventory(self._vessels_from_table())
            self.machine.store.save(inventory)
            self.machine.inventory = inventory
            self._vessel_table_dirty = False
            self._refresh_measurements()
            self._update_vessel_status()
            self._render_state(self.machine.state)
        except Exception as exc: self.show_error("Vessel error", exc)

    def refresh_vessels(self):
        self._refresh_measurements()
        category_labels = {
            VesselCategory.INPUT: "input", VesselCategory.OUTPUT: "output",
            VesselCategory.CLEANING: "wash", VesselCategory.WASTE: "dump",
        }
        for location in self.VESSEL_LOCATIONS:
            vessel = self.machine.inventory.vessels.get(location)
            if vessel is None:
                continue
            row = self.vessel_rows[location]
            widgets = tuple(row.values())
            for widget in widgets:
                widget.blockSignals(True)
            row["name"].setText(vessel.name)
            row["category"].setCurrentText(category_labels[vessel.category])
            row["density"].setText(f"{vessel.density_g_ml:g}")
            row["quantity"].setText(f"{vessel.quantity_ul:g}")
            for widget in widgets:
                widget.blockSignals(False)
        self._update_vessel_status()

    def _clear_vessel_table(self):
        for row in self.vessel_rows.values():
            widgets = tuple(row.values())
            for widget in widgets:
                widget.blockSignals(True)
            row["name"].clear()
            row["category"].setCurrentIndex(0)
            row["density"].clear()
            row["quantity"].clear()
            for widget in widgets:
                widget.blockSignals(False)

    def _vessel_setup_issue(self):
        assigned = {"wash": [], "dump": []}
        incomplete = []
        for location, row in self.vessel_rows.items():
            category = row["category"].currentText()
            if category not in assigned:
                continue
            assigned[category].append(location)
            missing = self._missing_vessel_fields(location)
            if missing:
                incomplete.append(f"{location} {category.title()} missing {', '.join(missing)}")
        if self._vessel_table_dirty:
            counts = f"assigned Wash: {len(assigned['wash'])}, Dump: {len(assigned['dump'])}"
            detail = f" Incomplete assignments: {'; '.join(incomplete)}." if incomplete else ""
            return f"Apply the vessel setup before running commands ({counts}).{detail}"
        physical_vessels = [
            vessel for location, vessel in self.machine.inventory.vessels.items()
            if location in self.VESSEL_LOCATIONS
        ]
        wash = sum(vessel.category is VesselCategory.CLEANING for vessel in physical_vessels)
        dump = sum(vessel.category is VesselCategory.WASTE for vessel in physical_vessels)
        if wash != 1 or dump != 1:
            assigned_counts = f"assigned Wash: {len(assigned['wash'])}, Dump: {len(assigned['dump'])}"
            complete_counts = f"complete Wash: {wash}, Dump: {dump}"
            detail = f" Incomplete assignments: {'; '.join(incomplete)}." if incomplete else ""
            return (
                "Configure exactly one complete Wash and one complete Dump vessel "
                f"({assigned_counts}; {complete_counts}).{detail}"
            )
        return None

    def _update_vessel_status(self):
        issue = self._vessel_setup_issue()
        self.vessel_status.setText(issue or "Vessel setup ready: exactly one Wash and one Dump are configured.")
        self.vessel_status.setStyleSheet(
            "QLabel { color: #b71c1c; font-weight: bold; }" if issue
            else "QLabel { color: #1b5e20; font-weight: bold; }"
        )

    @classmethod
    def _canonical_location(cls, text):
        location = text.strip().upper()
        if location.startswith("V") and location[1:].isdigit():
            location = f"V{int(location[1:]):02d}"
        return location

    def _require_configured_slot(self, text):
        location = self._canonical_location(text)
        if location not in self.vessel_rows:
            raise ValueError(f"{text.strip() or 'The requested location'} is not a physical vessel location.")
        missing = self._missing_vessel_fields(location)
        if len(missing) > 2:
            raise ValueError(f"{location} is an empty slot. Configure the vessel before using it in a command.")
        if missing:
            raise ValueError(f"{location} is missing: {', '.join(missing)}. Add the missing values before using it.")
        if location not in self.machine.inventory.vessels:
            raise ValueError(f"Apply the vessel setup before using {location} in a command.")
        return location

    def _require_command_ready(self, *locations):
        canonical = tuple(self._require_configured_slot(location) for location in locations)
        issue = self._vessel_setup_issue()
        if issue:
            raise ValueError(issue)
        return canonical

    @Slot()
    def reset_session(self):
        answer = QMessageBox.question(self, "Reset session", "Clear persistent vessel/liquid state? A backup will be retained.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            try:
                self.machine.reset_session(confirmed=True)
                self._clear_vessel_table()
                self._vessel_table_dirty = False
                self.refresh_vessels()
                self._render_state(self.machine.state)
            except Exception as exc: self.show_error("Reset failed", exc)

    def _pipette(self): return PipetteId(self.pipette_combo.currentText())
    def _amount(self): return float(self.operation_fields["amount"].text())

    def go_to_source(self):
        try:
            source, = self._require_command_ready(self.operation_fields["source"].text())
            self._submit_guarded("go to spot", self.machine.go_to_spot, source, self._pipette())
        except Exception as exc: self.show_error("Go to source", exc)

    def draw(self):
        try:
            source, = self._require_command_ready(self.operation_fields["source"].text())
            self._submit_guarded("draw", self.machine.draw, source, self._amount(), self._pipette())
        except Exception as exc: self.show_error("Draw", exc)

    def dispense(self):
        try:
            destination, = self._require_command_ready(self.operation_fields["destination"].text())
            self._submit_guarded("dispense", self.machine.dispense, destination, self._amount(), self._pipette())
        except Exception as exc: self.show_error("Dispense", exc)

    def dump(self):
        try:
            self._require_command_ready()
            self._submit_guarded("dump", self.machine.dump, self._pipette())
        except Exception as exc: self.show_error("Dump", exc)

    def clean(self):
        try:
            self._require_command_ready()
            self._submit_guarded("clean", self.machine.clean, self._pipette())
        except Exception as exc: self.show_error("Clean", exc)

    @Slot()
    def transfer(self):
        try:
            source, destination = self._require_command_ready(
                self.operation_fields["source"].text(), self.operation_fields["destination"].text()
            )
            amount, plan = self._amount(), self.machine.plan_transfer(self._amount())
            confirmed, prefer_p1 = True, None
            if plan.requires_confirmation:
                cycles = " + ".join(f"{volume:g}" for volume in plan.cycles_ul)
                answer = QMessageBox.question(self, "Confirm multi-cycle plan", f"Use {plan.pipette.value}: {cycles} µL?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                confirmed = answer == QMessageBox.StandardButton.Yes
                if not confirmed and amount < self.machine.config.transfer.p1_multi_threshold_ul:
                    prefer_p1, confirmed = False, True
            if confirmed:
                self._submit("transfer", self.machine.transfer, source, destination, amount, True, prefer_p1)
        except Exception as exc: self.show_error("Transfer error", exc)

    def _submit_guarded(self, name, function, *args):
        try: self._submit(name, function, *args)
        except Exception as exc: self.show_error(name.title(), exc)

    def _submit(self, name, function, *args): self.worker.submit(name, function, *args)

    @Slot(str, object)
    def _command_completed(self, name, result):
        if name == "limit status":
            self._limit_poll_pending = False
        if name == "calibration unlock":
            self._unlock_pending = False
            self.calibration_unlock.setEnabled(True)
        if name == "move axis":
            self.move_result.setText(f"Requested {result['requested']:g} {result['unit']} → {result['integer_steps']} motor steps; theoretical actual {result['theoretical_actual']:g}; GRBL {result['response']}")
        self.refresh_vessels()

    @Slot(str, object)
    def _command_failed(self, name, error):
        if name == "limit status":
            self._limit_poll_pending = False
            logging.getLogger(__name__).warning("Could not refresh GRBL limit state: %s", error)
            return
        if name == "calibration unlock":
            self._unlock_pending = False
            self.calibration_unlock.setEnabled(True)
            self._set_unlock_checkbox(False)
        self.show_error(name.title(), error)

    @Slot(object)
    def _render_state(self, state):
        self.grbl_connection_button.setText("Disconnect GRBL" if state.grbl_connected else "Connect GRBL")
        self.scale_connection_button.setText("Disconnect Scale" if state.scale_connected else "Connect Scale")
        self.grbl_devices.setEnabled(not state.grbl_connected and not self.mock_mode)
        self.scale_devices.setEnabled(not state.scale_connected and not self.mock_mode)
        issues = self.machine.calibration_issues()
        vessel_issue = self._vessel_setup_issue()
        ready = not issues and not vessel_issue and state.homed and state.position_trusted and state.grbl_connected and not state.abort_requested and not state.current_operation
        for button in self.operation_buttons:
            button.setEnabled(ready)
        if vessel_issue:
            command_status = vessel_issue
        elif issues:
            command_status = "Complete calibration before running programmed commands."
        elif not state.grbl_connected or not state.position_trusted or not state.homed:
            command_status = "Connect GRBL and Home all axes before running programmed commands."
        else:
            command_status = "Programmed commands ready."
        self.operation_status.setText(command_status)
        self.operation_status.setStyleSheet(
            "QLabel { color: #1b5e20; font-weight: bold; }" if ready
            else "QLabel { color: #b71c1c; font-weight: bold; }"
        )
        self.record_measurement_button.setEnabled(not state.current_operation)
        self.calibration_status.setText(
            "Automated operations LOCKED — required: " + "; ".join(issues)
            if issues else "Calibration complete. Home all axes before automated operation." if not ready
            else "Calibration complete; automated operations available."
        )
        reading = "—" if not state.scale_reading else f"{state.scale_reading.mass_g:g} g ({'stable' if state.scale_reading.stable else 'unstable'})"
        operation = state.current_operation or "none"
        if not self._unlock_pending and self.calibration_unlock.isChecked() != state.calibration_unlocked:
            self._set_unlock_checkbox(state.calibration_unlocked)
        homed_axes = ",".join(axis.value for axis in Axis if axis.value in state.homed_axes) or "none"
        self.status_label.setText(f"GRBL {'connected' if state.grbl_connected else 'off'} | Scale {'connected' if state.scale_connected else 'off'} | {state.operation_state.value} ({operation}) | Homed axes {homed_axes} | Fully homed {state.homed} | Trusted {state.position_trusted} | Calibration unlock {state.calibration_unlocked} | X {state.carousel_steps} steps | Scale {reading}")
        limits_available = state.grbl_connected and state.grbl_status_report is not None
        for axis, indicator in self.limit_indicators.items():
            if not limits_available:
                text, background, foreground = f"{axis}  UNKNOWN", "#eeeeee", "#616161"
            elif axis in state.limit_pins_pressed:
                text, background, foreground = f"{axis}  PRESSED", "#c62828", "white"
            else:
                text, background, foreground = f"{axis}  open", "#c8e6c9", "#1b5e20"
            indicator.setText(text)
            indicator.setStyleSheet(
                f"QLabel {{ color: {foreground}; background: {background}; border: 1px solid #9e9e9e; "
                "border-radius: 4px; font-weight: bold; padding: 6px; }"
            )
        self._render_home_distance()
        if not state.scale_connected:
            scale_text, scale_color = "Scale: disconnected", "#616161"
        elif state.scale_reading is None:
            scale_text, scale_color = "Scale: waiting for FZ-500i…", "#e65100"
        else:
            stability = "STABLE" if state.scale_reading.stable else "UNSTABLE"
            scale_text = f"{state.scale_reading.mass_g:+.3f} g   {stability}"
            scale_color = "#1b5e20" if state.scale_reading.stable else "#e65100"
        self.scale_readout.setText(scale_text)
        self.scale_readout.setStyleSheet(
            f"QLabel {{ color: {scale_color}; background: #f5f5f5; border: 1px solid #bdbdbd; "
            "font-size: 26px; font-weight: bold; padding: 8px; }"
        )
        color = {OperationState.IDLE: "#1b5e20", OperationState.BUSY: "#0d47a1", OperationState.ERROR: "#b71c1c", OperationState.RECOVERY_REQUIRED: "#e65100"}[state.operation_state]
        self.status_label.setStyleSheet(f"QLabel {{ color: {color}; font-weight: bold; padding: 4px; }}")

    def show_error(self, title, error):
        logging.getLogger(__name__).error("%s: %s", title, error, exc_info=(type(error), error, error.__traceback__))
        QMessageBox.critical(self, title, f"{type(error).__name__}: {error}")

    @Slot()
    def show_diagnostics(self):
        DiagnosticsDialog(format_diagnostics(environment_diagnostics(self.paths, self.device_preferences, self.serial_devices)), self).exec()

    def closeEvent(self, event: QCloseEvent):
        if self._closing:
            event.accept(); return
        self._closing = True
        self.bridge.stop()
        self.presence_timer.stop()
        self.limit_timer.stop()
        if self.machine.state.current_operation and self.machine.grbl.connected:
            try: self.machine.abort()
            except Exception: logging.getLogger(__name__).exception("Shutdown abort failed")
        self.worker.stop(cancel_pending=True)
        self.machine.shutdown()
        logging.getLogger().removeHandler(self.log_handler)
        logging.shutdown()
        event.accept()


AutosamplerApp = AutosamplerWindow

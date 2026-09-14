from __future__ import annotations

import logging
import queue

from PySide6.QtCore import QObject, QTimer, Signal, Slot, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from autosampler.devices import DeviceRole, SerialDeviceInfo, device_is_present, discover_serial_devices, resolve_identity
from autosampler.diagnostics import environment_diagnostics, format_diagnostics
from autosampler.models import Axis, OperationState, PipetteId
from autosampler.vessels.models import Vessel, VesselCategory, VesselType
from autosampler.worker import MachineWorker


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

    def __init__(self, machine, paths, device_preferences, mock_mode=True):
        super().__init__()
        self.machine = machine
        self.paths = paths
        self.device_preferences = device_preferences
        self.mock_mode = mock_mode
        self._unlock_pending = False
        self._limit_poll_pending = False
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
        outer.addWidget(self._connection_panel())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._scrollable(self._calibration_panel()), "Manual Calibration")
        self.tabs.addTab(self._scrollable(self._vessel_panel()), "Vessels")
        self.tabs.addTab(self._scrollable(self._operations_panel()), "Liquid Commands")
        outer.addWidget(self.tabs, 1)
        outer.addWidget(QLabel("Operation log"))
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(2000)
        outer.addWidget(self.log_output, 1)
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
        actions = (
            ("Connect GRBL", self.connect_grbl),
            ("Disconnect GRBL", lambda: self._submit("disconnect GRBL", self.machine.disconnect_grbl)),
            ("Connect Scale", self.connect_scale),
            ("Disconnect Scale", lambda: self._submit("disconnect scale", self.machine.disconnect_scale)),
        )
        for text, callback in actions:
            button = QPushButton(text)
            button.clicked.connect(callback)
            controls.addWidget(button)
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
        abort = QPushButton("ABORT")
        abort.setStyleSheet("QPushButton { background: #b71c1c; color: white; font-weight: bold; min-height: 42px; }")
        abort.clicked.connect(self.abort)
        controls.addWidget(abort)
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
        layout.setRowStretch(5, 1)
        return panel

    def _vessel_panel(self):
        panel, layout = QWidget(), QVBoxLayout()
        panel.setLayout(layout)
        self.vessel_table = QTableWidget(0, 7)
        self.vessel_table.setHorizontalHeaderLabels(("Location", "Name", "Category", "Type", "Quantity µL", "Density g/mL", "Material"))
        self.vessel_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.vessel_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.vessel_table, 1)
        form_group, form = QGroupBox("Add or update vessel"), QGridLayout()
        form_group.setLayout(form)
        definitions = (
            ("location_id", "Location", "V1"), ("number", "Number", "1"),
            ("name", "Name", "Vial 1"), ("category", "Category", "input"),
            ("vessel_type", "Type", "vial_15ml"), ("quantity_ul", "Quantity µL", "0"),
            ("density_g_ml", "Density g/mL", "1.0"), ("material_name", "Material", "Water"),
        )
        self.vessel_fields = {}
        for index, (key, label, default) in enumerate(definitions):
            row, column = divmod(index, 4)
            field = QLineEdit(default)
            self.vessel_fields[key] = field
            form.addWidget(QLabel(label), row * 2, column)
            form.addWidget(field, row * 2 + 1, column)
        buttons = QHBoxLayout()
        save, reset = QPushButton("Add / Update"), QPushButton("New Session / Reset")
        save.clicked.connect(self.save_vessel)
        reset.clicked.connect(self.reset_session)
        buttons.addWidget(save)
        buttons.addWidget(reset)
        buttons.addStretch()
        form.addLayout(buttons, 4, 0, 1, 4)
        layout.addWidget(form_group)
        self.refresh_vessels()
        return panel

    def _operations_panel(self):
        panel, layout = QWidget(), QVBoxLayout()
        panel.setLayout(layout)
        group, form = QGroupBox("Operation inputs"), QFormLayout()
        group.setLayout(form)
        self.operation_fields = {"source": QLineEdit("V1"), "destination": QLineEdit("V2"), "amount": QLineEdit("100")}
        self.pipette_combo = QComboBox()
        self.pipette_combo.addItems(("P1", "P2"))
        form.addRow("Source location", self.operation_fields["source"])
        form.addRow("Destination location", self.operation_fields["destination"])
        form.addRow("Amount µL", self.operation_fields["amount"])
        form.addRow("Pipette", self.pipette_combo)
        layout.addWidget(group)
        buttons = QGridLayout()
        for index, (text, callback) in enumerate((("Go to source", self.go_to_source), ("Draw", self.draw), ("Dispense", self.dispense), ("Dump", self.dump), ("Clean", self.clean), ("Transfer", self.transfer))):
            button = QPushButton(text)
            button.clicked.connect(callback)
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

    @Slot()
    def save_vessel(self):
        try:
            f = self.vessel_fields
            vessel = Vessel(f["location_id"].text(), int(f["number"].text()), f["name"].text(), VesselCategory(f["category"].text()), VesselType(f["vessel_type"].text()), float(f["quantity_ul"].text()), float(f["density_g_ml"].text()), f["material_name"].text())
            self.machine.inventory.add(vessel)
            self.machine.store.save(self.machine.inventory)
            self.refresh_vessels()
        except Exception as exc: self.show_error("Vessel error", exc)

    def refresh_vessels(self):
        vessels = sorted(self.machine.inventory.vessels.values(), key=lambda vessel: vessel.number)
        self.vessel_table.setRowCount(len(vessels))
        for row, vessel in enumerate(vessels):
            values = (vessel.location_id, vessel.name, vessel.category.value, vessel.vessel_type.value, f"{vessel.quantity_ul:g}", f"{vessel.density_g_ml:g}", vessel.material_name)
            for column, value in enumerate(values): self.vessel_table.setItem(row, column, QTableWidgetItem(value))

    @Slot()
    def reset_session(self):
        answer = QMessageBox.question(self, "Reset session", "Clear persistent vessel/liquid state? A backup will be retained.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            try: self.machine.reset_session(confirmed=True); self.refresh_vessels()
            except Exception as exc: self.show_error("Reset failed", exc)

    def _pipette(self): return PipetteId(self.pipette_combo.currentText())
    def _amount(self): return float(self.operation_fields["amount"].text())
    def go_to_source(self): self._submit_guarded("go to spot", self.machine.go_to_spot, self.operation_fields["source"].text(), self._pipette())
    def draw(self): self._submit_guarded("draw", self.machine.draw, self.operation_fields["source"].text(), self._amount(), self._pipette())
    def dispense(self): self._submit_guarded("dispense", self.machine.dispense, self.operation_fields["destination"].text(), self._amount(), self._pipette())
    def dump(self): self._submit_guarded("dump", self.machine.dump, self._pipette())
    def clean(self): self._submit_guarded("clean", self.machine.clean, self._pipette())

    @Slot()
    def transfer(self):
        try:
            amount, plan = self._amount(), self.machine.plan_transfer(self._amount())
            confirmed, prefer_p1 = True, None
            if plan.requires_confirmation:
                cycles = " + ".join(f"{volume:g}" for volume in plan.cycles_ul)
                answer = QMessageBox.question(self, "Confirm multi-cycle plan", f"Use {plan.pipette.value}: {cycles} µL?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                confirmed = answer == QMessageBox.StandardButton.Yes
                if not confirmed and amount < self.machine.config.transfer.p1_multi_threshold_ul:
                    prefer_p1, confirmed = False, True
            if confirmed:
                self._submit("transfer", self.machine.transfer, self.operation_fields["source"].text(), self.operation_fields["destination"].text(), amount, True, prefer_p1)
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
                "border-radius: 4px; font-weight: bold; padding: 6px; }}"
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
            "font-size: 26px; font-weight: bold; padding: 8px; }}"
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

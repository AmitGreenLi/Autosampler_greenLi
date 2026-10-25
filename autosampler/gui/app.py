from __future__ import annotations

import logging
import math
import os
import queue
import sys
import threading
import time
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
from autosampler.calibration import measurements, get_value, aspiration_measurement_help
from autosampler.remote_serial import DEFAULT_GLOVEBOX_TARGET, discover_remote_devices
from autosampler.errors import OperationAborted, ValidationError

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


class RemoteDiscoveryBridge(QObject):
    completed = Signal(str, str, object)
    failed = Signal(str, str, object)

    def start(self, purpose: str, target: str):
        def run():
            try:
                self.completed.emit(purpose, target, discover_remote_devices(target))
            except Exception as exc:
                self.failed.emit(purpose, target, exc)

        threading.Thread(target=run, name="glovebox-discovery", daemon=True).start()


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
        self._manual_command_pending = False
        self._command_inflight = None
        self._vessel_table_dirty = False
        self.serial_devices = []
        self.remote_discovery = RemoteDiscoveryBridge(self)
        self.remote_discovery.completed.connect(self._remote_discovery_completed)
        self.remote_discovery.failed.connect(self._remote_discovery_failed)
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
        if not self.mock_mode and sys.platform.startswith("linux") and os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            QTimer.singleShot(0, self._probe_glovebox)

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
        self.abort_button.setToolTip("Sends feed hold then RESET. Reset can release motor holding torque and allow gravity-loaded axes to fall. Position becomes untrusted; recovery and Home are required.")
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
        self.device_source = QComboBox()
        self.device_source.addItems(("Local USB", "Glovebox Pi over USB"))
        self.device_source.currentIndexChanged.connect(self.refresh_devices)
        self.glovebox_target = QLineEdit(DEFAULT_GLOVEBOX_TARGET)
        self.glovebox_target.setToolTip("SSH user and USB-network address of the inner Pi")
        self.glovebox_target.editingFinished.connect(self.refresh_devices)
        grid.addWidget(QLabel("Device source"), 0, 0)
        grid.addWidget(self.device_source, 0, 1, 1, 2)
        grid.addWidget(QLabel("Glovebox SSH"), 0, 3)
        grid.addWidget(self.glovebox_target, 0, 4)
        self.grbl_devices, self.scale_devices = QComboBox(), QComboBox()
        self.grbl_devices.setEditable(True)
        self.scale_devices.setEditable(True)
        grid.addWidget(QLabel("GRBL device"), 1, 0)
        grid.addWidget(self.grbl_devices, 1, 1, 1, 3)
        grid.addWidget(QLabel("Scale device"), 2, 0)
        grid.addWidget(self.scale_devices, 2, 1, 1, 3)
        self.refresh_button = QPushButton("Refresh devices")
        self.refresh_button.clicked.connect(self.refresh_devices)
        grid.addWidget(self.refresh_button, 1, 4, 2, 1)
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
        self.home_axis.setToolTip(
            f"Home all runs Z, X, A, then B. Z uses $25={self.machine.config.z_homing_search_rate:g}; "
            f"X searches at {self.machine.config.carousel_homing_search_deg_s:g} degrees/second. "
            "Each restores the previous search setting afterward."
        )
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
        grid.addLayout(controls, 3, 0, 1, 5)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.scale_readout = QLabel("Scale: disconnected")
        self.scale_readout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scale_readout.setMinimumHeight(58)
        grid.addWidget(self.scale_readout, 4, 0, 1, 5)
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
        grid.addLayout(limit_row, 5, 0, 1, 5)
        grid.addWidget(self.status_label, 6, 0, 1, 5)
        self.connection_homing_progress = QLabel()
        self.connection_homing_progress.setWordWrap(True)
        grid.addWidget(self.connection_homing_progress, 7, 0, 1, 5)
        terminal = QGroupBox("Manual G-code command")
        terminal_layout = QVBoxLayout(terminal)
        help_text = QLabel(
            "Available before calibration. Sends the exact command entered (for example G1, $$, $100=1). "
            "Raw commands can move the machine or change settings; check the travel path. "
            "Home is required afterward for automated operations."
        )
        help_text.setWordWrap(True)
        terminal_layout.addWidget(help_text)
        command_row = QHBoxLayout()
        self.manual_command_input = QLineEdit()
        self.manual_command_input.setPlaceholderText("Enter a G-code or controller command")
        self.manual_command_input.setAccessibleName("Manual G-code command")
        self.manual_command_input.returnPressed.connect(self.send_manual_command)
        self.manual_command_input.textChanged.connect(lambda: self._render_manual_controls())
        command_row.addWidget(self.manual_command_input, 1)
        self.manual_send_button = QPushButton("Send")
        self.manual_send_button.clicked.connect(self.send_manual_command)
        command_row.addWidget(self.manual_send_button)
        terminal_layout.addLayout(command_row)
        terminal_layout.addWidget(QLabel("Commands, responses, and errors appear in the operation log below."))
        grid.addWidget(terminal, 8, 0, 1, 5)
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
        move = self.move_button = QPushButton("Move")
        move.clicked.connect(self.move_axis)
        layout.addWidget(move, 1, 3)
        jog = QHBoxLayout()
        self.jog_buttons = []
        for value in (-10, -1, -0.1, 0.1, 1, 10):
            button = QPushButton(f"{value:+g}")
            button.clicked.connect(lambda _checked=False, v=value: self.jog(v))
            self.jog_buttons.append(button)
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
        self.homing_progress_label = QLabel()
        self.homing_progress_label.setWordWrap(True)
        layout.addWidget(self.homing_progress_label, 5, 0, 1, 4)
        group = QGroupBox("Guided calibration — operator measurements")
        form = QVBoxLayout(group)
        instructions = QLabel(
            "1. Home the axis, then use small manual jogs only after checking collision-free travel.\n"
            "2. Physically measure and verify the selected value; commanded steps cannot detect stalls.\n"
            "3. Enter the measurement and confirm it. Repeat for Z, A, B, transfer settings, and the V24 carousel references.\n"
            "Calibrate V24 from X home separately for P1 and P2. All other rack angles are derived automatically.\n"
            "Z home is 0; working positions are negative (e.g. −125 mm). Positive Z distances are converted when recorded; maximum travel is a positive distance.\n"
            "A/B also work in negative coordinates: contact is negative, maximum travel is a positive distance from home, and µL/step is positive.\n"
            "Recording sends no motion or firmware settings. Speed/acceleration values must match verified firmware settings."
        )
        instructions.setWordWrap(True)
        form.addWidget(instructions)
        self.read_firmware_button = QPushButton("Read from GRBL")
        self.read_firmware_button.setToolTip(
            "Read $$ and update Z/A/B firmware maximum speeds and accelerations. No settings are written to GRBL."
        )
        self.read_firmware_button.clicked.connect(
            lambda: self._submit("read firmware calibration", self.machine.read_firmware_calibration)
        )
        form.addWidget(self.read_firmware_button)
        self.firmware_summary = QLabel()
        self.firmware_summary.setWordWrap(True)
        form.addWidget(self.firmware_summary)
        self.measurement_combo = QComboBox()
        self.measurement_combo.currentIndexChanged.connect(self._show_measurement)
        form.addWidget(self.measurement_combo)
        self.measurement_help = QLabel()
        self.measurement_help.setWordWrap(True)
        form.addWidget(self.measurement_help)
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
        self.travel_comparison_button = QPushButton("Record travel comparison using the measurement above")
        self.travel_comparison_button.setToolTip("Uses the selected jog axis: X physical travel in degrees; Z/A/B in mm. Records a comparison in the operation log without changing calibration settings.")
        self.travel_comparison_button.clicked.connect(self.record_travel_comparison)
        form.addWidget(self.travel_comparison_button)
        self.travel_comparison_result = QLabel("Physical travel comparison: none recorded this session")
        self.travel_comparison_result.setWordWrap(True)
        form.addWidget(self.travel_comparison_result)
        layout.addWidget(self._position_panel(), 6, 0, 1, 4)
        layout.addWidget(group, 7, 0, 1, 4)
        layout.addWidget(self._overshoot_panel(), 8, 0, 1, 4)
        layout.setRowStretch(9, 1)
        return panel

    def _overshoot_panel(self):
        group = QGroupBox("Automatic carousel overshoot calibration")
        form = QFormLayout(group)
        note = QLabel("Positioning only: retracts Z to clearance and moves vials between P1 and P2. "
                      "Verify that the entire extended angle range is mechanically clear. No liquid or pipette strokes. "
                      "Reliability requires ≥80% and ≥2 successes at every spot in each direction.")
        note.setWordWrap(True)
        form.addRow(note)
        current = self.machine.config.carousel_overshoot_deg
        self.overshoot_min = QLineEdit("0")
        self.overshoot_max = QLineEdit(f"{max(5, current):g}")
        self.overshoot_step = QLineEdit("1")
        self.overshoot_secondary_min = QLineEdit("0")
        self.overshoot_secondary_max = QLineEdit(f"{max(2, self.machine.config.carousel_secondary_overshoot_deg):g}")
        self.overshoot_secondary_step = QLineEdit("1")
        self.overshoot_repetitions = QLineEdit("3")
        vials = [v.location_id for v in self.machine.inventory.vessels.values() if v.vessel_type is VesselType.VIAL_15ML]
        defaults = (vials[0],) if vials else ("V01",)
        self.overshoot_spots = QLineEdit(", ".join(defaults))
        self.overshoot_spots.setToolTip("One vial spot is sufficient. Optionally enter more comma-separated IDs to verify additional positions.")
        self.overshoot_deadline = QLineEdit("2")
        self.overshoot_stability_hold = QLineEdit("0.5")
        self.overshoot_stability_hold.setToolTip(
            "0 accepts the first fresh balance ST reading after motion. A positive value requires "
            "continuous ST readings for that duration within the trial deadline. Independent of transfer settings.")
        self._overshoot_inputs = (self.overshoot_min, self.overshoot_max, self.overshoot_step,
                                  self.overshoot_secondary_min, self.overshoot_secondary_max, self.overshoot_secondary_step,
                                  self.overshoot_repetitions, self.overshoot_spots, self.overshoot_deadline,
                                  self.overshoot_stability_hold)
        for label, widget in zip(("A primary minimum (°)", "A primary maximum (°)", "A increment (°)",
                                  "B secondary minimum (°)", "B secondary maximum (°)", "B increment (°)",
                                  "Trials per spot and direction", "Vial spot ID (additional spots optional, comma separated)",
                                  "Stabilization deadline (s)", "Stable-reading hold (s; 0 = first fresh stable reading)"), self._overshoot_inputs):
            form.addRow(label, widget)
        self.overshoot_start = QPushButton("Start overshoot calibration")
        self.overshoot_start.clicked.connect(self.start_overshoot_calibration)
        self.overshoot_stop = QPushButton("Stop calibration (fault hold)")
        self.overshoot_stop.setToolTip("Immediately requests hold without reset; cancels queued work and requires recovery/Home.")
        self.overshoot_stop.clicked.connect(self.stop_overshoot_calibration)
        self.overshoot_recover = QPushButton("Recover GRBL connection after Stop / Abort")
        self.overshoot_recover.clicked.connect(self.recover_overshoot_connection)
        self.overshoot_apply = QPushButton("Apply and save recommended A/B pair")
        self.overshoot_apply.clicked.connect(lambda: self._submit_guarded(
            "apply overshoot recommendation", self.machine.apply_overshoot_recommendation))
        for button in (self.overshoot_start, self.overshoot_stop, self.overshoot_recover, self.overshoot_apply):
            form.addRow(button)
        self.overshoot_status = QLabel(self.machine.overshoot_progress)
        self.overshoot_status.setWordWrap(True)
        form.addRow(self.overshoot_status)
        self.overshoot_comparison = QPlainTextEdit()
        self.overshoot_comparison.setReadOnly(True)
        self.overshoot_comparison.setMaximumHeight(180)
        form.addRow(self.overshoot_comparison)
        return group

    def start_overshoot_calibration(self):
        from autosampler.overshoot_calibration import OvershootOptions
        try:
            options = OvershootOptions(tuple(s.strip() for s in self.overshoot_spots.text().split(",") if s.strip()),
                float(self.overshoot_min.text()), float(self.overshoot_max.text()), float(self.overshoot_step.text()),
                int(self.overshoot_repetitions.text()), float(self.overshoot_deadline.text()),
                float(self.overshoot_stability_hold.text()),
                secondary_minimum_deg=float(self.overshoot_secondary_min.text()),
                secondary_maximum_deg=float(self.overshoot_secondary_max.text()),
                secondary_increment_deg=float(self.overshoot_secondary_step.text()))
            options.candidates()
            self.machine.overshoot_result = None
            self.overshoot_comparison.clear()
            self._submit("overshoot calibration", self.machine.calibrate_carousel_overshoot, options)
        except Exception as exc:
            self.show_error("Overshoot calibration", exc)

    def stop_overshoot_calibration(self):
        self.worker.cancel_pending()
        try:
            self.machine.stop_overshoot_calibration()
        except Exception as exc:
            self.show_error("Stop overshoot calibration", exc)
        self._render_state(self.machine.state)

    def recover_overshoot_connection(self):
        answer = QMessageBox.question(
            self, "Recover GRBL after Stop / Abort",
            "This deliberately resets and reconnects GRBL. Reset can release motor holding torque. "
            "Secure gravity-loaded axes first. No movement will be commanded; Home all axes afterward. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._submit_guarded("recover GRBL", self.machine.recover_grbl_connection)

    def _position_panel(self):
        group = QGroupBox("Controller-reported position and displacement")
        grid = QGridLayout(group)
        self.position_coordinate = QComboBox()
        self.position_coordinate.addItem("Machine coordinates (MPos)", "machine")
        self.position_coordinate.addItem("Work coordinates (WPos)", "work")
        self.position_coordinate.currentIndexChanged.connect(lambda: self._render_positions())
        grid.addWidget(self.position_coordinate, 0, 0, 1, 2)
        self.position_status = QLabel()
        self.position_status.setWordWrap(True)
        grid.addWidget(self.position_status, 0, 2, 1, 3)
        for col, title in enumerate(("Axis / units", "Current", "Signed Δ from reference", "Homing", "Measurement reference")):
            grid.addWidget(QLabel(title), 1, col)
        self.position_rows = {}
        self.position_reference_buttons = {}
        for row, axis in enumerate(Axis, 2):
            unit = "logical steps" if axis is Axis.CAROUSEL else "mm"
            grid.addWidget(QLabel(f"{axis.value} · {axis.friendly_name} ({unit})"), row, 0)
            labels = [QLabel() for _ in range(3)]
            for col, label in enumerate(labels, 1):
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                grid.addWidget(label, row, col)
            self.position_rows[axis.value] = labels
            button = QPushButton(f"Set {axis.value} reference")
            button.clicked.connect(lambda checked=False, a=axis.value: self.set_measurement_reference(a))
            grid.addWidget(button, row, 4)
            self.position_reference_buttons[axis.value] = button
        self.all_references_button = QPushButton("Set all references / repeat measurement")
        self.all_references_button.clicked.connect(lambda: self.set_measurement_reference())
        grid.addWidget(self.all_references_button, 6, 0, 1, 5)
        note = QLabel("GUI references only; no coordinate or work-offset changes. Assumes GRBL $13=0. "
                      "Reported values do not verify physical travel without independent position feedback. "
                      "Physical comparison below uses the selected jog axis: X degrees; Z/A/B mm.")
        note.setWordWrap(True)
        grid.addWidget(note, 7, 0, 1, 5)
        return group

    def set_measurement_reference(self, axis=None):
        self._submit("measurement reference", self.machine.set_measurement_reference,
                     self.position_coordinate.currentData(), axis)

    def record_travel_comparison(self):
        try:
            self._submit("travel comparison", self.machine.record_travel_comparison,
                         self.position_coordinate.currentData(), self.AXIS_LABELS[self.axis_combo.currentText()].value,
                         float(self.measurement_value.text()), self.measurement_confirm.isChecked())
            self.measurement_confirm.setChecked(False)
        except Exception as exc:
            self.show_error("Travel comparison", exc)

    def _render_positions(self):
        if not hasattr(self, "position_rows"):
            return
        state = self.machine.state
        tracking = state.reported_position
        coordinate = self.position_coordinate.currentData()
        availability = tracking.availability(state.grbl_connected)
        values = getattr(tracking, coordinate)
        self.position_status.setText(f"{availability} · {tracking.controller_state} · "
                                     f"Homing {'complete' if state.homed else 'incomplete / unhomed'}")
        self.position_status.setStyleSheet("color: #1b5e20;" if availability == "Live" and state.homed else "color: #e65100;")
        available = (availability == "Live" and tracking.controller_state == "Idle"
                     and not state.current_operation and not self._command_inflight)
        for axis in Axis:
            current, delta, homing = self.position_rows[axis.value]
            value = values.get(axis.value)
            displacement = tracking.displacement(coordinate, axis.value)
            suffix = " (stale)" if availability == "Stale" else ""
            current.setText(f"{value:+.3f}{suffix}" if value is not None and availability in ("Live", "Stale") else availability if availability != "Live" else "Unknown")
            delta.setText(f"{displacement:+.3f}{suffix}" if displacement is not None and availability in ("Live", "Stale") else "—")
            homing.setText("Homed" if axis.value in state.homed_axes else "UNHOMED")
            self.position_reference_buttons[axis.value].setEnabled(available and value is not None)
        self.all_references_button.setEnabled(available and all(a.value in values for a in Axis))

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
        explanation = aspiration_measurement_help(key)
        self.measurement_help.setText(explanation)
        self.measurement_help.setVisible(bool(explanation))
        value = get_value(self.machine.config, key) if key else None
        self.measurement_value.setText("" if value is None else str(value))
        self.measurement_value.setPlaceholderText(
            "Signed Z coordinate, e.g. -125 (positive distances are converted)"
            if key in ("z.safe_position_mm", "z.dispense_position_mm", "aspiration.vial_bottom_mm", "aspiration.beaker_bottom_mm")
            else "Enter physically verified measurement in the displayed units"
        )
        if key in ("pipette_1.contact_position_mm", "pipette_2.contact_position_mm"):
            self.measurement_value.setPlaceholderText("Signed A/B contact, e.g. -14.8 (positive distances are converted)")
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
            "Input, Output and Wash require all four values. Dump requires only Name and Category; "
            "its quantity and density are not tracked. Exactly one Wash and one Dump are required."
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
            category.currentIndexChanged.connect(
                lambda _index, slot=location: self._vessel_category_changed(slot)
            )
        layout.addWidget(self.vessel_table, 1)
        buttons = QHBoxLayout()
        save, reset = QPushButton("Apply vessel setup"), QPushButton("New Session / Reset")
        self.apply_vessels_button, self.reset_session_button = save, reset
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
        self.operation_fields = {"source": QComboBox(), "destination": QComboBox(), "amount": QLineEdit("100")}
        for role in ("source", "destination"):
            self.operation_fields[role].setEditable(False)
            self.operation_fields[role].setPlaceholderText("No fully configured vessels available")
        self.pipette_combo = QComboBox()
        self.pipette_combo.addItems(("P1", "P2"))
        form.addRow("Source location", self.operation_fields["source"])
        form.addRow("Destination location", self.operation_fields["destination"])
        form.addRow("Amount µL", self.operation_fields["amount"])
        default_buffer = self.machine.config.transfer.default_draw_buffer_percent
        self.operation_fields["buffer_percent"] = QLineEdit("" if default_buffer is None else f"{default_buffer:g}")
        self._draw_buffer_default = default_buffer
        self._draw_buffer_overridden = False
        self.operation_fields["buffer_percent"].textEdited.connect(self._draw_buffer_edited)
        self.operation_fields["buffer_percent"].setToolTip(
            "Override the calibrated draw buffer for Draw and Transfer. The saved default is unchanged. "
            "Cleaning uses zero buffer."
        )
        buffer_row = QHBoxLayout()
        buffer_row.addWidget(self.operation_fields["buffer_percent"])
        self.use_default_buffer_button = QPushButton("Use default")
        self.use_default_buffer_button.clicked.connect(self._use_default_draw_buffer)
        buffer_row.addWidget(self.use_default_buffer_button)
        form.addRow("Draw buffer (%)", buffer_row)
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
        for role in ("source", "destination"):
            self.operation_fields[role].currentIndexChanged.connect(lambda _index: self._render_state(self.machine.state))
        self.pipette_combo.currentIndexChanged.connect(lambda _index: self._render_state(self.machine.state))
        for field in ("amount", "buffer_percent"):
            self.operation_fields[field].textChanged.connect(self._update_operation_tooltips)
        return panel

    def _update_operation_tooltips(self, *_):
        source = self._selected_operation_location("source") or "the selected source"
        destination = self._selected_operation_location("destination") or "the selected destination"
        pipette = self.pipette_combo.currentText()
        def special(category, fallback):
            matches = self.machine.inventory.by_category(category)
            return matches[0].location_id if len(matches) == 1 else fallback
        wash = special(VesselCategory.CLEANING, "the Wash vessel")
        dump = special(VesselCategory.WASTE, "the Dump vessel")
        amount = "the entered amount"
        buffer = "the entered buffer"
        target = ""
        try:
            volume = self._amount()
            if math.isfinite(volume) and volume > 0:
                amount = f"{volume:g} µL"
            percent = self._draw_buffer()
            buffer = f"{percent:g}% buffer"
            if math.isfinite(volume) and volume > 0 and math.isfinite(volume * (1 + percent / 100)):
                target = f" (target {volume * (1 + percent / 100):g} µL)"
        except (ValueError, OverflowError):
            pass
        cfg = self.machine._pipette_config(self._pipette())
        tips = [
            f"Raise Z to carousel clearance, then align {source} under {pipette}. "
            + (f"A={self.machine.config.carousel_overshoot_deg:g}° beyond the target in the travel direction; "
               f"reverse past the target by B={self.machine.config.carousel_secondary_overshoot_deg:g}°, then return exactly. Wait at every stage. "
               if self.machine.config.carousel_overshoot_deg or self.machine.config.carousel_secondary_overshoot_deg else "")
            + (f"Then allow up to X={self.machine.config.carousel_stability_timeout_s:g}s for a fresh stable scale reading; "
               f"on timeout, jostle once by Y={self.machine.config.carousel_jostle_deg:g}° opposite the last segment and return. "
               "An unavailable scale skips this step with a warning in the operation log. "
               if self.machine.config.carousel_stability_timeout_s > 0 else "")
            + "Leaves Z at clearance; no liquid movement.",
            f"Draw {amount} plus {buffer}{target} from {source} into {pipette}, using scale feedback; then retract Z. Destination is unused.",
            f"Dispense {amount} from liquid already in {pipette} into {destination}, then empty any remainder into {dump}. Source and buffer are unused.",
            f"Empty {pipette} into {dump} using two full emptying strokes, then retract Z. Source, destination, amount and buffer are unused.",
            f"Wash {pipette} at {wash} for {self.machine.config.transfer.default_cleaning_cycles} cycles of {cfg.max_volume_ul:g} µL; return liquid to Wash with zero buffer. Other inputs are unused.",
            f"Transfer {amount} from {source} to {destination} using {buffer} on each draw; dump remainders into {dump}. Pipette and cycle count are planned automatically; the pipette selector is unused.",
        ]
        blockers = getattr(self, "_positioning_tooltip_blockers", [])
        if blockers:
            tips[0] += "\nCurrently locked: " + "; ".join(blockers)
        for button, tip in zip(self.operation_buttons, tips):
            if button.toolTip() != tip:
                button.setToolTip(tip)

    @staticmethod
    def _apply_control_availability(availability):
        """Apply final states only; stable refreshes must not interrupt a press."""
        for widget, enabled in availability:
            if widget.isEnabled() != enabled:
                widget.setEnabled(enabled)

    def _selected_operation_location(self, role):
        return self.operation_fields[role].currentData() or ""

    def _refresh_operation_vessels(self):
        eligible = []
        for vessel in sorted(self.machine.inventory.vessels.values(), key=lambda v: v.number):
            location = vessel.location_id
            if location not in self.vessel_rows or self._missing_vessel_fields(location):
                continue
            row = self.vessel_rows[location]
            if row["category"].currentText() != "dump":
                try:
                    density, quantity = float(row["density"].text()), float(row["quantity"].text())
                    if not math.isfinite(density) or not math.isfinite(quantity) or density <= 0 or not 0 <= quantity <= vessel.capacity_ul:
                        continue
                except ValueError:
                    continue
            eligible.append(vessel)
        for role in ("source", "destination"):
            combo = self.operation_fields[role]
            choices = [
                (f"{v.location_id} — {v.name}", v.location_id) for v in eligible
                if role != "source" or v.category is not VesselCategory.WASTE
            ]
            if choices == [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]:
                continue
            selected = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for label, location in choices:
                combo.addItem(label, location)
            index = combo.findData(selected)
            if index < 0 and choices:
                index = 0
                if role == "destination":
                    index = next((i for i, (_, location) in enumerate(choices) if location != self._selected_operation_location("source")), 0)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    @Slot()
    def refresh_devices(self):
        if self.mock_mode:
            self.serial_devices = []
            self.device_source.setEnabled(False)
            self.glovebox_target.setEnabled(False)
            for combo, text in ((self.grbl_devices, "Mock GRBL"), (self.scale_devices, "Mock scale")):
                combo.clear(); combo.addItem(text, None); combo.setEnabled(False)
            return
        self.glovebox_target.setEnabled(self._remote_mode())
        if self._remote_mode():
            self.serial_devices = []
            self._populate_devices()
            self.remote_discovery.start("refresh", self.glovebox_target.text())
            return
        self.serial_devices = discover_serial_devices()
        self._populate_devices()

    def _remote_mode(self) -> bool:
        return self.device_source.currentIndex() == 1

    def _preference_role(self, role: DeviceRole) -> DeviceRole:
        if not self._remote_mode():
            return role
        return DeviceRole.GRBL_REMOTE if role is DeviceRole.GRBL else DeviceRole.SCALE_REMOTE

    def _populate_devices(self):
        for role, combo in ((DeviceRole.GRBL, self.grbl_devices), (DeviceRole.SCALE, self.scale_devices)):
            combo.setEnabled(True)
            combo.clear(); combo.addItem("Select a serial device…", None)
            for device in self.serial_devices:
                combo.addItem(device.display_name, device)
            identity = self.device_preferences.identity(self._preference_role(role))
            resolution = resolve_identity(identity, self.serial_devices)
            combo.setCurrentIndex(0)
            if resolution.confident and resolution.selected:
                index = next((i for i in range(combo.count()) if combo.itemData(i) == resolution.selected), 0)
                combo.setCurrentIndex(index)
            elif (
                role is DeviceRole.GRBL
                and len(self.serial_devices) == 1
                and (identity is None or (identity.serial_number is None and identity.vid is None))
            ):
                # Make a lone port visible on first setup. Connecting remains
                # an explicit operator action, and Scale is never inferred.
                combo.setCurrentIndex(1)

    def _probe_glovebox(self):
        if not self._remote_mode():
            self.remote_discovery.start("probe", self.glovebox_target.text())

    @Slot(str, str, object)
    def _remote_discovery_completed(self, purpose, target, devices):
        if self._closing:
            return
        if purpose == "refresh":
            if self._remote_mode() and target == self.glovebox_target.text():
                self.serial_devices = devices
                self._populate_devices()
            return
        if purpose == "probe" and not self._remote_mode() and not (
            self.machine.state.grbl_connected or self.machine.state.scale_connected
        ):
            answer = QMessageBox.question(
                self, "Glovebox Pi found",
                f"The glovebox Pi is reachable and reports {len(devices)} serial device(s). "
                "Use its USB devices for GRBL and the scale?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.device_source.setCurrentIndex(1)

    @Slot(str, str, object)
    def _remote_discovery_failed(self, purpose, target, error):
        if self._closing:
            return
        if purpose == "probe":
            logging.getLogger(__name__).info("Glovebox Pi not available: %s", error)
        elif self._remote_mode() and target == self.glovebox_target.text():
            self.show_error("Glovebox discovery", error)

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
        if (self.machine.state.grbl_connected and not str(self.machine.config.grbl.port).startswith("ssh://")
                and not device_is_present(self.machine.config.grbl.port, devices)):
            self.machine.notify_grbl_disconnected()
        if (self.machine.state.scale_connected and not str(self.machine.config.scale.port).startswith("ssh://")
                and not device_is_present(self.machine.config.scale.port, devices)):
            self.machine.notify_scale_disconnected()

    @Slot()
    def _poll_limit_status(self):
        if (not self.machine.state.grbl_connected or self._limit_poll_pending
                or self._command_inflight or self.worker.busy or self.machine.state.current_operation):
            return
        self._limit_poll_pending = True
        self._submit("limit status", self.machine.refresh_grbl_status)

    @Slot()
    def connect_grbl(self):
        try:
            if not self.mock_mode:
                device = self._selected_device(self.grbl_devices, "GRBL")
                if self._remote_mode() and not device.device.startswith("ssh://"):
                    raise ValueError("Select a discovered GRBL device on the glovebox Pi.")
                self.machine.config.grbl.port = device.device
                self.device_preferences.associate(self._preference_role(DeviceRole.GRBL), device)
            self._submit("connect GRBL", self.machine.connect_grbl)
        except Exception as exc: self.show_error("GRBL selection", exc)

    @Slot()
    def connect_scale(self):
        try:
            if not self.mock_mode:
                device = self._selected_device(self.scale_devices, "scale")
                if self._remote_mode() and not device.device.startswith("ssh://"):
                    raise ValueError("Select a discovered scale device on the glovebox Pi.")
                self.machine.config.scale.port = device.device
                self.device_preferences.associate(self._preference_role(DeviceRole.SCALE), device)
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
        self.worker.cancel_pending()
        try: self.machine.abort()  # bypass FIFO so Abort is never queued
        except Exception as exc: self.show_error("Abort", exc)
        self._render_state(self.machine.state)

    @Slot()
    def home_selected(self):
        axis = self.home_axis.currentData()
        self._submit(f"home {axis or 'all axes'}", self.machine.home, axis)

    @Slot(bool)
    def toggle_calibration_unlock(self, enabled):
        if self._command_inflight or self.machine.state.current_operation or self.machine.grbl.recovery_required:
            self._set_unlock_checkbox(self.machine.state.calibration_unlocked)
            return
        if enabled:
            answer = QMessageBox.warning(
                self,
                "Calibration unlock",
                "Send GRBL $X and allow relative manual jogging without homing?\n\n"
                "Machine coordinates will remain UNKNOWN. Automated commands stay disabled. "
                "Confirm the carriage can move safely before continuing. X jogging requires Z homed and at collision-free clearance.",
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

    def _vessel_category_changed(self, location):
        row = self.vessel_rows[location]
        category = row["category"].currentText()
        name = row["name"]
        if category in ("wash", "dump") and name.text().strip().lower() in ("", "wash", "dump"):
            name.blockSignals(True)
            name.setText(category.title())
            name.blockSignals(False)
        self._vessel_table_changed()

    def _vessel_table_changed(self, *_):
        self._vessel_table_dirty = True
        self._update_dump_fields()
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
        if values["category"] == "dump":
            values.pop("density")
            values.pop("initial quantity")
        return [label for label, value in values.items() if not value]

    def _update_dump_fields(self):
        for row in self.vessel_rows.values():
            dump = row["category"].currentText() == "dump"
            for key in ("density", "quantity"):
                field = row[key]
                field.setEnabled(not dump)
                field.setToolTip("Not tracked for mixed waste" if dump else "")
                if dump:
                    field.blockSignals(True)
                    field.clear()
                    field.blockSignals(False)

    def _vessels_from_table(self):
        vessels = []
        for number, location in enumerate(self.VESSEL_LOCATIONS, 1):
            if self._missing_vessel_fields(location):
                continue
            row = self.vessel_rows[location]
            vessel_type = VesselType.VIAL_15ML if location.startswith("V") else VesselType.BEAKER_100ML
            dump = row["category"].currentText() == "dump"
            vessel = Vessel(
                location, number, row["name"].text().strip(),
                self.UI_CATEGORIES[row["category"].currentText()], vessel_type,
                None if dump else float(row["quantity"].text()), None if dump else float(row["density"].text()),
                row["name"].text().strip(),
            )
            vessel.validate()
            vessels.append(vessel)
        return vessels

    @Slot()
    def save_vessels(self):
        try:
            if self._command_inflight or self.worker.busy or self.machine.state.current_operation:
                raise ValidationError("Wait for the current operation to finish.")
            inventory = Inventory(self._vessels_from_table())
            self.machine.apply_inventory(inventory)
            self._vessel_table_dirty = False
            self._refresh_measurements()
            self._update_vessel_status()
            self._render_state(self.machine.state)
        except Exception as exc: self.show_error("Vessel error", exc)

    def refresh_vessels(self):
        self._refresh_measurements()
        if self._vessel_table_dirty:
            # Treat the table as an editing draft until Apply or explicit Reset.
            # Polling and unrelated command completions must never reload over it.
            self._update_vessel_status()
            return
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
            row["density"].setText("" if vessel.density_g_ml is None else f"{vessel.density_g_ml:g}")
            row["quantity"].setText("" if vessel.quantity_ul is None else f"{vessel.quantity_ul:g}")
            for widget in widgets:
                widget.blockSignals(False)
        self._update_dump_fields()
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

        self._update_dump_fields()

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
        uncertain = self.machine.inventory.unverified_sources
        dump_required = self.machine.inventory.dump_required
        if uncertain or dump_required:
            issue = (issue + " " if issue else "") + (
                f"Unverified liquid state. Dump required: {', '.join(sorted(dump_required)) or 'none'}. "
                f"Physically verify quantities and Apply vessel setup: {', '.join(sorted(uncertain)) or 'none'}."
            )
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
        if self._command_inflight or self.worker.busy or self.machine.state.current_operation:
            return
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

    def _draw_buffer_edited(self, _text):
        self._draw_buffer_overridden = True

    def _use_default_draw_buffer(self):
        self._draw_buffer_overridden = False
        self._draw_buffer_default = self.machine.config.transfer.default_draw_buffer_percent
        value = self._draw_buffer_default
        self.operation_fields["buffer_percent"].setText("" if value is None else f"{value:g}")

    def _draw_buffer(self):
        value = float(self.operation_fields["buffer_percent"].text())
        if not math.isfinite(value) or value < 0:
            raise ValueError("Draw buffer must be a finite, non-negative percentage.")
        return value

    def go_to_source(self):
        try:
            source = self._require_configured_slot(self._selected_operation_location("source"))
            if self._vessel_table_dirty:
                raise ValueError("Apply the vessel setup before positioning.")
            self._submit_guarded("go to spot", self.machine.go_to_spot, source, self._pipette())
        except Exception as exc: self.show_error("Go to source", exc)

    def draw(self):
        try:
            source, = self._require_command_ready(self._selected_operation_location("source"))
            self._submit_guarded("draw", self.machine.draw, source, self._amount(), self._pipette(), self._draw_buffer())
        except Exception as exc: self.show_error("Draw", exc)

    def dispense(self):
        try:
            destination, = self._require_command_ready(self._selected_operation_location("destination"))
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
                self._selected_operation_location("source"), self._selected_operation_location("destination")
            )
            buffer_percent = self._draw_buffer()
            amount, plan = self._amount(), self.machine.plan_transfer(self._amount(), buffer_percent=buffer_percent)
            confirmed, prefer_p1 = True, None
            if plan.requires_confirmation:
                cycles = " + ".join(f"{volume:g}" for volume in plan.cycles_ul)
                answer = QMessageBox.question(self, "Confirm multi-cycle plan", f"Use {plan.pipette.value}: {cycles} µL?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                confirmed = answer == QMessageBox.StandardButton.Yes
                if not confirmed and self.machine.config.pipette_1.max_volume_ul < amount < self.machine.config.transfer.p1_multi_threshold_ul:
                    alternate = self.machine.plan_transfer(amount, False, buffer_percent)
                    if not alternate.requires_confirmation:
                        prefer_p1, confirmed = False, True
            if confirmed:
                self._submit("transfer", self.machine.transfer, source, destination, amount, True, prefer_p1, buffer_percent)
        except Exception as exc: self.show_error("Transfer error", exc)

    def _submit_guarded(self, name, function, *args):
        try: self._submit(name, function, *args)
        except Exception as exc: self.show_error(name.title(), exc)

    def _submit(self, name, function, *args):
        if self._command_inflight or self.machine.state.current_operation:
            if name == "limit status":
                self._limit_poll_pending = False
            else:
                logging.getLogger(__name__).warning("%s ignored: a machine command is already pending or running.", name)
            return False
        if name != "limit status":
            self._command_inflight = name
        generation = self.machine._abort_generation
        def execute():
            if generation != self.machine._abort_generation:
                raise OperationAborted("Request cancelled by Abort/disconnection before execution.")
            if function == self.machine.home:
                return function(*args, abort_generation=generation)
            if function == self.machine.unlock_for_calibration:
                return function(abort_generation=generation)
            return function(*args)
        try:
            self.worker.submit(name, execute, cancel_on_error=name != "limit status")
        except Exception:
            if name != "limit status":
                self._command_inflight = None
            raise
        self._render_state(self.machine.state)
        return True

    def _render_manual_controls(self):
        state = self.machine.state
        available = (state.grbl_connected and self.machine.grbl.connected
                     and not state.abort_requested and not state.current_operation
                     and not self._command_inflight and not self.machine.grbl.recovery_required
                     and not self._manual_command_pending)
        self.manual_command_input.setEnabled(available)
        self.manual_send_button.setEnabled(available and bool(self.manual_command_input.text().strip()))

    @Slot()
    def send_manual_command(self):
        self._render_manual_controls()
        if not self.manual_send_button.isEnabled():
            return
        command = self.manual_command_input.text()
        generation = self.machine._abort_generation
        self._manual_command_pending = True
        try:
            self._submit(
                "manual command", self.machine.send_manual_command, command,
                None,
                generation,
            )
            self.manual_command_input.clear()
        except Exception as exc:
            self._manual_command_pending = False
            logging.getLogger(__name__).error("Manual command: %s: %s", type(exc).__name__, exc)
        self._render_manual_controls()

    @Slot(str, object)
    def _command_completed(self, name, result):
        if self._command_inflight == name:
            self._command_inflight = None
        if name == "record calibration":
            self._show_measurement()  # show the signed coordinate actually saved
        if name == "overshoot calibration":
            recommendation = (f"Recommended: A={result.recommendation_deg:g}°, B={result.recommendation_secondary_deg:g}° — review, then Apply and save."
                              if result.recommendation_pair is not None else "No reliable A/B pair found; nothing saved.")
            rows = [recommendation, "A | B | successes | median seconds | std dev | reliable across all groups"]
            rows.extend(f"{s.angle_deg:g}° | {s.secondary_deg:g}° | {s.successes}/{s.total} | {s.median_s:.3f} | {s.spread_s:.3f} | {'yes' if s.reliable else 'no'}"
                        for s in result.candidates)
            self.overshoot_comparison.setPlainText("\n".join(rows))
        if name == "apply overshoot recommendation":
            self.overshoot_comparison.appendPlainText(f"Applied and saved A={result[0]:g}°, B={result[1]:g}°.")
        if name == "travel comparison":
            self.travel_comparison_result.setText(
                f"Recorded {result['axis']} ({result['coordinate']}): controller-reported Δ {result['reported_displacement']:+.3f} {result['unit']}; "
                f"physically measured {result['physical_distance']:+.3f} {result['unit']}")
        if name == "manual command":
            self._manual_command_pending = False
            self._render_manual_controls()
            self.manual_command_input.setFocus()
        if name == "limit status":
            self._limit_poll_pending = False
            return  # status polling does not change vessel data
        if name == "calibration unlock":
            self._unlock_pending = False
            self.calibration_unlock.setEnabled(True)
        if name == "move axis":
            self.move_result.setText(f"Requested {result['requested']:g} {result['unit']} → {result['integer_steps']} motor steps; theoretical actual {result['theoretical_actual']:g}; GRBL {result['response']}")
        self.refresh_vessels()

    @Slot(str, object)
    def _command_failed(self, name, error):
        if self._command_inflight == name:
            self._command_inflight = None
        if name == "overshoot calibration":
            self.overshoot_comparison.setPlainText(f"Calibration stopped: {error}. Previous overshoot restored; nothing saved.")
        if name == "manual command":
            self._manual_command_pending = False
            self._render_manual_controls()
            return
        if name == "limit status":
            self._limit_poll_pending = False
            logging.getLogger(__name__).warning("Could not refresh GRBL limit state: %s", error)
            return
        if name == "calibration unlock":
            self._unlock_pending = False
            self.calibration_unlock.setEnabled(True)
            self._set_unlock_checkbox(False)
        if isinstance(error, OperationAborted):
            logging.getLogger(__name__).warning("%s: %s", name, error)
            return
        self.show_error(name.title(), error)

    @Slot(object)
    def _render_state(self, state):
        self._refresh_operation_vessels()
        if self.machine.config.transfer.default_draw_buffer_percent != self._draw_buffer_default:
            if not self._draw_buffer_overridden:
                self._use_default_draw_buffer()
            else:
                self._draw_buffer_default = self.machine.config.transfer.default_draw_buffer_percent
        self.read_firmware_button.setEnabled(state.grbl_connected and not state.current_operation)
        def firmware_value(value):
            return "not read" if value is None else f"{value:g}"
        self.firmware_summary.setText("Firmware maximum speed (mm/min) / acceleration (mm/s²): " + " | ".join(
            f"{axis}: {firmware_value(speed)} / {firmware_value(acceleration)}"
            for axis, speed, acceleration in (
                ("Z", self.machine.config.z.motion_speed_mm_min, self.machine.config.z.acceleration_mm_s2),
                ("A", self.machine.config.pipette_1.speed_mm_min, self.machine.config.pipette_1.acceleration_mm_s2),
                ("B", self.machine.config.pipette_2.speed_mm_min, self.machine.config.pipette_2.acceleration_mm_s2),
            )
        ))
        self._render_positions()
        self._render_manual_controls()
        busy = bool(self._command_inflight or state.current_operation)
        recovery_hint = ""
        if not busy:
            if self.machine.grbl.recovery_required:
                recovery_hint = "\nTo restart: Recover GRBL connection below, then Home all axes using the Home control."
            elif not state.position_trusted or state.abort_requested:
                recovery_hint = "\nTo start: connect GRBL if needed, then Home all axes using the Home control."
            elif not self.machine.scale.connected:
                recovery_hint = "\nConnect the scale before starting."
        self.overshoot_status.setText(self.machine.overshoot_progress + recovery_hint)
        overshoot_active = self._command_inflight == "overshoot calibration" or self.machine._overshoot_running
        self.overshoot_stop.setEnabled(overshoot_active and not state.abort_requested)
        self.overshoot_recover.setEnabled(not busy and (self.machine.grbl.recovery_required or state.abort_requested))
        self.overshoot_start.setEnabled(not busy and state.position_trusted and self.machine.grbl.connected
                                        and self.machine.scale.connected and not self.machine.grbl.recovery_required
                                        and not state.abort_requested)
        for widget in self._overshoot_inputs:
            widget.setEnabled(not busy)
        result = self.machine.overshoot_result
        self.overshoot_apply.setEnabled(not busy and result is not None and result.recommendation_deg is not None)
        recovery = self.machine.grbl.recovery_required
        connected = state.grbl_connected
        self.home_button.setEnabled(connected and not busy and not recovery)
        self.home_axis.setEnabled(not busy)
        self.calibration_unlock.setEnabled(connected and not busy and not recovery and not self._unlock_pending)
        self.grbl_connection_button.setEnabled(not busy)
        self.scale_connection_button.setEnabled(not busy)
        can_jog = (connected and not busy and not recovery and not state.abort_requested
                   and (state.calibration_unlocked or self.AXIS_LABELS[self.axis_combo.currentText()].value in state.homed_axes))
        for button in (self.move_button, *self.jog_buttons):
            button.setEnabled(can_jog)
        self.homing_progress_label.setVisible(bool(state.homing_progress) or recovery)
        progress = state.homing_progress or ""
        if state.homing_axis_started_at is not None:
            elapsed = int(time.monotonic() - state.homing_axis_started_at)
            timeout = getattr(self.machine.grbl, "HOMING_RESPONSE_TIMEOUT_S", 300)
            progress += f" · {elapsed}s elapsed (up to {timeout:g}s per cycle). Abort cancels homing."
        elif recovery:
            progress += (" Commands blocked; diagnostic status remains available. Motion stop / holding torque unconfirmed. "
                         "Secure gravity-loaded axes before Abort/reset or disconnect/reconnect; Home is required afterward.")
        self.homing_progress_label.setText(progress)
        self.homing_progress_label.setStyleSheet("color: #b71c1c;" if recovery else "color: #0d47a1;")
        self.connection_homing_progress.setText(progress)
        self.connection_homing_progress.setVisible(bool(state.homing_progress) or recovery)
        self.connection_homing_progress.setStyleSheet(self.homing_progress_label.styleSheet())
        self.grbl_connection_button.setText("Disconnect GRBL" if state.grbl_connected else "Connect GRBL")
        self.scale_connection_button.setText("Disconnect Scale" if state.scale_connected else "Connect Scale")
        self.grbl_devices.setEnabled(not state.grbl_connected and not self.mock_mode)
        self.scale_devices.setEnabled(not state.scale_connected and not self.mock_mode)
        can_change_source = not self.mock_mode and not state.grbl_connected and not state.scale_connected
        self.device_source.setEnabled(can_change_source)
        self.glovebox_target.setEnabled(can_change_source and self._remote_mode())
        issues = self.machine.calibration_issues()
        vessel_issue = self._vessel_setup_issue()
        ready = not issues and not vessel_issue and state.homed and state.position_trusted and state.grbl_connected and not state.abort_requested and not busy and not recovery
        source = self._selected_operation_location("source")
        positioning_issues = self.machine.positioning_issues(source, self._pipette())
        source_complete = source in self.vessel_rows and not self._missing_vessel_fields(source)
        positioning_blockers = list(positioning_issues)
        if source not in self.vessel_rows:
            positioning_blockers.append("Select a physical source location: V01–V24, B1 or B2")
        elif not source_complete:
            positioning_blockers.append(f"Complete {source}: {', '.join(self._missing_vessel_fields(source))}")
        if self._vessel_table_dirty:
            positioning_blockers.append("Apply vessel setup")
        if not state.grbl_connected or not self.machine.grbl.connected:
            positioning_blockers.append("Connect GRBL")
        if not state.homed or not state.position_trusted or state.abort_requested:
            positioning_blockers.append("Home all axes to establish trusted positions")
        if busy:
            positioning_blockers.append("Wait for the pending/running command to finish")
        if recovery:
            positioning_blockers.append("Resolve the GRBL recovery-required state")
        can_position = not positioning_blockers
        # Calculate every final action state before touching any control. In
        # particular, positioning is independently available during calibration;
        # applying a blanket liquid lock first would cancel an in-progress press.
        availability = [(self.operation_buttons[0], can_position)] + [
            (button, ready) for button in self.operation_buttons[1:]
        ]
        self._apply_control_availability(availability)
        self._positioning_tooltip_blockers = positioning_blockers
        self._update_operation_tooltips()
        if vessel_issue:
            command_status = vessel_issue
        elif issues:
            command_status = "Complete calibration before running programmed commands."
        elif not state.grbl_connected or not state.position_trusted or not state.homed:
            command_status = "Connect GRBL and Home all axes before running programmed commands."
        else:
            command_status = "Programmed commands ready."
        if can_position and not ready:
            command_status = "Go to source is available for positioning tests (Z stays at clearance). Liquid commands: " + command_status
        elif not can_position:
            command_status = "Go to source locked: " + "; ".join(positioning_blockers) + ". Liquid commands: " + command_status
        if self.operation_status.text() != command_status:
            self.operation_status.setText(command_status)
        status_style = (
            "QLabel { color: #1b5e20; font-weight: bold; }" if ready
            else "QLabel { color: #b71c1c; font-weight: bold; }"
        )
        if self.operation_status.styleSheet() != status_style:
            self.operation_status.setStyleSheet(status_style)
        self.record_measurement_button.setEnabled(not busy)
        self.travel_comparison_button.setEnabled(not busy and not recovery)
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
        self.apply_vessels_button.setEnabled(not busy)
        self.reset_session_button.setEnabled(not busy)
        limits_available = (state.grbl_connected and state.grbl_status_report is not None
                            and state.grbl_status_updated_at is not None
                            and time.monotonic() - state.grbl_status_updated_at <= state.reported_position.STALE_SECONDS
                            and not busy
                            and state.grbl_status_report[1:].split("|", 1)[0].rstrip(">").split(":", 1)[0]
                            in ("Idle", "Run", "Jog", "Hold", "Door", "Check", "Sleep"))
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

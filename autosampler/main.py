from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from autosampler.app_paths import ApplicationPaths
from autosampler.config import MachineConfig
from autosampler.controllers.grbl import SerialGrblController
from autosampler.controllers.mock_grbl import MockGrblController
from autosampler.controllers.mock_scale import MockScaleController
from autosampler.controllers.scale import SerialScaleController
from autosampler.devices import DevicePreferenceStore, DeviceRole, discover_serial_devices, resolve_identity
from autosampler.logging_config import configure_logging
from autosampler.machine import AutosamplerMachine


def build_runtime(mock_mode=True, paths=None, device_config_path=None):
    paths = paths or ApplicationPaths.platform_default()
    paths.ensure()
    legacy_state = Path(__file__).resolve().parent.parent / "autosampler_state.json"
    paths.migrate_legacy_session(legacy_state)
    config = MachineConfig(state_file=paths.session_state, log_file=paths.log_file)
    configure_logging(config.log_file)
    preferences = DevicePreferenceStore(device_config_path or paths.device_preferences)
    devices = [] if mock_mode else discover_serial_devices()
    if not mock_mode:
        grbl_match = resolve_identity(preferences.identity(DeviceRole.GRBL), devices)
        scale_match = resolve_identity(preferences.identity(DeviceRole.SCALE), devices)
        if grbl_match.confident:
            config.grbl.port = grbl_match.selected.device
        if scale_match.confident:
            config.scale.port = scale_match.selected.device
    if mock_mode:
        grbl, scale = MockGrblController(), MockScaleController()
    else:
        grbl, scale = SerialGrblController(config.grbl), SerialScaleController(config.scale)
    return AutosamplerMachine(config, grbl, scale), paths, preferences


def build_machine(hardware=False):
    """Compatibility helper; safe simulation remains the default."""
    return build_runtime(mock_mode=not hardware)[0]


def _parser():
    parser = argparse.ArgumentParser(description="DIY CNC autosampler PySide6 GUI")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", help="use simulated GRBL and scale (default)")
    mode.add_argument("--hardware", action="store_true", help="enable real serial-device selection")
    parser.add_argument("--fullscreen", action="store_true", help="start in fullscreen touchscreen-friendly mode")
    parser.add_argument("--config", type=Path, help="override the serial-device preference file")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    mock_mode = not args.hardware
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from autosampler.gui import AutosamplerWindow

    application = QApplication(sys.argv[:1] if argv is not None else sys.argv)
    application.setApplicationName("DIY CNC Autosampler")
    machine, paths, preferences = build_runtime(mock_mode, device_config_path=args.config)
    window = AutosamplerWindow(machine, paths, preferences, mock_mode)
    window.showFullScreen() if args.fullscreen else window.show()

    def request_close(*_):
        QTimer.singleShot(0, window.close)

    for signame in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signame):
            signal.signal(getattr(signal, signame), request_close)
    signal_timer = QTimer()
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(500)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())

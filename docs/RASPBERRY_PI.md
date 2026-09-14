# Raspberry Pi deployment preparation

## Target

The intended target is a 64-bit ARM Linux Raspberry Pi with a locally attached display or touchscreen. Machine logic does not assume a particular Pi model or desktop environment. Deployment must be tested on the actual target; Windows wheels and virtual environments cannot be copied to ARM64 Linux.

## Python environment

Use Python 3.11 or newer on the Pi and create the environment on the Pi itself:

```bash
sudo apt update
sudo apt install python3-venv python3-pip
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[test]'
```

The normal runtime dependencies are PySide6, pySerial, and platformdirs. `pytest` is only needed for software verification. Run the mock suite on the Pi before connecting hardware:

```bash
.venv/bin/python -m pytest
```

## PySide6 / Qt 6

Prefer the official PySide6 ARM64 wheel when it supports the installed Python/OS combination. If no compatible wheel exists for the selected Raspberry Pi OS release, use the distribution's Qt 6/PySide6 packages and install this project without replacing those packages. Do not copy the Windows PySide6 installation: Qt includes platform-specific native libraries and plugins.

A graphical session and a working Qt platform plugin are required. For a local X11 or Wayland desktop, launch from that user's graphical session. Offscreen testing can use `QT_QPA_PLATFORM=offscreen`, but that is not a production display mode.

## Serial devices

Linux commonly exposes USB serial devices as:

- `/dev/ttyACM*`
- `/dev/ttyUSB*`
- stable aliases beneath `/dev/serial/by-id/`

The application enumerates devices with pySerial, displays their USB metadata, and saves GRBL/scale identities separately from the assigned path. It will not select the first enumerated device. If a saved identity is missing or ambiguous, the operator must choose. Reconnecting never restores trusted coordinates; Home is required.

For the A&D FZ-500i RS-232-to-USB adapter, retain the same balance-side profile used during Windows development: 2400 baud, 7 data bits, even parity, one stop bit, CR/LF, and A&D standard format. The controller sends `Q` queries and normalizes reported mass to grams. Prefer the adapter's `/dev/serial/by-id/...` alias when available, but save its USB identity through the application as well.

## Serial permissions

The application never invokes `sudo` or changes system permissions. The service/desktop user must already be permitted to open the selected serial devices. On Raspberry Pi OS this commonly means membership in `dialout`:

```bash
sudo usermod -aG dialout "$USER"
```

Log out and back in (or reboot) before testing the new membership. Confirm with `groups` and inspect the device with `ls -l /dev/ttyACM0`. Distribution policies can differ; use the group shown on the actual device node. Permission-denied errors are reported distinctly by the application.

## Startup and fullscreen

Windowed safe simulation:

```bash
.venv/bin/python -m autosampler.main --mock
```

Real device-selection mode in fullscreen:

```bash
.venv/bin/python -m autosampler.main --hardware --fullscreen
```

For automatic graphical startup, first validate manual startup. Then use the desktop environment's per-user autostart mechanism, or a user service that starts only after the graphical session is available. An optional systemd user-service command can invoke the same module and flags, with `WorkingDirectory` omitted because startup does not depend on it. Ensure the service receives the correct display-session environment; a system service launched before the display is usually inappropriate for the GUI.

## Application data and logs

`platformdirs` selects user-writable data, configuration, and log directories. Source/install directories are not used for normal writes. The Diagnostics dialog reports the exact paths on the running Pi. Back up calibration, device preferences, and session data before OS upgrades.

## Clean shutdown

Closing the window, SIGINT, and SIGTERM follow the same shutdown path: stop accepting work, abort an active GRBL workflow if necessary, clear queued work, save logical vessel state, disconnect scale and GRBL, stop the worker, flush logs, and exit Qt. No shutdown path starts machine movement.

For a standalone appliance, arrange an orderly OS shutdown before removing power. The software Abort is not a substitute for the machine's physical emergency-stop circuit.

## Work remaining on the real Pi

- Confirm the chosen Raspberry Pi OS/Python combination has a compatible PySide6 build.
- Verify the display platform, DPI scaling, touchscreen calibration, and fullscreen behavior.
- Grant and test serial permissions for the intended application user.
- Select and save the actual GRBL and scale USB identities.
- Configure the real scale baud rate and protocol parser.
- Verify USB reconnect behavior and stable `/dev/serial/by-id` aliases.
- Decide whether the carousel reference pulse remains controller-driven or gains a GPIO adapter implementing `CarouselReferenceSensor`.
- Validate SIGTERM and desktop shutdown while idle and during a simulated operation.
- Only after software/mock validation, perform the hardware-verification checklist in the main README.

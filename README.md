# DIY CNC Autosampler Control Software

Cross-platform Python control software for a GRBL Mega 5X Edge autosampler, an independent serial scale, and a responsive PySide6/Qt 6 operator interface. Simulation is the safe default: it does not enumerate, connect to, or move real hardware.

## Run the application

Install Python 3.11 or newer, then create an environment and install the project:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

On Linux, use `.venv/bin/python` instead of `.venv\Scripts\python.exe`.

Start safe simulation mode:

```powershell
.\.venv\Scripts\python.exe -m autosampler.main --mock
```

Start real serial-device selection mode, optionally fullscreen:

```powershell
.\.venv\Scripts\python.exe -m autosampler.main --hardware
.\.venv\Scripts\python.exe -m autosampler.main --hardware --fullscreen
```

The program no longer assumes `COM4`. In hardware mode, select the GRBL and scale roles from discovered devices. A saved USB identity is recognized if the OS later assigns a different `COM` or `/dev/tty*` path. Ambiguous devices are never chosen automatically.

## Architecture

```text
Qt main thread
  |- responsive layouts, dialogs, table, status, log
  |- QtWorkerBridge signals
  `- safety Abort (bypasses normal FIFO)
               |
               v
framework-neutral MachineWorker queue
               |
               v
AutosamplerMachine and reusable commands
        /                     \
GRBL interface           Scale interface
  |- serial                 |- serial
  `- deterministic mock    `- deterministic mock
```

The controller, machine, inventory, geometry, persistence, planning, and calibration modules do not import PySide6 and can be tested without constructing `QApplication`. Worker threads never modify widgets.

Important modules:

- `autosampler/gui/app.py`: PySide6 main window and Qt signal bridge.
- `autosampler/machine.py`: unchanged command composition and safety semantics.
- `autosampler/devices.py`: serial enumeration, identity matching, ambiguity handling, and persisted role associations.
- `autosampler/app_paths.py`: centralized platform-appropriate writable paths and legacy session migration.
- `autosampler/controllers/`: real and mock GRBL/scale boundaries.
- `autosampler/reference_sensor.py`: source-neutral carousel reference interface for a future GPIO adapter.
- `autosampler/diagnostics.py`: OS/runtime/device/path diagnostics shown by the GUI.

## Persistent data

Runtime state is not written into the installed source directory. `platformdirs` chooses separate user data, configuration, and log directories. The Diagnostics dialog reports their exact locations.

- Current vessel/session state: user data directory
- GRBL/scale device associations: user configuration directory
- Operational log: user log directory
- Machine constants: typed project configuration
- Operator-confirmed calibration: `calibration.json` in the user configuration directory (`calibration-mock.json` in simulation)

If the original development `autosampler_state.json` exists beside the source, it is copied to the new session location on first startup and retained as a legacy backup. Physical coordinates are never persisted as trusted state.

## Device discovery and reconnection

Discovery captures the OS device path, description, HWID, VID, PID, USB serial number, manufacturer, product, and location where available. Matching prioritizes serial number and USB metadata. A previous port name alone is insufficient for automatic selection.

If GRBL disappears, the failed operation stops, the controller is marked disconnected, all coordinates become untrusted, and recovery/Home is required after reconnection. Scale loss stops measurement-dependent work and never fabricates a reading. Linux permission failures include a specific diagnostic; the application never changes groups or invokes `sudo`.

## Preserved machine behavior

- Axis mapping remains X carousel, Z carriage, A Pipette 1, B Pipette 2.
- Homing can run the complete `$H` cycle or one GRBL-Mega-5X axis command (`$HX`, `$HZ`, `$HA`, `$HB`) from the GUI. Partial homing remains untrusted until all four application axes have been homed.
- Homing uses a separate five-minute response window because GRBL sends its final `ok` only after the physical homing cycle completes; ordinary serial commands retain the short device timeout.
- Carousel conversion remains `140/27` full motor steps per degree with integer Python targets.
- Z remains 80 steps/mm. A and B use standard 1.8° NEMA17 motors (200 full steps/revolution), 2 mm of actuator travel per motor revolution, and 1/16 DRV8825 microstepping: `200 / 2 × 16 = 1600` driver steps/mm.
- Exactly 200 µL uses P1. The existing 1.1 mL and 10 mL planning boundaries are unchanged.
- Dispense always dumps afterward and never cleans automatically.
- Cleaning returns liquid to the Cleaning Beaker.
- Abort uses feed hold/reset, cancels workflow trust, and requires operator-initiated Home.
- Vessel state persists until confirmed New Session / Reset.

## A&D FZ-500i scale setup

The real scale controller is configured for the FZ-500i's documented A&D standard RS-232C protocol. It continuously issues the safe `Q` command and displays the latest value in grams together with the `STABLE`/`UNSTABLE` condition. Serial reading runs independently of the Qt thread and the same sample stream supplies transfer feedback.

Configure the balance's `5if` serial settings to match:

| Balance setting | Value | Meaning |
|---|---:|---|
| `bp5` | `2` | 2400 baud |
| `btpr` | `0` | 7 data bits, even parity |
| `Crlf` | `0` | CR LF terminator |
| `type` | `0` | A&D standard format |

Select grams as the current weighing unit. The application accepts the A&D `ST` stable and `US` unstable headers and rejects overload/error records rather than treating them as measurements. If the balance has been configured differently, either restore these communication settings or edit `AandFzScaleConfig` in `autosampler/config.py` to match it.

The balance is RS-232C DCE equipment with a female D-sub 9-pin connector. A&D specifies a straight-through connection to a DTE computer/adapter. Confirm that the USB–RS-232 adapter is a true RS-232 voltage-level adapter and that the cable is straight-through, not null-modem. On the Raspberry Pi it will appear as a serial device such as `/dev/ttyUSB0` or a stable `/dev/serial/by-id/...` alias.

## GRBL carousel coordinates

The chosen representation remains one GRBL X coordinate unit per physical full carousel-motor step, requiring the installed GRBL Mega 5X Edge build to be verified/configured with `$100=1`. Python converts calibrated degrees once and sends integer X targets. A recurring reference event synchronizes the active work coordinate with `G10 L20 P1 X0`.

A recurring index pulse must not be wired as an ordinary hard limit that alarms during normal rotation. The exact non-limit firmware/input route remains a hardware integration decision. A future Raspberry Pi GPIO input can be added by implementing `CarouselReferenceSensor`; no Pi-specific dependency is currently required.

## Limit-switch indicators and firmware pinout

The GUI polls GRBL's realtime `?` report and displays X/Z/A/B as **PRESSED** when the corresponding letter appears in its `Pn:` field. GRBL-Mega-5X reports an active axis but does not identify whether that axis's minimum or maximum input caused it.

Physical limit pins are not configured by this Python application. They are compiled into the GRBL-Mega-5X firmware. `grbl/config.h` selects the CPU map (normally `CPU_MAP_2560_RAMPS_BOARD`), and the selected section of `grbl/cpu_map.h` defines each `MIN_LIMIT_PORT_*`, `MIN_LIMIT_BIT_*`, `MAX_LIMIT_PORT_*`, and `MAX_LIMIT_BIT_*` value.

The default GRBL-Mega-5X RAMPS 1.4 mapping is X min D3 / max D2, Z min D18 / max D19, A min D42 / max D40, and B min D44 / max D59. Verify the exact `cpu_map.h` used to compile the firmware installed on this machine before relying on those defaults.

## Calibration unlock (`$X`)

The GUI includes a small **Calibration unlock ($X)** checkbox beside Home. After an explicit warning confirmation, it sends GRBL `$X` and allows only relative `move_axis` jogging so physical constants can be measured before the homing system is ready.

The Manual Calibration tab shows the selected axis's signed commanded step position since its most recent home. A separately homed axis may be jogged for calibration while all automated operations remain locked until every application axis is homed. X is reported in logical full motor steps (the carousel calibration coordinate); Z/A/B are reported in driver steps using their configured steps/mm. This open-loop value cannot detect lost or stalled motor steps.

This bypass does **not** establish coordinates: X/Z/A/B positions remain unknown, absolute/high-level commands remain blocked, and the application continues to show recovery-required state. Home exits the bypass and establishes trusted coordinates. Unchecking it disables bypass jogging; Abort, reset, USB loss, or disconnect also clears it automatically.

Before using `$X`, manually confirm that the requested axis and direction have collision-free travel. `$X` only clears a GRBL alarm; it does not establish machine zero, validate switch state, or replace a physical emergency stop.

## Guided calibration

The **Manual Calibration** tab includes a guided measurement form. Add the intended vessel locations on the Vessels tab first, then work through **Next missing measurement**:

1. Home the relevant axis to establish its reference. Check the physical travel path before each small manual jog. Never seek a mechanical stop automatically.
2. Measure Z maximum travel, collision-free clearance, dispense position, and the vessel-bottom machine offset. Enter signed positions relative to home; maximum travel is a positive distance.
3. Measure A/P1 and B/P2 contact positions, safe upper coordinates, and volume per millimetre. A full rated-volume stroke from contact must fit below the safe upper coordinate. These axes currently use positive strokes from home.
4. Verify speeds and accelerations against the installed firmware and enter transfer/scale settings. Recording these values does not program GRBL settings or verify physical motion limits.
5. Align every vessel separately for P1 and P2 and record its carousel angle from X home in degrees. The existing integer-step conversion remains unchanged.
6. For each entry, check **I physically verified this measurement and its reference / units**, then select **Record confirmed measurement**. Editing the value or selecting another measurement clears confirmation.

Recording a measurement sends no hardware commands. Values are validated and saved atomically into typed configuration; simulation and hardware use separate files. Restarting loads measurements but never restores homing or position trust. Invalid calibration files fail startup rather than partially loading. Session reset does not erase axis calibration.

The status lists missing measurements. All automated commands remain locked until required Z, A, B, transfer, and both pipettes' carousel values are valid for every configured vessel. Adding a vessel requires its two new carousel measurements. The machine API enforces the same lock before automated motion. Home and manual calibration jogging remain available; Abort/disconnection still require recovery and Home even after calibration is complete.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

All tests use mock hardware. CI is configured for Windows and Linux with Qt's offscreen platform. No normal unit test opens a serial port.

## Hardware verification checklist

**Requires hardware verification:**

- Confirm X/Z/A/B motor directions and GRBL axis/settings mapping.
- Confirm every home-switch direction, polarity, pull-up behavior, and failure response.
- Confirm `$100=1` yields one physical carousel full step per X unit.
- Confirm `G10 L20 P1 X0` and the selected work-coordinate policy on the installed firmware.
- Validate the recurring carousel index input without hard-limit alarms.
- Measure collision-free Z safe, dispense, and aspiration positions.
- Calibrate every pipette-specific carousel location and offset.
- Calibrate A/B contact positions, maximum strokes, volume/travel curves, speeds, and accelerations.
- Configure and validate the real scale protocol, baud rate, stability, resolution, disconnect behavior, and density conversion.
- Compare the persistent GUI readout against the FZ-500i display at zero, positive load, negative/tared load, unstable load, and overload.
- Validate draw correction limits, buffer, overdraw return, transfer tolerance, and non-convergence handling with liquid.
- Verify source/destination/waste/cleaning physical alignment and capacity assumptions.
- Test Abort during each type of motion and confirm Home is required afterward.
- Confirm the physical emergency-stop circuit works independently of software.
- Unplug/reconnect GRBL and scale during controlled tests and verify no workflow resumes automatically.

See [Raspberry Pi deployment preparation](docs/RASPBERRY_PI.md) for ARM64 Linux installation, permissions, fullscreen, startup, and remaining target work.

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

The program no longer assumes `COM4`. In hardware mode, select the GRBL and scale roles from discovered devices. On first setup, a sole detected serial port is preselected for GRBL but is never connected automatically; verify its role before pressing Connect. After a device is selected, a saved USB identity is recognized if the OS later assigns a different `COM` or `/dev/tty*` path. Serial-less USB adapters can use their physical USB location to keep two identical adapters in separate roles. Ambiguous devices are never chosen automatically.

For a two-Pi installation, **Device source → Glovebox Pi over USB** discovers
USB serial devices attached to the inner Pi over the authenticated USB gadget
network. GRBL and scale still use the same controllers and require explicit
Connect actions. See [Glovebox Pi USB serial bridge](docs/GLOVEBOX_BRIDGE.md).

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

Discovery captures the OS device path, description, HWID, VID, PID, USB serial number, manufacturer, product, and location where available. Matching prioritizes serial number and USB metadata. For adapters without a serial number, a saved USB location distinguishes otherwise identical adapters; keep them in the same physical USB sockets for automatic reselection. A previous port name alone is insufficient for automatic selection.

If GRBL disappears, the workflow is cancelled, the controller is marked disconnected, all coordinates become untrusted, and recovery/Home is required after reconnection. Scale loss during an operation requests feed hold and blocks further workflow steps without sending an automatic reset; motion stopping is not confirmed by sending hold alone. No scale reading is fabricated. Linux permission failures include a specific diagnostic; the application never changes groups or invokes `sudo`.

## Preserved machine behavior

- Axis mapping remains X carousel, Z carriage, A Pipette 1, B Pipette 2.
- GUI Home all runs `$HZ`, `$HX`, `$HA`, `$HB` in that order. Individual axis homing remains available. Partial homing remains untrusted until all four application axes have been homed.
- X uses a 30 degrees/second initial search (`MachineConfig.carousel_homing_search_deg_s`), equivalent to `9333.33333` X units/min with the current `140/27` conversion. The sender temporarily sets `$25` and, if needed, raises X's `$110` speed cap for `$HX` only. Existing `$24` locating speed, `$27` pull-off distance, acceleration, and other axes' speeds are retained. Acceleration may prevent reaching the requested speed during a short search. Switch detection, including a switch already pressed at startup, stays inside the standard firmware homing cycle; a switch that cannot clear within the configured pull-off distance causes a homing alarm.
- Z similarly uses `$25=1000` for `$HZ` (`MachineConfig.z_homing_search_rate`, in GRBL units/min), restoring the original `$25` afterward, normally `60`. If necessary, its `$112` speed cap is temporarily raised and restored. This applies to individual Z homing and Home all; restoration is verified before starting X. The same persistent settings recovery and pulse-rate preflight checks apply. `$24`, `$27`, and Z homing direction are retained.
- X homing checks `$22=1`, positive X homing direction (X bit of `$23` clear), and sufficient `$130` travel for a complete carousel revolution plus pull-off clearance, using this project's firmware search multiplier of 1.5. It reports incompatible settings without changing them. The sender does not change `$100`; the current hardware's previously confirmed value is `32`.
- Before temporary EEPROM writes, the sender saves the original values to `homing-recovery.json` in the application configuration directory. After successful X homing it restores and reads back the settings before continuing to A/B or trusting X. An alarm, timeout, Abort, failed restoration, or lost connection requires reconnection; further commands are blocked. Reconnection resets/flushes the controller and restores pending settings, including after an application restart. Recovery requires the original serial port; a different port is rejected to avoid applying values to another controller. These are EEPROM writes on each X homing cycle; do not repeatedly home in a tight loop.
- Homing uses a separate five-minute response window because GRBL sends its final `ok` only after the physical homing cycle completes; ordinary serial commands retain the short device timeout.
- Before GUI/API Home, the sender reads `$$` once to check the entire requested
  sequence against the installed Mega 5X firmware's nominal 30,000 steps/s ceiling.
  It checks resolution times the largest search, locate or configured rapid rate
  divided by 60, including the temporary X search rate. Raw terminal homing also
  receives this check. Missing/invalid settings or an excessive rate block homing
  before movement without changing EEPROM, resetting, or erasing prior trust merely
  because validation failed. A reset/disconnection observed during the read still
  invalidates trust. This ceiling is an upper rejection threshold, not a proven safe
  operating speed; mechanics, driver timing and this firmware build may require less.
  For example, 8,000 steps/unit at 500 units/min requests 66,667 steps/s; at 60 it
  requests 8,000 steps/s. Verify steps/unit physically rather than lowering the
  resolution setting just to bypass the check. Idle hold `$1=255` does not guarantee
  holding torque during a firmware fault/reset. No firmware or live settings are
  automatically changed by preflight.
- Both Connections & Safety and Manual Calibration show the active homing axis,
  its place in the Z/X/A/B sequence, and elapsed wait. Home, jog, unlock, manual
  commands, and other machine requests cannot accumulate behind an active GUI
  command. Status polling pauses during that wait; Abort remains available.
- A missing acknowledgement, alarm, or transport failure during **any** axis's
  homing ends the sequence without starting remaining axes. Where the transport
  is usable, the sender requests feed hold **without sending reset or closing
  the connection**, and blocks further movement, homing and unlock commands.
  Read-only status polling remains available on that connection. Feed hold is
  not proof of stopping, especially during homing: the upstream Mega 5X homing
  loop can only be interrupted by abort/reset. The GUI explicitly reports motion
  stop and holding torque as unconfirmed. Delayed acknowledgements are discarded
  while reading status; they never complete the failed sequence or restore trust.
  The GUI reports the failed axis and logs per-axis start/completion events.
- Abort and worker-command failures discard pending commands, including additional
  Home/unlock requests. Previously submitted Home/unlock requests cannot clear an
  Abort that happened after submission. After recovery, explicitly start a new
  Home; reconnection never starts motion automatically. These protections contain
  an unresponsive controller but do not repair firmware or diagnose a loss of
  motor holding torque.
- **Gravity-loaded axes:** secure them before Abort/reset, disconnect/reconnect,
  recovery, or shutdown. A timeout never automatically sends Ctrl-X or reconnects.
  A failed settings restoration retains the connection and the recovery record.
  After securing the axes, deliberately disconnect/reconnect and Home to recover;
  X recovery may explicitly reset the controller before restoring saved settings.
  Repeated Connect calls never reopen an already live port. Explicit Abort retains
  its feed hold/reset behavior and can release torque. Every reset/reconnect
  invalidates all homing and measurement references, with no automatic re-trust.
  Motor holding torque is controlled by firmware/drivers, not GUI trust flags.
  No idle-lock, driver-enable, or firmware settings are automatically changed.
  See the [Mega 5X homing implementation](https://github.com/fra589/grbl-Mega-5X/blob/edge/grbl/limits.c)
  for the homing stop limitation. Independent mechanical support/braking is needed
  to protect an axis if reset, power loss, or a driver fault releases holding torque.
- Carousel conversion remains `140/27` full motor steps per degree with integer Python targets.
- Z remains 80 steps/mm. A and B use standard 1.8° NEMA17 motors (200 full steps/revolution), 2 mm of actuator travel per motor revolution, and 1/16 DRV8825 microstepping: `200 / 2 × 16 = 1600` driver steps/mm.
- Exactly 200 µL uses P1. The existing 1.1 mL and 10 mL planning boundaries are unchanged.
- Dispense always dumps afterward and never cleans automatically.
- Cleaning returns liquid to the Cleaning Beaker.
- Abort uses feed hold/reset, cancels workflow trust and pending commands, and on real hardware requires reconnection followed by operator-initiated Home.
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

**Carousel overshoot:** separate calibration measurements **Carousel A** (primary overshoot) and **Carousel B** (secondary overshoot) are used automatically by `go_to_spot`, including positioning within liquid commands. For destination T and travel direction d, commands are T+d×A → T−d×B → T, with angles rounded independently to motor steps. Each stage requires GRBL `Idle` and confirmation of its reported work position before the next stage. B=0 preserves the existing primary overshoot and direct return; both zero preserve a single move. Existing files load B=0. Intermediate coordinates are not wrapped or clipped at 0/360°, preserving the commanded path and controller limits. Z clearance finishes before carousel motion, and the final return completes before subsequent liquid motion. A zero-length move adds no artificial overshoot. Any interrupted or failed stage prevents remaining stages; uncertain completion invalidates position trust and requires recovery/Home. [Automatic calibration](docs/OVERSHOOT_CALIBRATION.md) evaluates A/B combinations with scale feedback and saves the recommended pair only after explicit application.

For this machine, Z home is 0 and the usable work envelope is `[-maximum travel, 0]`. Maximum travel and vessel dimensions are positive distances; clearance, dispense, and vessel-reference Z positions are signed negative coordinates. The calibration form accepts negative positions directly and converts positive distance entries to negative positions when recorded. Older calibration files without a Z direction marker have their positive Z position distances converted on load, with the upward-height direction set to +1 when converting positive vessel references. Review the displayed coordinates after restarting. Automated Z targets are checked against both ends of the work envelope before being sent to GRBL; this does not replace firmware limits or deliberate manual-jog precautions.

The **Connections & Safety** tab includes a **Manual G-code command** terminal.
Connect GRBL, type a single command, and press **Send** or Enter. It is available
before calibration and homing, independently of the predefined commands. Command
text (including spaces and case) is sent unchanged with only the serial line
terminator appended. The shared operation log displays submitted commands, every received
response line (including `$$` settings), controller errors, and transport failures.
Simulation logs the command but does not execute raw G-code.

Raw commands may move axes or change settings outside the application's tracking,
so sending one invalidates position trust. Use Home before automated operations.
The terminal requires a connection, waits for the current operation to finish,
and stays blocked after Abort until recovery through Home. Abort continues to
send realtime feed hold/reset without waiting for a command response.

The **Manual Calibration** tab includes a guided measurement form. Add the intended vessel locations on the Vessels tab first, then work through **Next missing measurement**:

1. Home the relevant axis to establish its reference. Check the physical travel path before each small manual jog. Never seek a mechanical stop automatically.
2. Measure Z maximum travel, collision-free clearance, dispense position, and the vessel-bottom machine offset. Enter signed positions relative to home; maximum travel is a positive distance.
3. Measure A/P1 and B/P2 signed negative contact coordinates, positive maximum safe distances from home, and volume per driver step (µL/step, including configured microstepping). Calculate the calibration as measured liquid volume divided by working step count. Working strokes use `round(requested µL / µL per step)` whole steps. For this machine, `endpoint = contact − steps / steps_per_mm`; the actuator then returns to 0. For example, contact −14.8 mm and a 5 mm stroke produce −19.8 mm. A maximum safe distance of 34.2 mm defines an emptying endpoint of −34.2 mm, rounded inward to a whole step. All targets, including home returns and repeated emptying strokes, must stay between the negative endpoint and 0. A full rated-volume stroke must fit inside that envelope. Positive contact-distance entries convert to negative coordinates on recording; older files lacking A/B direction metadata convert their contacts on load. Volumes, µL/step, speeds, accelerations and maximum-travel distances remain positive. Legacy µL/mm calibration converts by dividing by configured steps/mm; verify calibration after microstepping or steps/mm changes. Changing units alone does not improve mechanical accuracy.
4. Verify speeds and accelerations against the installed firmware and enter transfer/scale settings. Recording these values does not program GRBL settings or verify physical motion limits.
5. Align V24 separately for P1 and P2 and record its carousel angle from X home in degrees. All physical locations are derived from the selected pipette's V24 reference: `Vnn = V24 − (24 − nn) × 12°`, `B2 = V24 + 29°`, and `B1 = V24 + 55°`, wrapped into 0–360°. The positive-direction gaps close the circle: 23 × 12° + 29° + 26° + 29° = 360°. Derived angles are converted once to integer motor steps. Older individual spot calibrations do not override this layout.
6. For each entry, check **I physically verified this measurement and its reference / units**, then select **Record confirmed measurement**. Editing the value or selecting another measurement clears confirmation.

Recording a measurement sends no hardware commands. Values are validated and saved atomically into typed configuration; simulation and hardware use separate files. Restarting loads measurements but never restores homing or position trust. Invalid calibration files fail startup rather than partially loading. Session reset does not erase axis calibration.

The status lists missing measurements. All automated commands remain locked until required Z, A, B, transfer, and both pipettes' V24 carousel references are valid. Adding a physical rack vessel uses its derived location without additional angle calibration; its vessel row must still be complete before commands can use it. Saved V24 references are retained, while a missing reference requires operator calibration. The machine API enforces the same lock before automated motion. Home and manual calibration jogging remain available; Abort/disconnection still require recovery and Home even after calibration is complete.

### Controller position measurements

The Manual Calibration tab shows **controller-reported position and displacement**
for X/Z/A/B. Select machine coordinates (MPos) or work coordinates (WPos), then
set an individual reference or all references while the controller reports Idle.
Repeat the reference action between calibration moves. References exist only in
the GUI session: they send no G-code and never change offsets, homing, or movement
authorization. They can be used while unhomed under the existing calibration unlock.
The older commanded-distance-from-home readout remains a separate estimate.

The panel reuses the 250 ms limit/status poll on the existing worker. Commands
and homing can block that worker; after two seconds without position feedback,
values are marked stale and reference capture is disabled until fresh feedback
arrives. Disconnects, reconnects, homing (including one axis), resets and alarms
clear references. Work-offset changes clear work references; machine references
survive ordinary jogs and manual motion. Connection/reset events also clear any
cached work-coordinate offset. Historical comparison records remain in the log.

Assumptions: GRBL Mega 5X positional vectors are ordered **X,Y,Z,A,B**, with Y
unused here; three-value reports provide only X/Y/Z, leaving A/B unknown. Named
axis reports are also supported. Work coordinates are derived only when WCO or
both MPos and WPos are available; an omitted WCO can reuse the last reported offset
within the same connection. Configure **$13=0** (metric status reporting) on the
controller; the panel assumes this initially and does not query or change settings.
If an observed settings response or manual setting enables inch reports, the panel
requires `$13=0` before measurements are available. X units are logical carousel
steps under this project's `$100=1` firmware convention; Z/A/B units are mm.
Firmware with a different axis order or reporting convention requires adapting
the parser. Changes made through another controller client, or a reset whose
startup banner is lost, cannot reliably be detected from positions alone.

To compare physical travel, enter the **signed separately measured distance** in
the existing guided measurement input, check its physical-verification box, and
select **Record travel comparison using the measurement above**. This comparison
uses the selected jog axis (X in degrees; Z/A/B in mm) and coordinate selector,
and requires a fresh Idle report and a measurement reference. It records the
reported displacement and physical distance together in the operation log without
changing saved calibration settings. Use Record confirmed measurement separately
to save an appropriate calibration value. GRBL's open-loop reports do **not**
verify physical travel, stalls, backlash, or skipped steps without independent
position feedback. Simulation does not execute raw G-code.

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

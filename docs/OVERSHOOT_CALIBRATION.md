# Automatic carousel overshoot calibration

Use **Manual Calibration → Automatic carousel overshoot calibration** after homing
and configuring the selected vial rows, both pipette carousel references, Z clearance,
and Z travel/speed. Full liquid-transfer
calibration is not required. Connect the scale and GRBL before starting.

The GUI defaults are A=0–5° and B=0–2° (or each saved angle, if larger), 1° increments,
three trials per direction at one vial spot, and a **2 second deadline**.
One spot is sufficient for a complete calibration and recommendation. Additional
comma-separated spot IDs are optional for verification across positions.
Each angle has separate minimum, maximum and increment inputs. The routine tests
every combination of A and B; set B minimum and maximum to 0 to test only the
original behavior. The programmatic `OvershootOptions` also supports both ranges;
its default B range is fixed at 0 for existing callers. Repetitions, spot IDs,
deadline, and stable-reading hold are editable. Each angle range is
sampled at minimum + n × increment up to the maximum; a non-divisible upper endpoint
is not added. Angles are rounded to achievable whole motor steps; duplicate step
candidates and positive sub-step angles are rejected. Results show actual angles.
Review the entire extended path for clearance before starting. Existing controller
limits remain active; the routine does not wrap or shorten extended moves.

The saved manual-calibration measurements are **Carousel A**
(`carousel_overshoot_deg`) and **Carousel B** (`carousel_secondary_overshoot_deg`).
For target T and approach direction d (+1 or −1), positioning commands
**T+d×A → T−d×B → T**, waiting for each stage to complete. For example, T=100°,
A=3°, B=1° gives 103°→99°→100° when approaching positively and 97°→101°→100°
when approaching negatively, subject to the existing whole-step rounding.
B=0 keeps the original two-stage motion without a duplicate final command.
A=0 with B>0 first reaches T, then crosses to T−d×B and returns. An already
reached target adds no artificial overshoot. Neither intermediate target is wrapped.
Older calibration files load B=0.

## Post-position scale check and corrective jostle

Normal `go_to_spot()` calls also use two independent saved manual-calibration
measurements: **Carousel X** (`carousel_stability_timeout_s`, default 2 seconds)
and **Carousel Y** (`carousel_jostle_deg`, default 1 degree). X must be finite and
nonnegative; zero disables this check. Y must be zero or at least one achievable
motor step; zero disables corrective movement while keeping the scale check.
Older calibration files without these values use the defaults on a fresh machine.

After Z clearance and every A/B stage have completed and their targets have been
confirmed, the program waits up to X seconds for the **first fresh ST reading**.
This check reuses the serial-scale detector with zero additional stable-flag hold;
it is independent of transfer settings and the automatic sweep's configurable
hold. Cached readings and queries begun before this check cannot satisfy it.

If valid fresh readings remain unstable for the full timeout, one corrective
motion goes Y degrees opposite the last completed, nonzero carousel segment,
then returns exactly to the original destination. Each jostle leg must complete
and confirm its reported position before continuing. For example, an A-only
positive approach ends with a negative return segment, so its jostle initially
moves positively; with B enabled, the final return is positive and the jostle
initially moves negatively. The original destination is never recalculated.
No scale recheck or second jostle follows. A call with no carousel displacement
has no direction to reverse and skips the jostle. Existing controller motion
limits apply to both legs; intermediate positions are never wrapped or clipped.

An unavailable scale, communication error, or absence of fresh readings skips the
check/jostle. `CAROUSEL_SCALE_UNAVAILABLE` warns in the GUI operation log once per
unavailable period. Reconnecting the scale or receiving fresh valid feedback
resets the warning latch. During standalone positioning, asynchronous scale-loss
notifications also use this fallback, without aborting completed positioning or
leaving an indefinite wait. A scale loss during an already-started jostle does not
prevent its return to the intended destination. Motion faults and emergency Abort
still stop remaining legs and invalidate position trust.

Existing scale-loss fault handling remains active for enclosing liquid operations
and the automatic A/B calibration sweep. The sweep uses its own timed stability
scoring and omits this extra check/jostle, so a rescue movement cannot conceal an
unreliable A/B pair. `CAROUSEL_STABILITY_WAIT`, `CAROUSEL_STABLE`, and
`CAROUSEL_JOSTLE` entries record normal positioning checks and corrective motion.

## Automatic A/B evaluation

Each candidate pair is applied temporarily. At each spot the routine positions at P1,
then repeatedly calls the normal `go_to_spot()` path for P1→P2 and P2→P1. Setup moves
are excluded from scoring. Both Z clearance and the complete carousel motion must
report Idle before sampling, including when overshoot is zero. The serial controller
also verifies the commanded Z-clearance and all three X stage coordinates in
the work coordinate system (using WPos or MPos plus WCO); an early Idle report at
another position does not count as arrival. Z retracts as in
normal positioning; the routine sends no pipette strokes or liquid commands.

Stability reuses the normal serial-scale detector and the A&D **ST** flag.
Calibration has its own **Stable-reading hold** setting, defaulting to **0.5 seconds**
to require sustained stable feedback before starting the next move. Setting it to
zero accepts the first fresh ST reading. A positive hold requires continuous
ST readings for that duration; **US** restarts the hold. The hold must be finite
and nonnegative. If it cannot be completed within the trial deadline, the trial
times out and testing continues. The two-second trial deadline is independent of
`transfer.scale_stabilization_time_s`; calibration neither requires nor changes
that transfer setting. Cached readings and responses to queries begun
before monitoring are excluded using query-start timestamps and sample sequences.
Timing begins only after the final return to T is complete. The monotonic timing
includes polling latency and ends when sustained stability is
observed. No mass-change threshold or requirement to see US first is added.

A deadline with valid unstable readings is a failed trial and testing continues.
Malformed responses, serial query errors, missing fresh communication, controller
errors, disconnections, and cancellation terminate the sweep. Stop requests a fault
hold without controller reset and requires recovery/Home. After Stop or emergency
Abort, use **Recover GRBL connection after Stop / Abort**, acknowledge the reset
notice with gravity-loaded axes secured, then use the existing **Home** control
with all axes selected. Recovery resets/reconnects only; it never moves or restores
position trust. After homing completes, Start is available again. The existing emergency
ABORT remains available with its existing hold/reset behavior. No automatic return
or home move is attempted after a fault. Software Idle and balance ST cannot detect
motor slip or prove that the vial is physically seated; hardware verification is
still needed. The scale must actually sense the settling disturbance at both
positions for this measurement to reflect vial settling.

Candidate pairs are ranked by overall success proportion, then median successful
stabilization time, then standard deviation, then smaller A and B. A recommendation
also requires **at least two successes and ≥80% success in every spot/direction
group**. With three repetitions, every trial must succeed. A candidate cannot pass
on a single fast success or by hiding failures at any additional selected spot. Time statistics
describe successful trials only; timeouts remain in the success-rate denominator.
No eligible candidate produces an explicit “No reliable candidate” result.

Progress shows outbound movement, secondary overshoot, final return and scale waiting separately.
`CAROUSEL_LEG` and `MOTION_CONFIRMED` logs identify target and reported coordinates;
`OVERSHOOT_SCALE_WAIT` records the start of each scale-monitoring phase.
Each trial is written as `OVERSHOOT_TRIAL` to
the normal operation log (spot, direction, actual A and B angles, stabilization time or
`None`, outcome, and deadline), and the final candidate comparison appears below
the controls. `None` means stability was not observed, not a zero-second result.
Both original angles are restored on completion, cancellation, or failure.
Only **Apply and save recommended A/B pair** publishes the recommendation through
the existing atomic calibration settings workflow, writing both values together.
A save failure leaves both previous in-memory settings intact. Mock mode exercises the workflow but does not
model physical settling; it cannot produce a hardware-valid calibration.

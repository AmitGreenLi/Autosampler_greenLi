from copy import deepcopy

import pytest

from autosampler.calibration import load_calibration, save_calibration
from autosampler.config import MachineConfig
from autosampler.errors import ControllerError, OperationAborted, RecoveryRequired, SerialTimeout, ValidationError
from autosampler.models import OperationState, PipetteId
from autosampler.motion.carousel import degrees_to_steps


@pytest.mark.parametrize("start,sign", [(0, 1), (500, -1), (-500, 1)])
def test_overshoot_and_equal_return_finish_at_destination(configured_machine, monkeypatch, start, sign):
    m = configured_machine
    m.config.carousel_overshoot_deg = 5
    m.state.carousel_steps = start
    m.state.positions["X"] = m.grbl.positions["X"] = start
    events = []
    original_move = m.grbl.move_absolute
    def move(axis, target, feed=None):
        events.append((axis, target))
        return original_move(axis, target, feed)
    def wait(cancelled=None, timeout_s=300):
        assert not cancelled()
        events.append(("complete", None))
    monkeypatch.setattr(m.grbl, "move_absolute", move)
    monkeypatch.setattr(m.grbl, "wait_for_motion_complete", wait)
    target = degrees_to_steps(30)
    extra = degrees_to_steps(5)
    assert m.go_to_spot("dest", PipetteId.P1) == target
    assert events == [("Z", 0), ("complete", None), ("X", target + sign * extra),
                      ("complete", None), ("X", target), ("complete", None)]
    assert m.state.carousel_steps == target
    assert m.grbl.positions["X"] == target
    assert m.state.vessel_under_pipette == "dest"


def test_zero_preserves_single_move_without_completion_wait(configured_machine, monkeypatch):
    m = configured_machine
    m.config.carousel_overshoot_deg = 0
    monkeypatch.setattr(m.grbl, "wait_for_motion_complete", lambda **kwargs: pytest.fail("zero must preserve existing behavior"))
    before = len(m.grbl.commands)
    m.go_to_spot("dest", PipetteId.P1)
    assert m.grbl.commands[before:] == ["G90 G0 Z0 F100", "G90 G0 X156"]


def test_destination_already_reached_has_no_artificial_overshoot(configured_machine):
    m = configured_machine
    m.config.carousel_overshoot_deg = 5
    before = len(m.grbl.commands)
    m.go_to_spot("source", PipetteId.P1)
    assert m.grbl.commands[before:] == ["G90 G0 Z0 F100", "G90 G0 X0"]


@pytest.mark.parametrize("failure", ["move", "wait", "abort", "disconnect", "hold"])
def test_no_return_after_first_leg_fails_or_is_interrupted(configured_machine, monkeypatch, failure):
    m = configured_machine
    m.config.carousel_overshoot_deg = 5
    original_move = m.grbl.move_absolute
    x_targets = []
    wait_count = 0
    def move(axis, target, feed=None):
        if axis == "X":
            x_targets.append(target)
            if failure == "move":
                raise ControllerError("controller soft limit rejected extended move")
        return original_move(axis, target, feed)
    def wait(cancelled=None, timeout_s=300):
        nonlocal wait_count
        wait_count += 1
        if wait_count == 2:
            if failure == "wait":
                raise SerialTimeout("motion timeout")
            if failure == "abort":
                m.abort()
            if failure == "disconnect":
                m.notify_grbl_disconnected()
            if failure == "hold":
                raise RecoveryRequired("Hold:0")
    monkeypatch.setattr(m.grbl, "move_absolute", move)
    monkeypatch.setattr(m.grbl, "wait_for_motion_complete", wait)
    with pytest.raises((ControllerError, OperationAborted, RecoveryRequired)):
        m.go_to_spot("dest", PipetteId.P1)
    assert x_targets == [degrees_to_steps(30) + degrees_to_steps(5)]
    assert not m.state.position_trusted
    assert m.state.carousel_steps is None
    assert m.state.operation_state is OperationState.RECOVERY_REQUIRED


def test_overshoot_is_not_wrapped_across_zero(configured_machine):
    m = configured_machine
    m.config.carousel_overshoot_deg = 5
    m.state.carousel_steps = 50
    m.grbl.positions["X"] = 50
    before = len(m.grbl.commands)
    m.go_to_spot("source", PipetteId.P1)
    assert m.grbl.commands[before:] == ["G90 G0 Z0 F100", "G90 G0 X-26", "G90 G0 X0"]


def test_overshoot_calibration_persists_and_legacy_defaults_to_zero(configured_machine, tmp_path):
    import json
    m = configured_machine
    before = list(m.grbl.commands)
    m.record_calibration("carousel_overshoot_deg", 2.5, True)
    assert m.grbl.commands == before
    path = tmp_path / "calibration.json"
    save_calibration(m.config, path, m.inventory.vessels)
    config = MachineConfig()
    load_calibration(config, path, m.inventory.vessels)
    assert config.carousel_overshoot_deg == 2.5
    data = json.loads(path.read_text())
    data["values"].pop("carousel_overshoot_deg")
    path.write_text(json.dumps(data))
    legacy = MachineConfig()
    load_calibration(legacy, path, m.inventory.vessels)
    assert legacy.carousel_overshoot_deg == 0


@pytest.mark.parametrize("angle", [-1, float("nan"), float("inf"), 0.001])
def test_invalid_overshoot_leaves_configuration_unchanged(configured_machine, angle):
    m = configured_machine
    before = deepcopy(m.config)
    with pytest.raises(ValidationError):
        m.record_calibration("carousel_overshoot_deg", angle, True)
    assert m.config == before


def test_transfer_uses_overshoot_automatically_for_every_destination(configured_machine):
    from autosampler.models import ScaleReading

    m = configured_machine
    m.config.carousel_overshoot_deg = 5
    m.scale.queue(ScaleReading(10, True), ScaleReading(9.9, True))
    start = len(m.grbl.commands)
    m.transfer("source", "dest", 100)
    x_moves = [c for c in m.grbl.commands[start:] if c.startswith("G90 G0 X")]
    assert x_moves == ["G90 G0 X0", "G90 G0 X182", "G90 G0 X156", "G90 G0 X337", "G90 G0 X311"]


def test_failed_return_completion_cannot_establish_destination_trust(configured_machine, monkeypatch):
    m = configured_machine
    m.config.carousel_overshoot_deg = 5
    count = 0
    def wait(**kwargs):
        nonlocal count
        count += 1
        if count == 3:
            raise SerialTimeout("return did not finish")
    monkeypatch.setattr(m.grbl, "wait_for_motion_complete", wait)
    with pytest.raises(SerialTimeout):
        m.go_to_spot("dest", PipetteId.P1)
    assert m.state.vessel_under_pipette is None
    assert m.state.carousel_steps is None
    assert not m.state.position_trusted
    assert m.grbl.commands[-1] == "!"


@pytest.mark.parametrize("angle", [10.123, 359.9])
def test_fractional_step_rounding_preserves_exact_original_target(configured_machine, angle):
    m = configured_machine
    m.config.pipette_1_positions_deg["dest"] = angle
    m.config.carousel_overshoot_deg = 2.5
    start = len(m.grbl.commands)
    target = degrees_to_steps(angle)
    extra = degrees_to_steps(2.5)
    m.go_to_spot("dest", PipetteId.P1)
    x_moves = [c for c in m.grbl.commands[start:] if c.startswith("G90 G0 X")]
    assert x_moves == [f"G90 G0 X{target + extra}", f"G90 G0 X{target}"]
    assert m.state.carousel_steps == target


def test_z_must_finish_before_any_carousel_leg(configured_machine, monkeypatch):
    m = configured_machine
    m.config.carousel_overshoot_deg = 5
    def unfinished_z(**kwargs):
        raise SerialTimeout("Z clearance did not finish")
    monkeypatch.setattr(m.grbl, "wait_for_motion_complete", unfinished_z)
    start = len(m.grbl.commands)
    with pytest.raises(SerialTimeout):
        m.go_to_spot("dest", PipetteId.P1)
    assert m.grbl.commands[start:] == ["G90 G0 Z0 F100", "!"]


@pytest.mark.parametrize("start,sign", [(0, 1), (1500, -1)])
@pytest.mark.parametrize("a,b", [(3, 1), (0, 1), (3, 0), (0, 0)])
def test_independent_overshoots_mirror_and_wait_each_stage(configured_machine, monkeypatch, start, sign, a, b):
    m = configured_machine
    m.config.pipette_1_positions_deg["dest"] = 100
    m.config.carousel_overshoot_deg = a
    m.config.carousel_secondary_overshoot_deg = b
    m.state.carousel_steps = start
    m.grbl.positions["X"] = start
    events = []
    original = m.grbl.move_absolute
    def move(axis, target, feed=None):
        events.append((axis, target))
        return original(axis, target, feed)
    def arrived(axis, target, **kwargs):
        assert m.grbl.positions[axis] == target
        events.append(("arrived", axis, target))
    monkeypatch.setattr(m.grbl, "move_absolute", move)
    monkeypatch.setattr(m.grbl, "wait_for_axis_position", arrived)
    m.go_to_spot("dest", PipetteId.P1)
    target = degrees_to_steps(100)
    stages = [target + sign * degrees_to_steps(a), target - sign * degrees_to_steps(b), target]
    stages = [v for i, v in enumerate(stages) if i == 0 or stages[i-1] != v]
    if a or b:
        assert events == [("Z", 0), ("arrived", "Z", 0)] + [event for v in stages for event in (("X", v), ("arrived", "X", v))]
    else:
        assert events == [("Z", 0), ("X", target)]
    assert m.state.carousel_steps == target


@pytest.mark.parametrize("stage", [1, 2, 3])
@pytest.mark.parametrize("failure", ["move", "completion", "abort"])
def test_secondary_sequence_stops_at_any_failed_stage(configured_machine, monkeypatch, stage, failure):
    m = configured_machine
    m.config.carousel_overshoot_deg = 3
    m.config.carousel_secondary_overshoot_deg = 1
    moves = []
    original = m.grbl.move_absolute
    def move(axis, target, feed=None):
        if axis == "X":
            moves.append(target)
            if failure == "move" and len(moves) == stage:
                raise ControllerError("Soft limit")
        return original(axis, target, feed)
    def arrive(axis, target, **kwargs):
        if axis == "X" and len(moves) == stage:
            if failure == "completion":
                raise SerialTimeout("Not at target")
            if failure == "abort":
                m.abort()
                raise OperationAborted("Abort")
    monkeypatch.setattr(m.grbl, "move_absolute", move)
    monkeypatch.setattr(m.grbl, "wait_for_axis_position", arrive)
    with pytest.raises((ControllerError, SerialTimeout, OperationAborted)):
        m.go_to_spot("dest", PipetteId.P1)
    assert len(moves) == stage
    assert not m.state.position_trusted
    assert m.state.carousel_steps is None
    assert m.state.vessel_under_pipette is None


def test_secondary_setting_roundtrips_and_legacy_disables_it(configured_machine, tmp_path):
    import json
    m = configured_machine
    path = tmp_path / "calibration.json"
    m.record_calibration("carousel_overshoot_deg", 3, True)
    m.record_calibration("carousel_secondary_overshoot_deg", 1, True)
    save_calibration(m.config, path, m.inventory.vessels)
    restored = MachineConfig()
    load_calibration(restored, path, m.inventory.vessels)
    assert (restored.carousel_overshoot_deg, restored.carousel_secondary_overshoot_deg) == (3, 1)
    data = json.loads(path.read_text())
    del data["values"]["carousel_secondary_overshoot_deg"]
    path.write_text(json.dumps(data))
    load_calibration(restored, path, m.inventory.vessels)
    assert (restored.carousel_overshoot_deg, restored.carousel_secondary_overshoot_deg) == (3, 0)


@pytest.mark.parametrize("b", [-1, .001, float("nan"), float("inf")])
def test_secondary_setting_rejects_invalid_values(configured_machine, b):
    m = configured_machine
    with pytest.raises(ValidationError):
        m.record_calibration("carousel_secondary_overshoot_deg", b, True)
    assert m.config.carousel_secondary_overshoot_deg == 0

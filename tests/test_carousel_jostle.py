import time

import pytest

from autosampler.calibration import load_calibration, save_calibration
from autosampler.config import MachineConfig
from autosampler.errors import ConnectionFailure, ControllerError, OperationAborted, ScaleError, ScaleUnstable, ValidationError
from autosampler.models import PipetteId, ScaleReading
from autosampler.motion.carousel import degrees_to_steps


@pytest.fixture
def jostle(configured_machine, monkeypatch):
    m = configured_machine
    m.config.carousel_stability_timeout_s = 1.0
    m.config.carousel_jostle_deg = 1.5
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    events = []
    original = m.grbl.move_absolute
    def move(axis, target, feed=None):
        events.append(("move", axis, target, clock[0]))
        return original(axis, target, feed)
    def arrive(axis, target, cancelled=None, **kwargs):
        if cancelled():
            raise OperationAborted("Motion wait cancelled")
        assert m.grbl.positions[axis] == target
        events.append(("arrive", axis, target, clock[0]))
    def fresh(timeout_s, cancelled=None):
        assert events[-1][0] in ("arrive", "sample")
        clock[0] += min(.1, timeout_s)
        events.append(("sample", clock[0]))
        return ScaleReading(1, False)
    monkeypatch.setattr(m.grbl, "move_absolute", move)
    monkeypatch.setattr(m.grbl, "wait_for_axis_position", arrive)
    monkeypatch.setattr(m.scale, "read_fresh", fresh)
    return m, clock, events


@pytest.mark.parametrize("a,b", [(0, 0), (3, 0), (3, 1), (0, 1)])
@pytest.mark.parametrize("start,approach", [(0, 1), (500, -1)])
def test_jostle_opposes_last_segment_and_returns_exactly_once(jostle, a, b, start, approach):
    m, clock, events = jostle
    m.config.carousel_overshoot_deg = a
    m.config.carousel_secondary_overshoot_deg = b
    m.state.carousel_steps = m.grbl.positions["X"] = start
    target = degrees_to_steps(30)
    m.go_to_spot("dest", PipetteId.P1)
    moves = [e for e in events if e[:2] == ("move", "X")]
    last_direction = -approach if a and not b else approach
    assert [e[2] for e in moves[-2:]] == [target-last_direction*degrees_to_steps(1.5), target]
    assert moves[-2][3] >= 1  # no corrective movement before the full timeout
    assert moves[-1][3] == moves[-2][3]
    assert events[-1][:3] == ("arrive", "X", target)
    assert m.state.carousel_steps == m.grbl.positions["X"] == target
    assert m.state.position_trusted
    assert m.state.vessel_under_pipette == "dest"
    assert len(moves) == (3 if b else 2 if a else 1) + 2
    assert m.config.carousel_jostle_deg == 1.5


def test_fresh_stable_reading_finishes_without_jostle(jostle, monkeypatch):
    m, clock, events = jostle
    reads = []
    def fresh(timeout_s, cancelled=None):
        assert events[-1][:3] == ("arrive", "X", degrees_to_steps(30))
        reads.append(1)
        clock[0] += .1
        return ScaleReading(1, True)
    monkeypatch.setattr(m.scale, "read_fresh", fresh)
    m.go_to_spot("dest", PipetteId.P1)
    assert reads == [1]
    assert len([e for e in events if e[:2] == ("move", "X")]) == 1


def test_cached_stable_does_not_satisfy_check(jostle):
    m, _, events = jostle
    m.scale._latest = ScaleReading(1, True)
    m.go_to_spot("dest", PipetteId.P1)
    assert len([e for e in events if e[:2] == ("move", "X")]) == 3


@pytest.mark.parametrize("error", [ConnectionFailure("Disconnected"), ScaleError("Query unavailable"), ScaleUnstable("No sample before deadline")])
def test_unavailable_scale_skips_jostle_and_warns_once(jostle, monkeypatch, caplog, error):
    m, _, events = jostle
    def unavailable(*args, **kwargs):
        raise error
    monkeypatch.setattr(m.scale, "read_fresh", unavailable)
    caplog.set_level("WARNING")
    m.go_to_spot("dest", PipetteId.P1)
    m.go_to_spot("source", PipetteId.P1)
    assert caplog.text.count("CAROUSEL_SCALE_UNAVAILABLE:") == 1
    assert len([e for e in events if e[:2] == ("move", "X")]) == 2
    assert m.state.position_trusted


def test_disconnected_warning_resets_after_reconnection(jostle, caplog):
    m, _, events = jostle
    m.disconnect_scale()
    caplog.set_level("WARNING")
    m.go_to_spot("dest", PipetteId.P1)
    m.go_to_spot("source", PipetteId.P1)
    assert caplog.text.count("CAROUSEL_SCALE_UNAVAILABLE:") == 1
    m.connect_scale()
    m.disconnect_scale()
    m.go_to_spot("dest", PipetteId.P1)
    assert caplog.text.count("CAROUSEL_SCALE_UNAVAILABLE:") == 2
    assert len([e for e in events if e[:2] == ("move", "X")]) == 3


def test_disconnect_notification_during_check_does_not_abort_standalone_positioning(jostle, monkeypatch, caplog):
    m, _, events = jostle
    def disconnected(*args, **kwargs):
        m.notify_scale_disconnected("Device disappeared")
        raise ConnectionFailure("Device disappeared")
    monkeypatch.setattr(m.scale, "read_fresh", disconnected)
    caplog.set_level("WARNING")
    m.go_to_spot("dest", PipetteId.P1)
    assert m.state.position_trusted
    assert not m.state.abort_requested
    assert not m.state.scale_connected
    assert "!" not in m.grbl.commands
    assert len([e for e in events if e[:2] == ("move", "X")]) == 1
    assert caplog.text.count("CAROUSEL_SCALE_UNAVAILABLE:") == 1
    assert not m._optional_positioning_scale


def test_liquid_operation_retains_scale_disconnect_fault(jostle, monkeypatch):
    m, _, _ = jostle
    def disconnected(*args, **kwargs):
        m.notify_scale_disconnected("Device disappeared")
        raise ConnectionFailure("Device disappeared")
    monkeypatch.setattr(m.scale, "read_fresh", disconnected)
    with pytest.raises(OperationAborted):
        with m._operation("transfer"):
            m.go_to_spot("dest", PipetteId.P1)
    assert not m.state.position_trusted
    assert m.grbl.commands[-1] == "!"


@pytest.mark.parametrize("jostle_leg", [1, 2])
@pytest.mark.parametrize("fault", ["move", "wait", "abort"])
def test_failed_jostle_stage_never_sends_remaining_motion(jostle, monkeypatch, jostle_leg, fault):
    m, _, events = jostle
    original_move = m.grbl.move_absolute
    original_arrive = m.grbl.wait_for_axis_position
    moves = []
    def move(axis, target, feed=None):
        if axis == "X":
            moves.append(target)
            if fault == "move" and len(moves) == 1 + jostle_leg:
                raise ControllerError("Limit")
        return original_move(axis, target, feed)
    def arrive(axis, target, **kwargs):
        if axis == "X" and len(moves) == 1 + jostle_leg:
            if fault == "wait":
                raise ControllerError("Motion uncertain")
            if fault == "abort":
                m.abort()
        return original_arrive(axis, target, **kwargs)
    monkeypatch.setattr(m.grbl, "move_absolute", move)
    monkeypatch.setattr(m.grbl, "wait_for_axis_position", arrive)
    with pytest.raises((ControllerError, OperationAborted)):
        m.go_to_spot("dest", PipetteId.P1)
    assert len(moves) == 1 + jostle_leg
    assert not m.state.position_trusted
    assert m.state.carousel_steps is None


def test_abort_during_scale_wait_never_jostles(jostle, monkeypatch):
    m, _, events = jostle
    def aborted(*args, **kwargs):
        m.abort()
        raise OperationAborted("Abort")
    monkeypatch.setattr(m.scale, "read_fresh", aborted)
    with pytest.raises(OperationAborted):
        m.go_to_spot("dest", PipetteId.P1)
    assert len([e for e in events if e[:2] == ("move", "X")]) == 1


def test_no_carousel_displacement_skips_directionless_jostle(jostle):
    m, _, events = jostle
    m.go_to_spot("source", PipetteId.P1)
    assert len([e for e in events if e[:2] == ("move", "X")]) == 1


def test_jostle_does_not_recurse_or_recheck_scale(jostle, monkeypatch):
    m, clock, events = jostle
    calls = []
    original = m.go_to_spot
    def go(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(m, "go_to_spot", go)
    m.go_to_spot("dest", PipetteId.P1)
    assert calls == [1]
    assert clock[0] == pytest.approx(1)
    assert events[-1][0] == "arrive"


def test_zero_jostle_angle_still_checks_but_never_moves_again(jostle):
    m, clock, events = jostle
    m.config.carousel_jostle_deg = 0
    m.go_to_spot("dest", PipetteId.P1)
    assert clock[0] == pytest.approx(1)
    assert len([e for e in events if e[:2] == ("move", "X")]) == 1


def test_scale_can_disconnect_during_jostle_without_losing_the_return(jostle, monkeypatch):
    m, _, events = jostle
    original = m.grbl.wait_for_axis_position
    def arrive(axis, target, **kwargs):
        if axis == "X" and target != degrees_to_steps(30):
            m.notify_scale_disconnected("Disconnected during jostle")
        return original(axis, target, **kwargs)
    monkeypatch.setattr(m.grbl, "wait_for_axis_position", arrive)
    m.go_to_spot("dest", PipetteId.P1)
    assert events[-1][:3] == ("arrive", "X", degrees_to_steps(30))
    assert m.state.position_trusted


def test_ab_sweep_retains_its_own_monitoring_without_jostles(jostle, caplog):
    from autosampler.overshoot_calibration import OvershootOptions
    m, _, events = jostle
    m.config.pipette_2_positions_deg["source"] = 20
    caplog.set_level("INFO")
    result = m.calibrate_carousel_overshoot(OvershootOptions(("source",), maximum_deg=0))
    assert all(t.outcome == "timeout" for t in result.trials)
    assert "CAROUSEL_JOSTLE phase=" not in caplog.text
    assert "CAROUSEL_STABILITY_WAIT" not in caplog.text
    assert len([e for e in events if e[:2] == ("move", "X")]) == 7


def test_successful_fresh_read_resets_unavailable_warning(jostle, monkeypatch, caplog):
    m, clock, _ = jostle
    def unavailable(*args, **kwargs):
        raise ScaleError("Unavailable")
    monkeypatch.setattr(m.scale, "read_fresh", unavailable)
    caplog.set_level("WARNING")
    m.go_to_spot("dest", PipetteId.P1)
    assert caplog.text.count("CAROUSEL_SCALE_UNAVAILABLE:") == 1
    def stable(timeout_s, cancelled=None):
        clock[0] += .1
        return ScaleReading(1, True)
    monkeypatch.setattr(m.scale, "read_fresh", stable)
    m.go_to_spot("source", PipetteId.P1)
    monkeypatch.setattr(m.scale, "read_fresh", unavailable)
    m.go_to_spot("dest", PipetteId.P1)
    assert caplog.text.count("CAROUSEL_SCALE_UNAVAILABLE:") == 2


def test_jostle_settings_roundtrip(configured_machine, tmp_path):
    m = configured_machine
    path = tmp_path / "calibration.json"
    m.record_calibration("carousel_stability_timeout_s", 3, True)
    m.record_calibration("carousel_jostle_deg", 2, True)
    save_calibration(m.config, path, m.inventory.vessels)
    restored = MachineConfig()
    load_calibration(restored, path, m.inventory.vessels)
    assert (restored.carousel_stability_timeout_s, restored.carousel_jostle_deg) == (3, 2)


@pytest.mark.parametrize("key,value", [("carousel_stability_timeout_s", -1), ("carousel_stability_timeout_s", float("nan")),
                                      ("carousel_jostle_deg", -1), ("carousel_jostle_deg", .001), ("carousel_jostle_deg", float("inf"))])
def test_invalid_jostle_settings_rejected(configured_machine, key, value):
    m = configured_machine
    before = getattr(m.config, key)
    with pytest.raises(ValidationError):
        m.record_calibration(key, value, True)
    assert getattr(m.config, key) == before

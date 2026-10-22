import threading
import time

import pytest

from autosampler.errors import ConnectionFailure, ControllerError, OperationAborted, RecoveryRequired, ScaleError, ValidationError
from autosampler.models import ScaleReading
from autosampler.overshoot_calibration import OvershootOptions, OvershootTrial, rank_trials


@pytest.fixture
def sweep(configured_machine, monkeypatch):
    m = configured_machine
    m.config.pipette_2_positions_deg.update(source=20, dest=50)
    m.config.carousel_overshoot_deg = 2.5
    # Deterministic time and acquisition: a new sample costs 100 ms.
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    def fresh(timeout_s, cancelled=None):
        clock[0] += min(0.1, timeout_s)
        return ScaleReading(1, m.config.carousel_overshoot_deg > 0)
    monkeypatch.setattr(m.scale, "read_fresh", fresh)
    return m, OvershootOptions(("source", "dest"), maximum_deg=1, repetitions=3)


def test_full_sweep_times_out_continues_restores_and_saves_only_explicitly(sweep, tmp_path, caplog):
    m, options = sweep
    m.calibration_file = tmp_path / "calibration.json"
    caplog.set_level("INFO")
    before = len(m.grbl.commands)
    result = m.calibrate_carousel_overshoot(options)
    assert len(result.trials) == 24
    assert result.recommendation_deg == options.candidates()[1]
    assert sum(t.outcome == "timeout" for t in result.trials) == 12
    assert sum(t.outcome == "success" for t in result.trials) == 12
    assert m.config.carousel_overshoot_deg == 2.5
    assert not m.calibration_file.exists()
    assert all(" A" not in c and " B" not in c for c in m.grbl.commands[before:])
    assert caplog.text.count("OVERSHOOT_TRIAL spot=") == 24
    m.apply_overshoot_recommendation()
    assert m.config.carousel_overshoot_deg == options.candidates()[1]
    assert m.calibration_file.exists()


@pytest.mark.parametrize("transfer_duration", [None, 0, 2, 5])
def test_default_deadline_independent_of_transfer_duration(sweep, transfer_duration):
    m, options = sweep
    m.config.transfer.scale_stabilization_time_s = transfer_duration
    result = m.calibrate_carousel_overshoot(options)
    assert result.recommendation_deg == options.candidates()[1]
    assert len(result.trials) == 24
    assert all(t.stabilization_s < 2 for t in result.trials if t.outcome == "success")
    assert m.config.transfer.scale_stabilization_time_s == transfer_duration
    assert m.config.carousel_overshoot_deg == 2.5


@pytest.mark.parametrize("hold", [2, 3])
def test_hold_exceeding_available_time_is_timeout_and_sweep_continues(sweep, hold):
    m, options = sweep
    result = m.calibrate_carousel_overshoot(OvershootOptions(
        options.spots, minimum_deg=1, maximum_deg=1, stability_hold_s=hold))
    assert len(result.trials) == 12
    assert all(t.outcome == "timeout" for t in result.trials)
    assert result.recommendation_deg is None
    assert m.config.carousel_overshoot_deg == 2.5


def test_calibration_honors_its_explicit_stability_hold(sweep):
    m, options = sweep
    m.config.transfer.scale_stabilization_time_s = 5
    result = m.calibrate_carousel_overshoot(OvershootOptions(
        options.spots, minimum_deg=1, maximum_deg=1, stability_hold_s=.25))
    assert all(t.outcome == "success" and .35 <= t.stabilization_s < 2 for t in result.trials)
    assert m.config.transfer.scale_stabilization_time_s == 5


def test_pair_sweep_measures_only_after_final_return_and_saves_both(sweep, monkeypatch, tmp_path):
    from autosampler.calibration import load_calibration
    from autosampler.config import MachineConfig
    from autosampler.motion.carousel import degrees_to_steps
    m, _ = sweep
    m.config.carousel_secondary_overshoot_deg = 1.5
    options = OvershootOptions(("source",), maximum_deg=1, secondary_maximum_deg=1)
    confirmed = []
    monkeypatch.setattr(m.grbl, "wait_for_axis_position", lambda axis, target, **kw: confirmed.append((axis, target)))
    original = m.scale.read_fresh
    def fresh(timeout_s, cancelled=None):
        assert confirmed[-1] == ("X", m.state.carousel_steps)
        if m.config.carousel_overshoot_deg and m.config.carousel_secondary_overshoot_deg:
            target = m.state.carousel_steps
            direction = 1 if m.state.active_pipette.value == "P2" else -1
            x_targets = [value for axis, value in confirmed if axis == "X"]
            assert x_targets[-3:] == [target + direction * degrees_to_steps(m.config.carousel_overshoot_deg),
                                      target - direction * degrees_to_steps(m.config.carousel_secondary_overshoot_deg), target]
        reading = original(timeout_s, cancelled)
        return ScaleReading(reading.mass_g, reading.stable and m.config.carousel_secondary_overshoot_deg > 0)
    monkeypatch.setattr(m.scale, "read_fresh", fresh)
    m.calibration_file = tmp_path / "calibration.json"
    result = m.calibrate_carousel_overshoot(options)
    assert len(result.trials) == 24
    assert len(result.candidates) == 4
    assert {(t.angle_deg, t.secondary_deg) for t in result.trials} == set(options.candidate_pairs())
    assert result.recommendation_pair == options.candidate_pairs()[-1]
    assert (m.config.carousel_overshoot_deg, m.config.carousel_secondary_overshoot_deg) == (2.5, 1.5)
    assert not m.calibration_file.exists()
    def fail(*args):
        raise OSError("Disk unavailable")
    with monkeypatch.context() as patch:
        patch.setattr("autosampler.machine.save_calibration", fail)
        with pytest.raises(OSError):
            m.apply_overshoot_recommendation()
        assert (m.config.carousel_overshoot_deg, m.config.carousel_secondary_overshoot_deg) == (2.5, 1.5)
        assert not m.calibration_file.exists()
    assert m.apply_overshoot_recommendation() == result.recommendation_pair
    restored = MachineConfig()
    load_calibration(restored, m.calibration_file, m.inventory.vessels)
    assert (restored.carousel_overshoot_deg, restored.carousel_secondary_overshoot_deg) == result.recommendation_pair


@pytest.mark.parametrize("failure", ["abort", "scale", "motion"])
def test_pair_sweep_restores_both_on_failure(sweep, monkeypatch, failure):
    m, _ = sweep
    m.config.carousel_secondary_overshoot_deg = 1.5
    def fresh(*args):
        assert m.config.carousel_secondary_overshoot_deg != 1.5
        if failure == "abort":
            m.abort()
            raise OperationAborted("Abort")
        raise ScaleError("Scale disconnected")
    monkeypatch.setattr(m.scale, "read_fresh", fresh)
    if failure == "motion":
        def failed_arrival(*args, **kwargs):
            raise ControllerError("Stage failed")
        monkeypatch.setattr(m.grbl, "wait_for_axis_position", failed_arrival)
    with pytest.raises((ScaleError, OperationAborted, ControllerError)):
        m.calibrate_carousel_overshoot(OvershootOptions(("source",), minimum_deg=1, maximum_deg=1,
                                      secondary_minimum_deg=1, secondary_maximum_deg=1))
    assert (m.config.carousel_overshoot_deg, m.config.carousel_secondary_overshoot_deg) == (2.5, 1.5)
    assert m.overshoot_result is None


@pytest.mark.parametrize("kwargs", [dict(secondary_minimum_deg=-1), dict(secondary_maximum_deg=float("nan")),
                                    dict(secondary_increment_deg=.001, secondary_maximum_deg=1),
                                    dict(secondary_minimum_deg=.001, secondary_maximum_deg=.001),
                                    dict(secondary_maximum_deg=1000)])
def test_invalid_secondary_sweep_rejected_before_motion(sweep, kwargs):
    m, _ = sweep
    before = list(m.grbl.commands)
    with pytest.raises(ValidationError):
        m.calibrate_carousel_overshoot(OvershootOptions(("source",), **kwargs))
    assert m.grbl.commands == before


def test_requires_repeated_success_in_every_spot_and_direction():
    options = OvershootOptions(("a", "b"), maximum_deg=0)
    trials = [OvershootTrial(s, d, 0, 0.1 if i == 0 else None, "success" if i == 0 else "timeout")
              for s in options.spots for d in ("P1→P2", "P2→P1") for i in range(3)]
    assert rank_trials(options, trials).recommendation_deg is None
    # Many successes elsewhere cannot conceal a failed verification position.
    trials = [OvershootTrial(t.spot, t.direction, 0, .1, "success" if t.spot == "a" else "timeout") for t in trials]
    assert rank_trials(options, trials).recommendation_deg is None


def test_single_spot_sweep_can_recommend_and_save(sweep, tmp_path):
    m, _ = sweep
    options = OvershootOptions(("source",), maximum_deg=1)
    m.calibration_file = tmp_path / "calibration.json"
    result = m.calibrate_carousel_overshoot(options)
    assert len(result.trials) == 12
    assert {t.spot for t in result.trials} == {"source"}
    assert {t.direction for t in result.trials} == {"P1→P2", "P2→P1"}
    assert result.recommendation_deg == options.candidates()[1]
    assert m.config.carousel_overshoot_deg == 2.5
    assert not m.calibration_file.exists()
    m.apply_overshoot_recommendation()
    assert m.config.carousel_overshoot_deg == options.candidates()[1]
    assert m.calibration_file.exists()


def test_single_spot_still_requires_reliability_in_both_directions():
    options = OvershootOptions(("source",), maximum_deg=0)
    trials = [OvershootTrial("source", d, 0, .1, "success" if d == "P1→P2" else "timeout")
              for d in ("P1→P2", "P2→P1") for _ in range(3)]
    assert rank_trials(options, trials).recommendation_deg is None


@pytest.mark.parametrize("stop_method", ["stop_overshoot_calibration", "abort"])
def test_recovery_and_home_allow_new_calibration(sweep, stop_method):
    m, options = sweep
    getattr(m, stop_method)()
    assert not m.state.position_trusted
    with pytest.raises(RecoveryRequired):
        m.calibrate_carousel_overshoot(options)
    before = len(m.grbl.commands)
    m.recover_grbl_connection()
    assert m.grbl.commands[before:] == ["!", "CTRL-X", "DISCONNECT", "CONNECT"]
    assert not m.state.position_trusted
    m.home()
    result = m.calibrate_carousel_overshoot(options)
    assert result.recommendation_deg is not None
    assert m.config.carousel_overshoot_deg == 2.5


def test_no_new_motion_until_return_confirmed_and_scale_hold_satisfied(sweep, monkeypatch):
    m, options = sweep
    moves = []
    confirmed = []
    original_move = m.grbl.move_absolute
    def move(axis, target, feed=None):
        moves.append((axis, target))
        return original_move(axis, target, feed)
    def arrive(axis, target, **kwargs):
        assert m.grbl.positions[axis] == target
        confirmed.append((axis, target))
    monkeypatch.setattr(m.grbl, "move_absolute", move)
    monkeypatch.setattr(m.grbl, "wait_for_axis_position", arrive)
    original_fresh = m.scale.read_fresh
    sample_group = [None, None]
    def fresh(timeout_s, cancelled=None):
        assert confirmed[-1] == ("X", m.state.carousel_steps)
        if len(confirmed) != sample_group[0]:
            sample_group[:] = [len(confirmed), len(moves)]
        assert len(moves) == sample_group[1]
        return original_fresh(timeout_s, cancelled)
    monkeypatch.setattr(m.scale, "read_fresh", fresh)
    result = m.calibrate_carousel_overshoot(OvershootOptions(("source",), minimum_deg=1, maximum_deg=1))
    assert all(t.stabilization_s >= .5 and t.outcome == "success" for t in result.trials)
    # Each trial must send an extended X target and then the exact final target.
    x_moves = [p for axis, p in moves if axis == "X"]
    assert len(x_moves) == 13  # setup already at P1, then two X legs per trial


def test_rank_uses_success_rate_then_time_and_consistency():
    options = OvershootOptions(("a", "b"), maximum_deg=2, repetitions=5)
    a0, a1, a2 = options.candidates()
    trials = []
    for s in options.spots:
        for d in ("P1→P2", "P2→P1"):
            for i in range(5):
                trials.extend((OvershootTrial(s, d, a0, .01, "success" if i < 4 else "timeout"),
                               OvershootTrial(s, d, a1, .2, "success"),
                               OvershootTrial(s, d, a2, .1 if i == 0 else .2 if i < 4 else .3, "success")))
    result = rank_trials(options, trials)
    assert result.recommendation_deg == a1
    assert [c.angle_deg for c in result.candidates] == [a1, a2, a0]


@pytest.mark.parametrize("failure", ["motion", "scale", "disconnect", "cancel", "abort"])
def test_faults_stop_without_more_moves_and_restore(sweep, monkeypatch, failure):
    m, options = sweep
    count = []
    original = m.grbl.move_absolute
    def move(axis, target, feed=None):
        if axis == "X":
            count.append(target)
            if failure == "motion" and len(count) == 2:
                raise ControllerError("Soft limit")
        return original(axis, target, feed)
    monkeypatch.setattr(m.grbl, "move_absolute", move)
    def fresh(timeout_s, cancelled=None):
        if failure == "disconnect":
            m.scale.disconnect()
            raise ConnectionFailure("Disconnected")
        if failure == "cancel":
            m.stop_overshoot_calibration()
            raise OperationAborted("Stop")
        if failure == "abort":
            m.abort()
            raise OperationAborted("Abort")
        raise ScaleError("No response")
    monkeypatch.setattr(m.scale, "read_fresh", fresh)
    with pytest.raises((ControllerError, ConnectionFailure, OperationAborted, ScaleError)):
        m.calibrate_carousel_overshoot(options)
    assert len(count) == 2
    assert m.config.carousel_overshoot_deg == 2.5
    assert m.overshoot_result is None
    assert not m.state.position_trusted


def test_zero_candidate_finishes_clearance_and_motion_before_sampling(sweep, monkeypatch):
    m, options = sweep
    events = []
    original = m.grbl.move_absolute
    def move(axis, target, feed=None):
        events.append(axis)
        return original(axis, target, feed)
    monkeypatch.setattr(m.grbl, "move_absolute", move)
    monkeypatch.setattr(m.grbl, "wait_for_motion_complete", lambda **kw: events.append("idle"))
    def fresh(*args):
        assert events[-1] == "idle"
        events.append("sample")
        return ScaleReading(1, True)
    monkeypatch.setattr(m.scale, "read_fresh", fresh)
    m.calibrate_carousel_overshoot(OvershootOptions(options.spots, maximum_deg=0, stability_hold_s=0))
    assert events[:3] == ["Z", "idle", "X"]


def test_stable_flag_must_be_continuous_and_cache_does_not_count(sweep, monkeypatch):
    m, options = sweep
    m.scale._latest = ScaleReading(1, True)
    m.config.transfer.scale_stabilization_time_s = .15
    readings = iter([True, False, True, True, True])
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    def fresh(*args):
        clock[0] += .1
        return ScaleReading(1, next(readings))
    monkeypatch.setattr(m.scale, "read_fresh", fresh)
    assert m.scale.measure_stabilization(.15, 2) == pytest.approx(.5)


@pytest.mark.parametrize("kwargs", [dict(repetitions=1), dict(increment_deg=.001), dict(deadline_s=0),
                                    dict(minimum_deg=-1), dict(maximum_deg=float("nan")), dict(spots=()),
                                    dict(spots=("source", "source")), dict(spots=(" ",)),
                                    dict(stability_hold_s=-1), dict(stability_hold_s=float("nan"))])
def test_invalid_options_rejected_without_motion(sweep, kwargs):
    m, _ = sweep
    values = dict(spots=("source", "dest"))
    values.update(kwargs)
    before = list(m.grbl.commands)
    with pytest.raises(ValidationError):
        m.calibrate_carousel_overshoot(OvershootOptions(**values))
    assert m.grbl.commands == before
    assert m.config.carousel_overshoot_deg == 2.5


def test_serial_barrier_rejects_cached_and_inflight_samples(monkeypatch):
    from autosampler.controllers.scale import SerialScaleController
    from autosampler.config import AandFzScaleConfig
    controller = SerialScaleController(AandFzScaleConfig("COM7", 2400))
    monkeypatch.setattr(SerialScaleController, "connected", property(lambda self: True))
    controller._latest = ScaleReading(99, True)
    controller._query_started = time.monotonic() - 10
    entered = threading.Event()
    original_wait = controller._condition.wait
    def wait(timeout):
        entered.set()
        return original_wait(timeout)
    monkeypatch.setattr(controller._condition, "wait", wait)
    result = []
    thread = threading.Thread(target=lambda: result.append(controller.read_fresh(1)))
    thread.start()
    assert entered.wait(.5)
    with controller._condition:
        # A response to a pre-motion query is still not fresh.
        controller._latest = ScaleReading(88, True)
        controller._sequence += 1
        controller._condition.notify_all()
    assert result == []
    with controller._condition:
        controller._latest = ScaleReading(1, True)
        controller._sequence += 1
        controller._query_started = time.monotonic()
        controller._condition.notify_all()
    thread.join(1)
    assert len(result) == 1
    assert result[0].mass_g == 1 and result[0].stable


def test_no_reliable_result_restores_and_cannot_be_applied(sweep):
    m, options = sweep
    result = m.calibrate_carousel_overshoot(OvershootOptions(options.spots, maximum_deg=0))
    assert result.recommendation_deg is None
    assert "No reliable candidate" in m.overshoot_progress
    assert m.config.carousel_overshoot_deg == 2.5
    with pytest.raises(ValidationError, match="No reliable"):
        m.apply_overshoot_recommendation()


def test_scale_reader_fault_during_motion_prevents_return(sweep, monkeypatch):
    m, options = sweep
    fault = [False]
    def health():
        if fault[0]:
            raise ScaleError("Query response lost")
    monkeypatch.setattr(m.scale, "check_communication", health)
    original = m.grbl.wait_for_motion_complete
    waits = []
    def wait(cancelled=None, **kwargs):
        waits.append(1)
        if len(waits) == 2:
            fault[0] = True
        return original(cancelled=cancelled, **kwargs)
    monkeypatch.setattr(m.grbl, "wait_for_motion_complete", wait)
    m.state.carousel_steps = 500
    before = len(m.grbl.commands)
    with pytest.raises(ScaleError):
        m.calibrate_carousel_overshoot(OvershootOptions(options.spots, minimum_deg=1, maximum_deg=1))
    assert len([c for c in m.grbl.commands[before:] if c.startswith("G90 G0 X")]) == 1
    assert not m.state.position_trusted
    assert m.config.carousel_overshoot_deg == 2.5


def test_healthy_serial_poll_gap_is_unsuccessful_trial_not_communication_fault(monkeypatch):
    from autosampler.controllers.scale import SerialScaleController
    from autosampler.config import AandFzScaleConfig
    from autosampler.errors import ScaleUnstable
    controller = SerialScaleController(AandFzScaleConfig("COM7", 2400))
    monkeypatch.setattr(SerialScaleController, "connected", property(lambda self: True))
    controller._latest = ScaleReading(1, False)
    controller._query_started = time.monotonic()
    with pytest.raises(ScaleUnstable):
        controller.read_fresh(.001)
    controller._query_started = time.monotonic() - 100
    with pytest.raises(ScaleError):
        controller.read_fresh(.001)
    controller._last_error = ScaleError("Serial query timed out")
    with pytest.raises(ScaleError, match="Serial query timed out"):
        controller.read_fresh(2)


def test_scale_loss_during_outbound_leg_never_queues_return(sweep, monkeypatch):
    m, options = sweep
    original = m.grbl.wait_for_motion_complete
    waits = []
    def wait(cancelled=None, **kwargs):
        waits.append(1)
        # Nonzero candidate setup: Z completes, outbound completion loses scale.
        if len(waits) == 2:
            m.scale.disconnect()
        return original(cancelled=cancelled, **kwargs)
    monkeypatch.setattr(m.grbl, "wait_for_motion_complete", wait)
    m.state.carousel_steps = 500
    before = len(m.grbl.commands)
    with pytest.raises((OperationAborted, ConnectionFailure)):
        m.calibrate_carousel_overshoot(OvershootOptions(options.spots, minimum_deg=1, maximum_deg=1))
    assert len([c for c in m.grbl.commands[before:] if c.startswith("G90 G0 X")]) == 1
    assert m.config.carousel_overshoot_deg == 2.5


def test_save_failure_keeps_previous_angle_and_file(sweep, monkeypatch, tmp_path):
    m, options = sweep
    m.calibrate_carousel_overshoot(options)
    m.calibration_file = tmp_path / "calibration.json"
    m.calibration_file.write_text("original")
    def fail(*args):
        raise OSError("Disk unavailable")
    monkeypatch.setattr("autosampler.machine.save_calibration", fail)
    with pytest.raises(OSError):
        m.apply_overshoot_recommendation()
    assert m.config.carousel_overshoot_deg == 2.5
    assert m.calibration_file.read_text() == "original"


def test_gui_sweep_results_and_explicit_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from autosampler.app_paths import ApplicationPaths
    from autosampler.gui import AutosamplerWindow
    from autosampler.main import build_runtime
    app = QApplication.instance() or QApplication([])
    m, paths, preferences = build_runtime(True, ApplicationPaths.under(tmp_path))
    w = AutosamplerWindow(m, paths, preferences, mock_mode=True)
    def show_error(title, error):
        raise AssertionError(f"{title}: {error}")
    monkeypatch.setattr(w, "show_error", show_error)
    w.limit_timer.stop()
    try:
        m.connect_grbl()
        m.connect_scale()
        m.home()
        w._render_state(m.state)
        # Exercise the worker and queued Qt completion with a deterministic result.
        options = OvershootOptions(("V01", "V13"), maximum_deg=0)
        trials = [OvershootTrial(s, d, 0, .1, "success")
                  for s in options.spots for d in ("P1→P2", "P2→P1") for _ in range(3)]
        result = rank_trials(options, trials)
        entered, release = threading.Event(), threading.Event()
        def run(options):
            assert options.deadline_s == 2
            assert options.stability_hold_s == .25
            assert options.spots == ("V01",)
            assert options.secondary_minimum_deg == .5
            assert options.secondary_maximum_deg == 1.5
            assert options.secondary_increment_deg == .5
            entered.set()
            assert release.wait(2)
            m.overshoot_result = result
            return result
        monkeypatch.setattr(m, "calibrate_carousel_overshoot", run)
        assert w.overshoot_spots.text() == "V01"
        assert w.overshoot_stability_hold.text() == "0.5"
        w.overshoot_stability_hold.setText("0.25")
        w.overshoot_secondary_min.setText("0.5")
        w.overshoot_secondary_max.setText("1.5")
        w.overshoot_secondary_step.setText("0.5")
        w.overshoot_start.click()
        assert entered.wait(1)
        assert not w.overshoot_start.isEnabled()
        assert w.overshoot_stop.isEnabled()
        assert not w.overshoot_apply.isEnabled()
        release.set()
        deadline = time.monotonic() + 2
        while w._command_inflight and time.monotonic() < deadline:
            QTest.qWait(10)
            w.bridge.drain()
        w._render_state(m.state)
        assert "Recommended: A=0°, B=0°" in w.overshoot_comparison.toPlainText()
        assert w.overshoot_apply.isEnabled()
        before = m.config.carousel_overshoot_deg
        assert before == 0
        calls = []
        monkeypatch.setattr(m, "apply_overshoot_recommendation", lambda: calls.append((0, 0)) or (0, 0))
        w.overshoot_apply.click()
        deadline = time.monotonic() + 2
        while w._command_inflight and time.monotonic() < deadline:
            QTest.qWait(10)
            w.bridge.drain()
        assert calls == [(0, 0)]
        assert "Applied and saved A=0°, B=0°" in w.overshoot_comparison.toPlainText()
        # A stopped run has a visible, usable recovery path and stays locked
        # until deliberate recovery and homing have both completed.
        m.stop_overshoot_calibration()
        w._render_state(m.state)
        assert not w.overshoot_start.isEnabled()
        assert w.overshoot_recover.isEnabled()
        assert "Home all axes" in w.overshoot_status.text()
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
        w.overshoot_recover.click()
        deadline = time.monotonic() + 2
        while w._command_inflight and time.monotonic() < deadline:
            QTest.qWait(10)
            w.bridge.drain()
        w._render_state(m.state)
        assert not w.overshoot_start.isEnabled()
        assert w.home_button.isEnabled()
        m.home()
        w._render_state(m.state)
        assert w.overshoot_start.isEnabled()
    finally:
        release.set()
        w.close()
        app.processEvents()

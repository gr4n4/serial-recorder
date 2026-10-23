import numpy as np
import pytest

from client.risk import RiskAccumulator


def make_clock(start=0.0):
    state = {"t": start}

    def clock():
        return state["t"]

    def advance(dt):
        state["t"] += dt

    return clock, advance


def test_accumulates_over_elapsed_wall_clock_time():
    clock, advance = make_clock()
    acc = RiskAccumulator(n_cells=4, clock=clock)
    pressure = np.array([100, 0, 0, 0], dtype=np.uint8)

    # First update establishes the tick baseline; dt=0 so nothing accumulates yet.
    fired, mask, cleared = acc.update(pressure, calibration_factor=0.5, critical_pressure=32,
                                       critical_time=10)
    assert fired.size == 0
    assert mask.tolist() == [True, False, False, False]
    assert acc.accumulated_risk[0] == 0.0

    advance(5 * 60.0)  # 5 minutes
    fired, mask, cleared = acc.update(pressure, calibration_factor=0.5, critical_pressure=32,
                                       critical_time=10)
    assert acc.accumulated_risk[0] == pytest.approx(5.0)
    assert fired.size == 0  # not yet at critical_time=10


def test_non_risky_cell_resets_to_zero():
    clock, advance = make_clock()
    acc = RiskAccumulator(n_cells=1, clock=clock)
    high = np.array([100], dtype=np.uint8)
    low = np.array([0], dtype=np.uint8)

    acc.update(high, calibration_factor=0.5, critical_pressure=32, critical_time=10)
    advance(3 * 60.0)
    acc.update(high, calibration_factor=0.5, critical_pressure=32, critical_time=10)
    assert acc.accumulated_risk[0] == pytest.approx(3.0)

    advance(60.0)
    fired, mask, cleared = acc.update(low, calibration_factor=0.5, critical_pressure=32, critical_time=10)
    assert mask[0] == False
    assert acc.accumulated_risk[0] == 0.0


def test_fires_when_critical_time_reached_and_respects_cooldown():
    clock, advance = make_clock()
    acc = RiskAccumulator(n_cells=1, alert_cooldown_s=300.0, clock=clock)
    high = np.array([100], dtype=np.uint8)

    acc.update(high, calibration_factor=0.5, critical_pressure=32, critical_time=10)
    advance(10 * 60.0)
    fired, _, _ = acc.update(high, calibration_factor=0.5, critical_pressure=32, critical_time=10)
    assert fired.tolist() == [0]

    # Still risky, but within cooldown -> must not re-fire immediately.
    advance(60.0)
    fired, _, _ = acc.update(high, calibration_factor=0.5, critical_pressure=32, critical_time=10)
    assert fired.size == 0

    # Past cooldown -> fires again.
    advance(300.0)
    fired, _, _ = acc.update(high, calibration_factor=0.5, critical_pressure=32, critical_time=10)
    assert fired.tolist() == [0]


def test_config_change_mid_stream_does_not_reset_accumulated_risk():
    clock, advance = make_clock()
    acc = RiskAccumulator(n_cells=1, clock=clock)
    pressure = np.array([50], dtype=np.uint8)  # > 32/0.5=64? no -> use tighter values below

    # threshold = critical_pressure / calibration_factor = 32/0.5 = 64; 50 is not risky yet.
    acc.update(pressure, calibration_factor=0.5, critical_pressure=32, critical_time=10)
    advance(60.0)
    acc.update(pressure, calibration_factor=0.5, critical_pressure=32, critical_time=10)
    assert acc.accumulated_risk[0] == 0.0  # never crossed threshold

    # Server lowers calibration_factor so the same pressure now IS risky
    # (threshold = 32/0.8 = 40 < 50). accumulated_risk must start accruing
    # fresh from its current value (0), not be forcibly reset by the change.
    advance(60.0)
    fired, mask, cleared = acc.update(pressure, calibration_factor=0.8, critical_pressure=32,
                                       critical_time=10)
    assert mask[0] == True
    assert acc.accumulated_risk[0] == pytest.approx(1.0)  # 60s = 1 min accrued this tick

    advance(9 * 60.0)
    fired, _, _ = acc.update(pressure, calibration_factor=0.8, critical_pressure=32,
                              critical_time=10)
    assert fired.tolist() == [0]


def test_cleared_idx_reported_once_fired_cell_drops_below_threshold():
    clock, advance = make_clock()
    acc = RiskAccumulator(n_cells=1, clock=clock)
    high = np.array([100], dtype=np.uint8)
    low = np.array([0], dtype=np.uint8)

    acc.update(high, calibration_factor=0.5, critical_pressure=32, critical_time=10)
    advance(10 * 60.0)
    fired, _, cleared = acc.update(high, calibration_factor=0.5, critical_pressure=32, critical_time=10)
    assert fired.tolist() == [0]
    assert cleared.size == 0  # still risky, nothing to clear yet

    advance(60.0)
    fired, _, cleared = acc.update(low, calibration_factor=0.5, critical_pressure=32, critical_time=10)
    assert fired.size == 0
    assert cleared.tolist() == [0]

    # Already cleared -- must not be reported again on a later tick.
    advance(60.0)
    fired, _, cleared = acc.update(low, calibration_factor=0.5, critical_pressure=32, critical_time=10)
    assert cleared.size == 0


def test_rejects_wrong_length_vector():
    acc = RiskAccumulator(n_cells=4)
    with pytest.raises(ValueError):
        acc.update(np.zeros(3, dtype=np.uint8), calibration_factor=0.5,
                   critical_pressure=32, critical_time=10)


def test_reset_reports_previously_active_cells_as_cleared():
    """After a `reset`, the server must be told the active warning is gone,
    or it keeps showing it even once pressure drops."""
    clock, advance = make_clock()
    acc = RiskAccumulator(n_cells=4, alert_cooldown_s=0.0, clock=clock)
    hot = np.array([100, 0, 0, 0], dtype=np.uint8)
    kwargs = dict(calibration_factor=0.5, critical_pressure=32, critical_time=1.0)
    acc.update(hot, **kwargs)
    advance(61.0)
    fired, _, _ = acc.update(hot, **kwargs)
    assert fired.tolist() == [0]

    acc.reset()
    acc.resync()
    _, _, cleared = acc.update(hot, **kwargs)
    assert cleared.tolist() == [0]
    # Reported only once.
    _, _, cleared = acc.update(hot, **kwargs)
    assert cleared.tolist() == []

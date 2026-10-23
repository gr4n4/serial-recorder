"""Per-cell risk accumulation for the pressure-monitoring client.

risk_mask = pressure_vector > (critical_pressure / calibration_factor)
accumulated_risk = (accumulated_risk + risk_mask * dt_minutes) * risk_mask

`dt_minutes` is derived from wall-clock elapsed time (not tick count) so the
critical_time comparison stays accurate regardless of jitter in the frame
arrival rate.
"""
import threading
import time

import numpy as np


class RiskAccumulator:
    def __init__(self, n_cells, alert_cooldown_s=300.0, clock=time.monotonic):
        self.n_cells = n_cells
        self.alert_cooldown_s = alert_cooldown_s
        self._clock = clock
        # update() runs on the serial thread; reset()/resync() come from the
        # command-poller thread (via MonitorState.apply).
        self._lock = threading.Lock()
        self.accumulated_risk = np.zeros(n_cells, dtype=np.float64)
        self.last_alert_time = np.full(n_cells, -np.inf, dtype=np.float64)
        self._last_tick_ts = None
        # Cells currently reported to the server as an active warning (i.e.
        # fired and not yet cleared). Used to detect when a previously-fired
        # cell's pressure drops back under threshold, so the caller can tell
        # the server the warning is resolved.
        self.active_idx = set()
        # Active cells dropped by reset(); reported as cleared on the next
        # update() so the server's warning for them is resolved too.
        self._pending_clear = set()

    def update(self, pressure_vector, calibration_factor, critical_pressure,
               critical_time, detection_mask=None, now=None):
        """Advance the accumulator by one frame.

        detection_mask: optional bool array (True = detect), shape
        (n_cells,). Cells where it's False never accumulate risk or fire,
        regardless of pressure. None means the whole grid is detected.

        Returns (fired_idx, risk_mask, cleared_idx):
          - risk_mask: bool array, cells currently over the pressure threshold
            (and within the detection mask)
          - fired_idx: int array, cells that just reached critical_time AND
            are past their alert cooldown (i.e. should be reported now)
          - cleared_idx: int array, cells that were part of an active
            (fired, unresolved) warning but have now dropped back under the
            pressure threshold -- i.e. the warning for those cells resolved
        """
        if pressure_vector.shape[0] != self.n_cells:
            raise ValueError(
                f"pressure_vector has {pressure_vector.shape[0]} cells, "
                f"expected {self.n_cells}"
            )
        now = self._clock() if now is None else now
        with self._lock:
            return self._update_locked(pressure_vector, calibration_factor,
                                       critical_pressure, critical_time,
                                       detection_mask, now)

    def _update_locked(self, pressure_vector, calibration_factor, critical_pressure,
                       critical_time, detection_mask, now):
        if self._last_tick_ts is None:
            dt_min = 0.0
        else:
            dt_min = max(0.0, now - self._last_tick_ts) / 60.0
        self._last_tick_ts = now

        threshold = critical_pressure / calibration_factor
        risk_mask = pressure_vector > threshold
        if detection_mask is not None:
            risk_mask = risk_mask & detection_mask

        self.accumulated_risk = (self.accumulated_risk + risk_mask * dt_min) * risk_mask

        reached = self.accumulated_risk >= critical_time
        cooldown_ok = (now - self.last_alert_time) >= self.alert_cooldown_s
        fired_mask = reached & cooldown_ok
        fired_idx = np.nonzero(fired_mask)[0]
        if fired_idx.size:
            self.last_alert_time[fired_idx] = now
            self.active_idx.update(fired_idx.tolist())

        not_risky_now = np.nonzero(~risk_mask)[0]
        cleared = self.active_idx.intersection(not_risky_now.tolist())
        if cleared:
            self.active_idx.difference_update(cleared)
        # Cells dropped by reset() that haven't re-fired this frame.
        cleared |= self._pending_clear - self.active_idx
        self._pending_clear = set()
        cleared_idx = np.array(sorted(cleared), dtype=np.int64)

        return fired_idx, risk_mask, cleared_idx

    def reset(self):
        """Zero out accumulated_risk (the `reset` command). Alert cooldowns
        are left untouched -- only the accumulated risk itself is cleared.
        Cells that were an active warning are reported as cleared by the
        next update(), so the server doesn't keep showing them forever."""
        with self._lock:
            self.accumulated_risk = np.zeros(self.n_cells, dtype=np.float64)
            self._pending_clear |= self.active_idx
            self.active_idx = set()

    def resync(self):
        """Forget the last tick timestamp so the next update() computes
        dt=0 instead of a large jump. Must be called whenever monitoring
        resumes after being paused/stopped, since no update() calls (and
        therefore no elapsed-time accounting) happened while inactive."""
        with self._lock:
            self._last_tick_ts = None

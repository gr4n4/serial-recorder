"""Client-local configuration: static CLI/file settings plus the
server-managed runtime thresholds (calibration_factor, critical_pressure,
critical_time) and detection mask, which are refreshed by periodic
polling."""
import os
import sys
import threading
from dataclasses import dataclass

import numpy as np

# common/config.py (the shared sensor grid size) sits one level above
# risk_monitor/, which isn't on sys.path when this module is imported
# directly (e.g. by tests) rather than via client/main.py, which does
# this same insert.
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_APP_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.config import DEFAULT_COLS, DEFAULT_HEADER_HEX, DEFAULT_ROWS  # noqa: E402

DEFAULT_WARNING_LOG_DIR = os.path.join(os.path.expanduser("~"), "risk_monitor_logs", "client_warnings")

DEFAULT_CALIBRATION_FACTOR = 0.4755
DEFAULT_CRITICAL_PRESSURE = 32.0  # mmHg
DEFAULT_CRITICAL_TIME = 90.0  # minutes
DEFAULT_PRESSURE_MASK_THRESHOLD = 10  # fixed, not server-configurable


@dataclass(frozen=True)
class ClientConfig:
    client_id: str = ""
    port: str = "/dev/ttyUSB0"
    baud: int = 921600
    cols: int = DEFAULT_COLS
    rows: int = DEFAULT_ROWS
    header_hex: str = DEFAULT_HEADER_HEX

    server_base_url: str = "http://localhost:5000"
    config_path: str = "/config"
    event_path: str = "/event"
    event_clear_path: str = "/event/clear"
    image_path: str = "/image"
    command_path: str = "/command"
    state_path: str = "/state"
    poll_interval_s: float = 60.0
    command_poll_interval_s: float = 2.0
    http_timeout_s: float = 5.0
    http_retry_delay_s: float = 5.0
    alert_cooldown_s: float = 300.0
    pressure_mask_threshold: int = DEFAULT_PRESSURE_MASK_THRESHOLD
    event_queue_maxsize: int = 64
    warning_log_dir: str = DEFAULT_WARNING_LOG_DIR


class RuntimeConfig:
    """Thread-safe holder for the server-managed thresholds plus the
    detection mask.

    `update()` swaps the whole snapshot tuple atomically so a concurrent
    `get()` from the frame-processing thread never observes a torn mix of
    old/new values. Updating never touches accumulated_risk state — new
    values simply apply starting with the next `RiskAccumulator.update()`
    call.

    detection_mask is a bool array (True = detect, False = excluded from
    risk detection), shape (n_cells,). Defaults to all-True (whole grid
    detected) until the server sends a mask.
    """

    def __init__(self,
                 calibration_factor=DEFAULT_CALIBRATION_FACTOR,
                 critical_pressure=DEFAULT_CRITICAL_PRESSURE,
                 critical_time=DEFAULT_CRITICAL_TIME,
                 detection_mask=None):
        self._lock = threading.Lock()
        self._snapshot = (calibration_factor, critical_pressure, critical_time,
                           detection_mask)

    def update(self, calibration_factor, critical_pressure, critical_time,
               detection_mask=None):
        with self._lock:
            self._snapshot = (calibration_factor, critical_pressure, critical_time,
                               detection_mask)

    def get(self):
        with self._lock:
            return self._snapshot


def build_detection_mask(n_cells, excluded_idx):
    """Builds a bool array of length n_cells, True everywhere except at the
    (flat) indices listed in excluded_idx. Indices outside [0, n_cells)
    (e.g. a mask saved for a different grid size) are ignored rather than
    raising or -- for negative values -- wrapping to the wrong cell."""
    mask = np.ones(n_cells, dtype=bool)
    if excluded_idx:
        idx = np.asarray(excluded_idx, dtype=np.int64)
        idx = idx[(idx >= 0) & (idx < n_cells)]
        mask[idx] = False
    return mask

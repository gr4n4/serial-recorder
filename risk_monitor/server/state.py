"""Thread-safe in-memory state for the real server: config thresholds,
pending monitor command, latest/history risk events, and the latest
reported pressure state. A single global mutation lock is enough at this
scale (mock_server/server.py uses the same pattern)."""
import os
import sys
import threading
import time
from collections import deque

# The sensor grid size (DEFAULT_COLS/DEFAULT_ROWS) lives in the repo-root
# common/config.py, shared with pressure_recorder and the client, so it
# only ever needs changing in one place. common/ sits one level above
# risk_monitor/, which isn't on sys.path when the server is run as
# `python -m server.app` from within risk_monitor/ (unlike client/main.py,
# which already does this same insert).
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_APP_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.config import DEFAULT_COLS, DEFAULT_ROWS  # noqa: E402

DEFAULT_CALIBRATION_FACTOR = 0.4755
DEFAULT_CRITICAL_PRESSURE = 32.0
DEFAULT_CRITICAL_TIME = 90.0
EVENT_HISTORY_MAXLEN = 50


class ServerState:
    def __init__(self, cols=DEFAULT_COLS, rows=DEFAULT_ROWS,
                 calibration_factor=DEFAULT_CALIBRATION_FACTOR,
                 critical_pressure=DEFAULT_CRITICAL_PRESSURE,
                 critical_time=DEFAULT_CRITICAL_TIME, clock=time.time):
        self._lock = threading.Lock()
        self._clock = clock
        self.config = {
            "cols": cols,
            "rows": rows,
            "calibration_factor": calibration_factor,
            "critical_pressure": critical_pressure,
            "critical_time": critical_time,
            "mask_excluded_idx": [],  # flat cell indices excluded from detection; empty = whole grid detected
        }
        self.pending_command = None
        self.latest_event = None
        self.event_history = deque(maxlen=EVENT_HISTORY_MAXLEN)
        self.latest_state = None
        self.state_reports = []
        self.latest_image = None
        self.last_seen = None
        self.connected = None  # None = unknown (no contact yet)

    def get_config(self):
        with self._lock:
            return dict(self.config)

    def update_config(self, updates):
        with self._lock:
            self.config.update(updates)
            return dict(self.config)

    def set_command(self, command):
        with self._lock:
            self.pending_command = command

    def consume_command(self):
        with self._lock:
            cmd = self.pending_command
            self.pending_command = None
            return cmd

    def record_event(self, payload):
        """The client only reports the cells that *just* fired, so a new
        event is merged into the still-active warning instead of replacing
        it -- otherwise cells from an earlier event that are still over
        threshold would vanish from the live view (and clearing the newer
        cells would drop the warning entirely). event_history keeps the raw
        per-event record."""
        record = dict(payload)
        record["received_at"] = self._clock()
        with self._lock:
            self.latest_event = self._merge_into_latest(record)
            self.event_history.append(record)
            # A new event has its own pressure_mask_idx silhouette; drop any
            # earlier "현재 상태 요청" snapshot so the dashboard grid draws
            # *this* event's silhouette instead of overlaying its risky
            # cells on a stale, unrelated pressure snapshot (see
            # clear_latest_state).
            self.latest_state = None
        return record

    def _merge_into_latest(self, record):
        prev = self.latest_event
        if not prev or bool(prev.get("is_mock")) != bool(record.get("is_mock")):
            return record
        new_idx = record.get("risky_idx") or []
        new_pressure = record.get("risky_pressure") or [None] * len(new_idx)
        merged = {}
        prev_idx = prev.get("risky_idx") or []
        prev_pressure = prev.get("risky_pressure") or [None] * len(prev_idx)
        for i, p in zip(prev_idx, prev_pressure):
            merged[i] = p
        for i, p in zip(new_idx, new_pressure):
            merged[i] = p  # newer reading wins for re-fired cells
        updated = dict(record)
        updated["risky_idx"] = list(merged.keys())
        updated["risky_pressure"] = list(merged.values())
        return updated

    def clear_event(self, cleared_idx):
        """Removes `cleared_idx` cells from latest_event's risky_idx (the
        client reports these once their pressure drops back under
        threshold). If no risky cells remain, the warning is fully
        resolved and latest_event is dropped, so the dashboard's
        has_warning flips back to False. event_history is left untouched
        -- it's a record of what was actually reported, not a live view."""
        cleared_idx = set(cleared_idx)
        with self._lock:
            if not self.latest_event:
                return None
            risky_idx = self.latest_event.get("risky_idx", [])
            risky_pressure = self.latest_event.get("risky_pressure", [])
            remaining = [
                (i, p) for i, p in zip(risky_idx, risky_pressure or [None] * len(risky_idx))
                if i not in cleared_idx
            ]
            if not remaining:
                self.latest_event = None
                # The snapshot belongs to the warning that just ended; don't
                # keep serving it from /image/latest.
                self.latest_image = None
                return None
            updated = dict(self.latest_event)
            updated["risky_idx"] = [i for i, _ in remaining]
            updated["risky_pressure"] = [p for _, p in remaining]
            self.latest_event = updated
            return dict(updated)

    def clear_mock_latest_event(self):
        """Drops mock-tagged records from event_history, and un-sticks
        latest_event if it's currently showing a mock warning -- so the
        dashboard canvas stops rendering a mock event's risky cells after
        "모의 경고 삭제" instead of redrawing the same stale picture on
        every poll until the next real event arrives."""
        with self._lock:
            self.event_history = deque(
                (r for r in self.event_history if not r.get("is_mock")),
                maxlen=EVENT_HISTORY_MAXLEN,
            )
            if self.latest_event and self.latest_event.get("is_mock"):
                self.latest_event = self.event_history[-1] if self.event_history else None
                if self.latest_event is None:
                    self.latest_image = None

    def record_state(self, payload):
        with self._lock:
            self.latest_state = dict(payload)
            self.state_reports.append(dict(payload))
        return self.latest_state

    def get_latest_state(self):
        with self._lock:
            return dict(self.latest_state) if self.latest_state else None

    def record_image(self, image_bytes):
        with self._lock:
            self.latest_image = image_bytes

    def get_latest_image(self):
        with self._lock:
            return self.latest_image

    def touch(self):
        """Records that the client just contacted the server (GET /config,
        GET /command, POST /event/state/image) -- the connection heartbeat."""
        with self._lock:
            self.last_seen = self._clock()

    def get_last_seen(self):
        with self._lock:
            return self.last_seen

    def set_connected(self, connected):
        """Updates the connected flag; returns True iff this is a real
        transition (not the first-ever observation), so callers can alert
        exactly once per disconnect instead of on every check."""
        with self._lock:
            changed = self.connected is not None and self.connected != connected
            self.connected = connected
            return changed

    def is_connected(self):
        with self._lock:
            return self.connected

    def snapshot(self):
        """Everything the dashboard's /api/latest needs, in one locked read."""
        with self._lock:
            return {
                "cols": self.config["cols"],
                "rows": self.config["rows"],
                "mask_excluded_idx": list(self.config["mask_excluded_idx"]),
                "latest_event": dict(self.latest_event) if self.latest_event else None,
                "latest_state": dict(self.latest_state) if self.latest_state else None,
                "has_image": self.latest_image is not None,
                "connected": self.connected,
                "last_seen": self.last_seen,
            }

    def has_warning(self):
        with self._lock:
            return self.latest_event is not None


class ClientRegistry:
    """Holds one ServerState per connected client_id, created lazily on
    first contact. Multiple Raspberry Pi clients can report to the same
    server concurrently; each gets its own independent config/event/state,
    so one client's data never overwrites another's (see CLAUDE.md: the
    server supports multiple device clients)."""

    def __init__(self, cols=DEFAULT_COLS, rows=DEFAULT_ROWS,
                 calibration_factor=DEFAULT_CALIBRATION_FACTOR,
                 critical_pressure=DEFAULT_CRITICAL_PRESSURE,
                 critical_time=DEFAULT_CRITICAL_TIME, clock=time.time):
        self._lock = threading.Lock()
        self._defaults = dict(
            cols=cols, rows=rows, calibration_factor=calibration_factor,
            critical_pressure=critical_pressure, critical_time=critical_time,
            clock=clock,
        )
        self._clients = {}

    def get_or_create(self, client_id):
        with self._lock:
            state = self._clients.get(client_id)
            if state is None:
                state = ServerState(**self._defaults)
                self._clients[client_id] = state
            return state

    def get(self, client_id):
        with self._lock:
            return self._clients.get(client_id)

    def all_ids(self):
        with self._lock:
            return list(self._clients.keys())

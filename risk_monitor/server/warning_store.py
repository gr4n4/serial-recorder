"""Persists received risk warnings to disk: one JSON-line per warning in
warnings.log, plus the snapshot PNG (if/when POST /image arrives) saved
under images/. Kept separate from ServerState (which only holds the
latest-for-dashboard values in memory) so a restart doesn't lose history."""
import bisect
import json
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


DATA_DIRNAME = "press_warnings"


def _default_dir():
    """<exe folder>\\press_warnings when running as the frozen PyInstaller
    exe, so the data sits next to the deployed app. A subfolder rather than
    the exe folder itself, so per-client directories can't collide with
    PyInstaller's _internal/. Running from source: %APPDATA%\\carerobot\\
    press_warnings on Windows, a home-directory fallback elsewhere (dev
    machines / CI don't set APPDATA)."""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve().parent / DATA_DIRNAME)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return str(Path(appdata) / "carerobot" / DATA_DIRNAME)
    return str(Path.home() / DATA_DIRNAME)


DEFAULT_DIR = _default_dir()
LOG_FILENAME = "warnings.log"
IMAGE_DIRNAME = "images"


def _nearest_gap(sorted_times, ts):
    """Distance from ts to the closest value in sorted_times (inf if empty)."""
    i = bisect.bisect_left(sorted_times, ts)
    gaps = [abs(sorted_times[j] - ts) for j in (i - 1, i) if 0 <= j < len(sorted_times)]
    return min(gaps, default=float("inf"))


class WarningStore:
    def __init__(self, base_dir=DEFAULT_DIR):
        self.base_dir = Path(base_dir)
        self.image_dir = self.base_dir / IMAGE_DIRNAME
        self.log_path = self.base_dir / LOG_FILENAME
        self._lock = threading.Lock()
        self._event_times_cache = (None, [])
        self._image_times_cache = (None, ([], []))
        self.image_dir.mkdir(parents=True, exist_ok=True)

    def find_image(self, received_at, is_mock=False, before=1.0, after=10.0):
        """Path of the snapshot PNG belonging to the warning logged at
        `received_at`, or None. POST /event and POST /image are separate
        requests stamped independently, so a real warning's image filename
        differs from the event's received_at by a few ms; pick the closest
        unprefixed PNG within [-before, +after] seconds that no other
        logged warning is closer to. Mock images share the exact stamp."""
        if is_mock:
            path = self.image_dir / f"mock_{received_at:.6f}.png"
            return path if path.exists() else None
        exact = self.image_dir / f"{received_at:.6f}.png"
        if exact.exists():
            return exact
        event_times = self._event_times()
        image_times, image_paths = self._image_times()
        best, best_gap = None, None
        lo = bisect.bisect_left(image_times, received_at - before)
        hi = bisect.bisect_right(image_times, received_at + after)
        for ts, path in zip(image_times[lo:hi], image_paths[lo:hi]):
            gap = abs(ts - received_at)
            if _nearest_gap(event_times, ts) < gap:
                continue  # closer to another warning
            if best_gap is None or gap < best_gap:
                best, best_gap = path, gap
        return best if best is not None and best.exists() else None

    def _event_times(self):
        """Sorted received_at of every real (non-mock) logged warning,
        re-parsed only when warnings.log changes -- the dashboard requests
        one history image per row, so re-reading the whole log each time
        gets slow once months of warnings have piled up."""
        try:
            st = self.log_path.stat()
            key = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return []
        if self._event_times_cache[0] != key:
            times = [r.get("received_at") for r in self.read_history(limit=None)
                     if not r.get("is_mock")]
            times = sorted(t for t in times if isinstance(t, (int, float)))
            self._event_times_cache = (key, times)
        return self._event_times_cache[1]

    def _image_times(self):
        """(sorted timestamps, matching paths) of unprefixed (real warning)
        PNGs, re-scanned only when images/ changes."""
        try:
            key = self.image_dir.stat().st_mtime_ns
        except FileNotFoundError:
            return [], []
        if self._image_times_cache[0] != key:
            entries = []
            for path in self.image_dir.glob("*.png"):
                try:
                    entries.append((float(path.stem), path))
                except ValueError:
                    continue  # mock_/status_ prefixed
            entries.sort(key=lambda e: e[0])
            self._image_times_cache = (
                key, ([ts for ts, _ in entries], [p for _, p in entries])
            )
        return self._image_times_cache[1]

    def log_event(self, payload, received_at):
        """Appends one JSON line for a client risk warning (POST /event)."""
        record = dict(payload)
        record["received_at"] = received_at
        record["server_time"] = datetime.now().astimezone().isoformat()
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        self._event_times_cache = (None, [])
        logger.info("warning logged -> %s", self.log_path)
        return record

    def read_history(self, limit=50):
        """Returns the most recent `limit` logged warnings (POST /event
        records), newest first, for the dashboard's history list."""
        if not self.log_path.exists():
            return []
        with self._lock:
            with open(self.log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        records = []
        for line in (lines if limit is None else lines[-limit:]):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                logger.warning("skipping malformed warnings.log line")
        records.reverse()
        return records

    def annotate_site(self, received_at, site):
        """Attaches a user-entered '위험 발생 부위' (body site) to the
        warning record matching `received_at`, rewriting warnings.log in
        place. Returns True if a matching record was found and updated."""
        with self._lock:
            if not self.log_path.exists():
                return False
            with open(self.log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            found = False
            new_lines = []
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    new_lines.append(line)
                    continue
                try:
                    record = json.loads(stripped)
                except ValueError:
                    new_lines.append(line)
                    continue
                if not found and record.get("received_at") == received_at:
                    record["site"] = site
                    found = True
                    new_lines.append(json.dumps(record, ensure_ascii=False) + "\n")
                else:
                    new_lines.append(line)
            if found:
                with open(self.log_path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
        return found

    def clear_mock_events(self):
        """Removes all mock-warning records (is_mock=True) from
        warnings.log, plus their saved snapshot images (see save_image's
        "mock_" filename prefix), keeping real client warnings/images
        intact. Returns the number of log records removed."""
        with self._lock:
            if not self.log_path.exists():
                removed = 0
            else:
                with open(self.log_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                kept_lines = []
                removed = 0
                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        kept_lines.append(line)
                        continue
                    try:
                        record = json.loads(stripped)
                    except ValueError:
                        kept_lines.append(line)
                        continue
                    if record.get("is_mock"):
                        removed += 1
                    else:
                        kept_lines.append(line)
                if removed:
                    with open(self.log_path, "w", encoding="utf-8") as f:
                        f.writelines(kept_lines)
            for path in self.image_dir.glob("mock_*.png"):
                path.unlink()
            for path in self.image_dir.glob("mock_*.json"):
                path.unlink()
        return removed

    def save_image(self, image_bytes, received_at, is_mock=False, is_status=False):
        """Saves a warning snapshot PNG (POST /image, a mock warning's
        generated snapshot, or a STATUS-requested frame), named by its
        received timestamp so it can be matched up with warnings.log. Mock
        snapshots get a "mock_" prefix so clear_mock_events can find and
        remove them; STATUS-requested frames (not tied to a risk warning)
        get a "status_" prefix."""
        prefix = "mock_" if is_mock else ("status_" if is_status else "")
        filename = f"{prefix}{received_at:.6f}.png"
        path = self.image_dir / filename
        with open(path, "wb") as f:
            f.write(image_bytes)
        # Don't rely on the directory mtime alone (coarse on some filesystems).
        self._image_times_cache = (None, ([], []))
        logger.info("warning image saved -> %s", path)
        return str(path)

    def save_status(self, pressure, cols, rows, received_at, is_mock=False, is_status=False):
        """Saves the raw 0-255 pressure grid (STATUS data) behind a
        warning snapshot, alongside its image (same received_at, same
        prefix convention), so the frame that produced the image can be
        inspected/reconstructed later."""
        prefix = "mock_" if is_mock else ("status_" if is_status else "")
        filename = f"{prefix}{received_at:.6f}.json"
        path = self.image_dir / filename
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"cols": cols, "rows": rows, "pressure": list(pressure)}, f)
        logger.info("warning status saved -> %s", path)
        return str(path)

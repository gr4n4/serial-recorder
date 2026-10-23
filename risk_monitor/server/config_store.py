"""Persists the dashboard-managed config (thresholds + mask) to disk as a
single JSON file, so a server restart (crash or otherwise) restores the
last values the dashboard set instead of falling back to hardcoded
defaults. Kept separate from ServerState (in-memory only) the same way
WarningStore is."""
import json
import logging
import threading
from pathlib import Path

# Same default as warning_store.py, so config.json lives alongside the
# other persisted server data.
from server.warning_store import DEFAULT_DIR

logger = logging.getLogger(__name__)

FILENAME = "config.json"


class ConfigStore:
    def __init__(self, base_dir=DEFAULT_DIR):
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / FILENAME
        self._lock = threading.Lock()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def load(self):
        """Returns the last-saved config dict, or None if none was ever
        saved (first run, or the file is missing/corrupt)."""
        with self._lock:
            if not self.path.exists():
                return None
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("failed to load %s, ignoring: %s", self.path, e)
                return None

    def save(self, config):
        """Overwrites the saved config with the current full snapshot."""
        with self._lock:
            tmp_path = self.path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            tmp_path.replace(self.path)
        logger.info("config saved -> %s", self.path)

    def delete(self):
        """Removes the saved config file, if any."""
        with self._lock:
            try:
                self.path.unlink()
                logger.info("config deleted -> %s", self.path)
            except FileNotFoundError:
                pass

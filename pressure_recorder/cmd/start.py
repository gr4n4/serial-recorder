"""Headless CLI recorder.

Usage:
    python cmd/start.py [--port /dev/ttyUSB0] [--outdir DIR]
                        [--baud 921600] [--cols 32] [--rows 64]
                        [--header A55A01060801]
                        [--interval 1.0]
                        [--upload] [--upload-interval 60]
                        [--wandb-project NAME] [--wandb-entity NAME]
                        [--wandb-run-name NAME] [--wandb-offline-after 10]
                        [--dry-run] [--dry-fps 30]
"""
import argparse
import os
import signal
import sys
import threading
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
for _p in (_PROJECT_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np

from sensor import config
from sensor.csv_logger import CsvLogger
from common.frame_parser import FrameParser
from common.serial_reader import SerialReader


DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_OUT_DIR = os.path.join(os.path.expanduser("~"), "sensor_logs")


def build_parser():
    p = argparse.ArgumentParser(prog="sensor.start", description="Sensor Recorder CLI")
    p.add_argument("--port", default=DEFAULT_PORT, help=f"Serial port (default: {DEFAULT_PORT})")
    p.add_argument("--baud", type=int, default=config.DEFAULT_BAUD)
    p.add_argument("--cols", type=int, default=config.DEFAULT_COLS)
    p.add_argument("--rows", type=int, default=config.DEFAULT_ROWS)
    p.add_argument("--header", default=config.DEFAULT_HEADER_HEX, help="hex string, e.g. A55A")
    p.add_argument("--interval", type=float, default=1.0,
                   help="save interval in seconds (default 1.0, 0 = every frame)")
    p.add_argument("--outdir", default=None,
                   help=f"output directory (default: {DEFAULT_OUT_DIR}). "
                        f"File name is always log_<timestamp>.csv")
    p.add_argument("--upload", action="store_true",
                   help="stream metrics to wandb and upload CSV as artifact periodically")
    p.add_argument("--upload-interval", type=float, default=60.0,
                   help="poll cadence in seconds for date-rollover check and "
                        "pending-upload retry (default 60). Rotation itself "
                        "is triggered on calendar date change.")
    p.add_argument("--wandb-project", default="sensor-recorder", help="wandb project name")
    p.add_argument("--wandb-entity", default=None, help="wandb entity (user/team)")
    p.add_argument("--wandb-run-name", default=None, help="wandb run name (default: auto)")
    p.add_argument("--wandb-offline-after", type=int, default=10,
                   help="switch to wandb offline mode after N failed online init "
                        "attempts (default 10 ≈ 5 min at 30s retries; 0 = never)")
    p.add_argument("--dry-run", action="store_true",
                   help="generate synthetic frames instead of reading the serial port "
                        "(for testing CSV writes, wandb upload, and network resilience)")
    p.add_argument("--dry-fps", type=float, default=30.0,
                   help="synthetic frame rate in dry-run mode (default 30)")
    return p


class FakeReader:
    """Synthetic frame producer for --dry-run. Mimics SerialReader.start/stop."""

    def __init__(self, cols, rows, fps, on_frame, on_status=None):
        self.cols = cols
        self.rows = rows
        self.period = 1.0 / max(fps, 1e-3)
        self.on_frame = on_frame
        self.on_status = on_status or (lambda c, m: None)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self):
        self.on_status(True, "Dry-run: synthetic frames")
        x = np.linspace(0, 4 * np.pi, self.cols, dtype=np.float32)
        y = np.linspace(0, 4 * np.pi, self.rows, dtype=np.float32)
        xx, yy = np.meshgrid(x, y)
        i = 0
        while not self._stop.is_set():
            wave = 127 + 100 * np.sin(xx + i * 0.1) * np.cos(yy + i * 0.05)
            frame = np.clip(wave, 0, 255).astype(np.uint8)
            try:
                self.on_frame(time.time(), frame)
            except Exception:
                pass
            i += 1
            if self._stop.wait(self.period):
                break
        self.on_status(False, "Dry-run: stopped")


def resolve_outpath(outdir):
    """Build the output CSV path from a directory. Filename is always
    log_<timestamp>.csv. Relative dirs resolve under DEFAULT_OUT_DIR."""
    if outdir is None or outdir == "":
        directory = DEFAULT_OUT_DIR
    else:
        directory = os.path.expanduser(outdir)
        if not os.path.isabs(directory):
            directory = os.path.join(DEFAULT_OUT_DIR, directory)
    fname = time.strftime("log_%Y%m%d_%H%M%S.csv")
    return os.path.join(directory, fname)


class WandbUploader:
    """Streams scalar metrics to wandb. The Recorder owns the CSV file;
    rotation is triggered by the Recorder when the calendar date changes
    (system clock crosses midnight). On rotation, the closed file is
    uploaded as a wandb artifact.

    Resilient to network drops:
      - wandb.init is retried in the background until it succeeds
      - after N failed online inits, falls back to wandb offline mode
        so log data persists to disk instead of accumulating in RAM
      - failed artifact uploads are queued and retried on every poll
        (and once more at shutdown)
      - if consecutive artifact uploads fail, wandb.log() is paused to
        prevent the SDK's internal queue from growing unboundedly
      - file rotation is decoupled from wandb state — recording keeps
        producing one CSV per calendar day even when offline for days
    """

    INIT_RETRY_S = 30.0
    LOG_DISABLE_FAILURES = 3  # pause wandb.log() after N consecutive upload fails

    def __init__(self, args, outpath, stop_event, recorder):
        try:
            import wandb  # noqa: F401
        except ImportError as e:
            raise RuntimeError("wandb not installed. Run: pip install wandb") from e
        import wandb
        self.wandb = wandb
        self.args = args
        self.base_name = os.path.splitext(os.path.basename(outpath))[0]
        self.interval = args.upload_interval
        self.stop_event = stop_event
        self.recorder = recorder
        self.run = None
        self._wandb_config = {
            "port": args.port,
            "baud": args.baud,
            "cols": args.cols,
            "rows": args.rows,
            "header": args.header,
            "save_interval": args.interval,
            "outpath": outpath,
        }
        self._pending = []  # list of (path, final_flag) waiting to upload
        self._pending_lock = threading.Lock()
        self._init_failures = 0
        self._log_failures = 0
        self._upload_failures = 0  # consecutive artifact upload failures
        self._log_enabled = True   # gates wandb.log() to protect RAM
        self._offline_mode = False
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        # Attempt the first init synchronously so the user sees the run URL
        # immediately when network is up; if it fails we retry in the loop.
        self._try_init()
        self._thread.start()

    def _try_init(self):
        if self.run is not None:
            return True
        # Fall back to offline mode after too many online failures.
        # In offline mode wandb writes everything to disk locally and never
        # buffers in RAM, so a long offline period doesn't OOM the process.
        threshold = self.args.wandb_offline_after
        use_offline = (
            threshold > 0
            and self._init_failures >= threshold
            and not self._offline_mode
        )
        kwargs = dict(
            project=self.args.wandb_project,
            entity=self.args.wandb_entity,
            name=self.args.wandb_run_name,
            config=self._wandb_config,
            reinit=True,
        )
        if use_offline:
            kwargs["mode"] = "offline"
        try:
            self.run = self.wandb.init(**kwargs)
            if use_offline:
                self._offline_mode = True
                print(f"[wandb] online init failed {self._init_failures} times; "
                      f"switched to OFFLINE mode. Run `wandb sync` later to "
                      f"upload logs.", file=sys.stderr, flush=True)
            else:
                print(f"[wandb] run: {self.run.url}", flush=True)
            self._init_failures = 0
            return True
        except Exception as e:
            self._init_failures += 1
            if self._init_failures <= 3 or self._init_failures % 10 == 0:
                print(f"[wandb] init failed (attempt {self._init_failures}): {e}. "
                      f"Will retry.", file=sys.stderr, flush=True)
            self.run = None
            return False

    def log_frame(self, ts, frame, saved_count):
        if self.run is None:
            return  # not connected yet; metric stream resumes after init succeeds
        if not self._log_enabled:
            return  # paused to keep wandb SDK's queue from growing in RAM
        try:
            self.wandb.log(
                {
                    "min": int(frame.min()),
                    "max": int(frame.max()),
                    "mean": float(frame.mean()),
                    "saved_count": saved_count,
                },
                step=saved_count,
            )
            self._log_failures = 0
        except Exception as e:
            self._log_failures += 1
            if self._log_failures <= 3 or self._log_failures % 50 == 0:
                print(f"[wandb] log failed (#{self._log_failures}): {e}",
                      file=sys.stderr, flush=True)

    def _enqueue(self, path, final):
        with self._pending_lock:
            self._pending.append((path, final))

    def _flush_pending(self):
        if self.run is None:
            return
        with self._pending_lock:
            queue = list(self._pending)
            self._pending.clear()
        leftover = []
        for path, final in queue:
            if not self._upload_once(path, final):
                leftover.append((path, final))
        if leftover:
            with self._pending_lock:
                # prepend so order is preserved on next attempt
                self._pending = leftover + self._pending

    def _upload_once(self, path, final):
        """Single attempt. Returns True on success, False on failure."""
        if not os.path.exists(path):
            print(f"[wandb] file missing, dropping: {path}", flush=True)
            return True  # don't keep retrying a missing file
        try:
            art = self.wandb.Artifact(
                name=self.base_name,
                type="sensor-log",
                metadata={
                    "final": final,
                    "uploaded_at": time.time(),
                    "source_file": os.path.basename(path),
                },
            )
            art.add_file(path)
            self.run.log_artifact(art)
            tag = "final" if final else "periodic"
            print(f"[wandb] uploaded artifact ({tag}) <- {path}", flush=True)
            self._upload_failures = 0
            # Resume metric streaming if it was paused (online mode only).
            if not self._log_enabled and not self._offline_mode:
                print("[wandb] resuming metric stream", flush=True)
                self._log_enabled = True
            return True
        except Exception as e:
            print(f"[wandb] upload failed for {path}: {e}. Will retry.",
                  file=sys.stderr, flush=True)
            self._upload_failures += 1
            # In online mode, pause log() if uploads keep failing so the
            # wandb SDK's internal queue doesn't grow unboundedly in RAM.
            if (not self._offline_mode
                    and self._log_enabled
                    and self._upload_failures >= self.LOG_DISABLE_FAILURES):
                self._log_enabled = False
                print(f"[wandb] pausing metric stream after "
                      f"{self._upload_failures} upload failures (RAM guard)",
                      file=sys.stderr, flush=True)
            return False

    def _upload_file(self, path, final=False):
        """Try to upload now; on failure, enqueue for retry."""
        if self.run is None or not self._upload_once(path, final):
            self._enqueue(path, final)

    def _loop(self):
        # Rotation triggers on calendar-date change (independent of wandb state).
        # `--upload-interval` is the poll cadence for the date check and for
        # retrying pending uploads / init.
        poll_s = max(1.0, self.interval)
        now = time.monotonic()
        next_init_retry = now  # try immediately on first iteration if disconnected
        while not self.stop_event.is_set():
            if self.stop_event.wait(poll_s):
                break

            now = time.monotonic()
            if self.run is None and now >= next_init_retry:
                self._try_init()
                next_init_retry = now + self.INIT_RETRY_S

            # Drain prior failures whenever we have a run available.
            if self.run is not None:
                self._flush_pending()

            # Rotate ONLY when the system date has changed.
            closed_path = self.recorder.rotate_if_date_changed()
            if closed_path is not None:
                # Upload the closed (yesterday's) file to the current run...
                self._upload_file(closed_path, final=False)
                # ...then finish the run so a fresh run starts for the new day.
                self._rotate_run()
                next_init_retry = time.monotonic()  # try new init immediately

    def _rotate_run(self):
        """Called on calendar date rollover. Finalize the current wandb run
        (if any) and reset state so a fresh run is initialized for the new
        day. Online init is attempted again from scratch — useful when the
        previous day was in offline/disconnected mode and wifi may now be
        back."""
        if self.run is not None:
            try:
                self.run.finish()
            except Exception:
                pass
            print("[wandb] finished previous run; starting a new run for the new day",
                  flush=True)
        self.run = None
        self._init_failures = 0
        self._offline_mode = False
        self._upload_failures = 0
        self._log_enabled = True

    def shutdown(self):
        try:
            # Attempt one last init if we never connected.
            if self.run is None:
                self._try_init()
            if self.run is not None:
                self._flush_pending()
            current = self.recorder.current_outpath
            if current is not None:
                self._upload_file(current, final=True)
            if self.run is not None:
                self._flush_pending()
            with self._pending_lock:
                if self._pending:
                    print(f"[wandb] {len(self._pending)} file(s) NOT uploaded "
                          f"due to network. They remain on disk:",
                          file=sys.stderr, flush=True)
                    for path, _ in self._pending:
                        print(f"  - {path}", file=sys.stderr, flush=True)
        finally:
            if self.run is not None:
                try:
                    self.run.finish()
                except Exception:
                    pass


class Recorder:
    def __init__(self, args):
        self.args = args
        self.logger = None
        self.last_save_ts = 0.0
        self.interval = args.interval
        self.stop_event = threading.Event()
        self.frame_count = 0
        self.saved_count = 0
        self.uploader = None
        self.base_outpath = None
        self.current_outpath = None
        self.current_date = None        # YYYYMMDD of the active file
        self.lock = threading.Lock()

    @staticmethod
    def _today_str():
        return time.strftime("%Y%m%d")

    def _next_rotated_path(self):
        """Rotated files use log_<YYYYMMDD>.csv (today's date).
        If that name already exists, fall back to (2), (3), ..."""
        directory = os.path.dirname(self.base_outpath) or "."
        today = self._today_str()
        base = os.path.join(directory, f"log_{today}.csv")
        if not os.path.exists(base):
            return base
        idx = 2
        while True:
            candidate = os.path.join(directory, f"log_{today} ({idx}).csv")
            if not os.path.exists(candidate):
                return candidate
            idx += 1

    def rotate(self):
        """Close the current CSV, open the next rotated one, return the closed path."""
        with self.lock:
            if self.logger is None or self.current_outpath is None:
                return None
            closed = self.current_outpath
            self.logger.close()
            new_path = self._next_rotated_path()
            self.logger = CsvLogger(new_path, self.args.cols, self.args.rows)
            self.logger.open()
            self.current_outpath = new_path
            self.current_date = self._today_str()
            print(f"[rec] rotated -> {new_path}", flush=True)
            return closed

    def rotate_if_date_changed(self):
        """Rotate only when the calendar date has rolled over."""
        if self.current_date is None or self.current_date == self._today_str():
            return None
        return self.rotate()

    def on_frame(self, ts, frame):
        self.frame_count += 1
        if self.interval <= 0 or (ts - self.last_save_ts) >= self.interval:
            self.last_save_ts = ts
            with self.lock:
                try:
                    self.logger.write(ts, frame)
                    self.saved_count += 1
                except Exception as e:
                    print(f"[write error] {e}", file=sys.stderr)
            fmin = int(frame.min())
            fmax = int(frame.max())
            fmean = float(frame.mean())
            print(
                f"[{time.strftime('%H:%M:%S', time.localtime(ts))}] "
                f"saved #{self.saved_count}  "
                f"min={fmin:3d}  max={fmax:3d}  mean={fmean:6.2f}  "
                f"(fps≈{self.frame_count})",
                flush=True,
            )
            self.frame_count = 0
            if self.uploader is not None:
                try:
                    self.uploader.log_frame(ts, frame, self.saved_count)
                except Exception as e:
                    print(f"[wandb] log failed: {e}", file=sys.stderr, flush=True)

    def on_status(self, connected, msg):
        print(f"[serial] {msg}", flush=True)

    def run(self):
        args = self.args
        if not args.dry_run:
            if args.port == DEFAULT_PORT and not os.path.exists(DEFAULT_PORT):
                raise FileNotFoundError(
                    f"{DEFAULT_PORT} not found. Plug in the device or pass --port."
                )
            if not os.path.exists(args.port):
                raise FileNotFoundError(f"Serial port not found: {args.port}")

        outpath = resolve_outpath(args.outdir)
        try:
            header = bytes.fromhex(args.header.strip().replace(" ", ""))
        except ValueError as e:
            raise ValueError(f"Invalid --header hex: {args.header}") from e
        if not header:
            raise ValueError("--header must not be empty")

        self.base_outpath = outpath
        self.current_outpath = outpath
        self.current_date = self._today_str()
        self.logger = CsvLogger(outpath, args.cols, args.rows)
        self.logger.open()
        print(f"[rec] writing to {outpath}", flush=True)
        print(
            f"[cfg] port={args.port} baud={args.baud} "
            f"cols={args.cols} rows={args.rows} "
            f"header={args.header} "
            f"interval={self.interval}s",
            flush=True,
        )

        if args.upload:
            self.uploader = WandbUploader(args, outpath, self.stop_event, self)
            self.uploader.start()

        if args.dry_run:
            print(f"[dry-run] generating synthetic frames at {args.dry_fps} fps", flush=True)
            reader = FakeReader(args.cols, args.rows, args.dry_fps,
                                self.on_frame, on_status=self.on_status)
        else:
            parser = FrameParser(args.cols, args.rows, header, self.on_frame)
            reader = SerialReader(args.port, args.baud, parser, on_status=self.on_status)

        def handle_sig(signum, frame):
            self.stop_event.set()
        signal.signal(signal.SIGINT, handle_sig)
        signal.signal(signal.SIGTERM, handle_sig)

        reader.start()
        print("[rec] recording... press Ctrl+C to stop", flush=True)
        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(0.5)
        finally:
            reader.stop()
            # Close the CSV first so all buffered rows are flushed to disk,
            # then let the uploader push the finalized file as the final artifact.
            with self.lock:
                if self.logger is not None:
                    self.logger.close()
                    self.logger = None
            if self.uploader is not None:
                self.uploader.shutdown()
            print(f"[rec] stopped. saved {self.saved_count} frames "
                  f"-> {self.current_outpath}", flush=True)


def main(argv=None):
    args = build_parser().parse_args(argv)
    Recorder(args).run()


if __name__ == "__main__":
    main()

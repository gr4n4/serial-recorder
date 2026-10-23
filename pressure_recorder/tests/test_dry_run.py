"""Tests that exercise the recorder without a real serial device.

Covers:
  - dry-run subprocess actually writes a CSV
  - Recorder.rotate() creates suffixed files
  - WandbUploader queues failed uploads and retries (network drop simulation)
  - WandbUploader retries init() when offline at startup

Run with:
    python -m pytest tests/test_dry_run.py -v
"""
import csv
import importlib.util
import os
import subprocess
import sys
import threading
import time

import pytest  # noqa: F401  -- pytest collected as runner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# common/ lives at the repo root, one level above pressure_recorder/.
sys.path.insert(0, os.path.dirname(ROOT))
from common import config as _config  # noqa: E402

# `cmd` is a folder of scripts, not a Python package — load start.py by path.
_spec = importlib.util.spec_from_file_location(
    "cmd_start", os.path.join(ROOT, "cmd", "start.py")
)
_start_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_start_mod)
Recorder = _start_mod.Recorder
WandbUploader = _start_mod.WandbUploader
build_parser = _start_mod.build_parser


# ----------------------------- helpers -----------------------------

class FakeWandb:
    """Minimal stand-in for the wandb module. Configurable failure modes."""

    class _Run:
        def __init__(self):
            self.url = "fake://run"
            self.logged = []
            self.artifacts = []
            self.finished = False

        def log_artifact(self, art):
            if getattr(art, "_should_fail", False):
                raise RuntimeError("simulated artifact upload failure")
            self.artifacts.append(art)

        def finish(self):
            self.finished = True

    class Artifact:
        def __init__(self, name, type, metadata=None):
            self.name = name
            self.type = type
            self.metadata = metadata or {}
            self.files = []
            self._should_fail = False

        def add_file(self, path):
            self.files.append(path)

    def __init__(self, init_fails=0, log_artifact_fails=0):
        self._init_fails_remaining = init_fails
        self._log_artifact_fails_remaining = log_artifact_fails
        self.run = None
        self.log_calls = []

    def init(self, **kwargs):
        if self._init_fails_remaining > 0:
            self._init_fails_remaining -= 1
            raise RuntimeError("simulated wandb.init failure (offline)")
        self.run = FakeWandb._Run()
        return self.run

    def log(self, payload, step=None):
        self.log_calls.append((payload, step))


def make_args(tmp_path, **overrides):
    p = build_parser()
    args = p.parse_args(["--dry-run"])
    args.outdir = str(tmp_path)
    args.dry_fps = 30.0
    args.interval = 0.1
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


# ----------------------------- subprocess tests -----------------------------

def test_dry_run_writes_csv(tmp_path):
    """End-to-end: spawn the CLI in dry-run mode, kill it, verify CSV content."""
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "cmd", "start.py"),
         "--dry-run", "--outdir", str(tmp_path),
         "--interval", "0.05", "--dry-fps", "60"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        time.sleep(2.0)
    finally:
        proc.send_signal(subprocess.signal.SIGINT)
        proc.wait(timeout=10)

    csvs = list(tmp_path.glob("log_*.csv"))
    assert csvs, f"No log_*.csv created in {tmp_path}"
    out = csvs[0]
    with open(out) as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    assert rows[0][0] == "timestamp"
    assert len(rows) > 5, f"Too few rows: {len(rows)}"
    expected_cols = 1 + _config.DEFAULT_COLS * _config.DEFAULT_ROWS
    assert len(rows[1]) == expected_cols


# ----------------------------- unit tests -----------------------------

def test_rotation_uses_date_in_filename(tmp_path):
    """Rotated files are log_<YYYYMMDD>.csv, then (2), (3) on conflict."""
    import time as _t
    args = make_args(tmp_path, upload=False)
    rec = Recorder(args)

    # Bootstrap: open a first file with the original (timestamped) name.
    from sensor.csv_logger import CsvLogger
    first = str(tmp_path / "log_20260520_120000.csv")
    rec.base_outpath = first
    rec.current_outpath = first
    rec.current_date = "00000000"  # force a rotation trigger
    rec.logger = CsvLogger(first, args.cols, args.rows)
    rec.logger.open()

    today = _t.strftime("%Y%m%d")
    closed1 = rec.rotate()
    assert closed1 == first
    assert rec.current_outpath == str(tmp_path / f"log_{today}.csv")
    assert os.path.exists(rec.current_outpath)

    # Same-day second rotation -> (2)
    closed2 = rec.rotate()
    assert closed2 == str(tmp_path / f"log_{today}.csv")
    assert rec.current_outpath == str(tmp_path / f"log_{today} (2).csv")

    rec.logger.close()


def test_rotation_skips_existing_files(tmp_path):
    """If log_<today>.csv and (2) already exist, rotation should skip to (3)."""
    import time as _t
    today = _t.strftime("%Y%m%d")
    (tmp_path / f"log_{today}.csv").write_text("preexisting")
    (tmp_path / f"log_{today} (2).csv").write_text("also preexisting")

    args = make_args(tmp_path, upload=False)
    rec = Recorder(args)
    from sensor.csv_logger import CsvLogger
    first = str(tmp_path / "log_20260520_120000.csv")
    rec.base_outpath = first
    rec.current_outpath = first
    rec.current_date = "00000000"
    rec.logger = CsvLogger(first, args.cols, args.rows)
    rec.logger.open()

    rec.rotate()
    assert rec.current_outpath == str(tmp_path / f"log_{today} (3).csv")
    rec.logger.close()


def test_rotate_if_date_changed_noop_when_same_day(tmp_path):
    """rotate_if_date_changed() must not rotate when the date hasn't changed."""
    import time as _t
    args = make_args(tmp_path, upload=False)
    rec = Recorder(args)
    from sensor.csv_logger import CsvLogger
    first = str(tmp_path / "log_20260520_120000.csv")
    rec.base_outpath = first
    rec.current_outpath = first
    rec.current_date = _t.strftime("%Y%m%d")
    rec.logger = CsvLogger(first, args.cols, args.rows)
    rec.logger.open()

    assert rec.rotate_if_date_changed() is None
    assert rec.current_outpath == first
    rec.logger.close()


# ------------------------- wandb resilience tests -------------------------

def _make_uploader(tmp_path, fake_wandb, recorder, **arg_overrides):
    """Construct a WandbUploader with the wandb module replaced."""
    args = make_args(tmp_path, upload=True, upload_interval=0.5, **arg_overrides)
    outpath = str(tmp_path / "wbtest.csv")
    # Pre-create the file so artifact.add_file() doesn't fail.
    open(outpath, "w").close()

    stop_event = threading.Event()
    # Build uploader without doing real wandb init
    class _Stub(WandbUploader):
        def __init__(self_):
            self_.wandb = fake_wandb
            self_.args = args
            self_.base_name = "wbtest"
            self_.interval = args.upload_interval
            self_.stop_event = stop_event
            self_.recorder = recorder
            self_.run = None
            self_._wandb_config = {}
            self_._pending = []
            self_._pending_lock = threading.Lock()
            self_._init_failures = 0
            self_._log_failures = 0
            self_._upload_failures = 0
            self_._log_enabled = True
            self_._offline_mode = False
            self_._thread = None

    up = _Stub()
    return up, outpath, stop_event


class _DummyRecorder:
    def __init__(self, current_outpath):
        self.current_outpath = current_outpath

    def rotate(self):
        return self.current_outpath


def test_init_retries_when_offline_at_startup(tmp_path):
    """If wandb.init fails twice then succeeds, uploader recovers."""
    fake = FakeWandb(init_fails=2)
    rec = _DummyRecorder(str(tmp_path / "f.csv"))
    open(rec.current_outpath, "w").close()
    up, _, _ = _make_uploader(tmp_path, fake, rec)

    assert up._try_init() is False
    assert up._try_init() is False
    assert up._try_init() is True
    assert up.run is not None


def test_pending_queue_grows_on_upload_failure_then_drains(tmp_path):
    """Network drops → uploads queue. Network back → queue drains."""
    fake = FakeWandb(init_fails=0)
    rec = _DummyRecorder(str(tmp_path / "primary.csv"))
    open(rec.current_outpath, "w").close()
    up, _, _ = _make_uploader(tmp_path, fake, rec)
    up._try_init()
    assert up.run is not None

    # Simulate failures by making log_artifact raise.
    fail_flag = {"on": True}
    real_log = up.run.log_artifact

    def maybe_fail(art):
        if fail_flag["on"]:
            raise RuntimeError("net down")
        real_log(art)
    up.run.log_artifact = maybe_fail

    # Three failed uploads → all queued
    for i in range(3):
        path = str(tmp_path / f"q{i}.csv")
        open(path, "w").close()
        up._upload_file(path, final=False)
    assert len(up._pending) == 3

    # Network restored → flush should drain the queue
    fail_flag["on"] = False
    up._flush_pending()
    assert len(up._pending) == 0
    assert len(up.run.artifacts) == 3


def test_log_failure_is_swallowed(tmp_path):
    """A wandb.log exception must not propagate out of log_frame."""
    import numpy as np

    fake = FakeWandb(init_fails=0)
    rec = _DummyRecorder(str(tmp_path / "f.csv"))
    open(rec.current_outpath, "w").close()
    up, _, _ = _make_uploader(tmp_path, fake, rec)
    up._try_init()

    def boom(*a, **kw):
        raise RuntimeError("net down")
    fake.log = boom  # type: ignore[attr-defined]

    # Should not raise
    up.log_frame(time.time(), np.zeros((4, 4), dtype=np.uint8), saved_count=1)


def test_log_pauses_after_repeated_upload_failures(tmp_path):
    """If artifact uploads keep failing, wandb.log() should be paused
    (RAM guard) and resumed after the next successful upload."""
    import numpy as np

    fake = FakeWandb(init_fails=0)
    rec = _DummyRecorder(str(tmp_path / "f.csv"))
    open(rec.current_outpath, "w").close()
    up, _, _ = _make_uploader(tmp_path, fake, rec)
    up._try_init()

    fail_flag = {"on": True}
    real_log = up.run.log_artifact
    def maybe_fail(art):
        if fail_flag["on"]:
            raise RuntimeError("net down")
        real_log(art)
    up.run.log_artifact = maybe_fail

    log_calls = []
    def fake_log(payload, step=None):
        log_calls.append(payload)
    fake.log = fake_log  # type: ignore[attr-defined]

    # Trigger LOG_DISABLE_FAILURES (=3) consecutive failures
    for i in range(WandbUploader.LOG_DISABLE_FAILURES):
        p = str(tmp_path / f"x{i}.csv")
        open(p, "w").close()
        up._upload_file(p, final=False)
    assert up._log_enabled is False

    # log_frame should now be a no-op
    up.log_frame(time.time(), np.zeros((4, 4), dtype=np.uint8), saved_count=1)
    assert log_calls == []

    # Network restored + flush → log_enabled comes back
    fail_flag["on"] = False
    up._flush_pending()
    assert up._log_enabled is True

    up.log_frame(time.time(), np.zeros((4, 4), dtype=np.uint8), saved_count=2)
    assert len(log_calls) == 1


def test_offline_fallback_after_repeated_init_failures(tmp_path):
    """After --wandb-offline-after attempts, init switches to mode='offline'."""
    fake = FakeWandb(init_fails=100)
    rec = _DummyRecorder(str(tmp_path / "f.csv"))
    open(rec.current_outpath, "w").close()
    up, _, _ = _make_uploader(tmp_path, fake, rec, wandb_offline_after=3)

    captured_kwargs = {}
    original_init = fake.init
    def spy_init(**kwargs):
        captured_kwargs.update(kwargs)
        # Let the offline fallback succeed
        if kwargs.get("mode") == "offline":
            fake._init_fails_remaining = 0
        return original_init(**kwargs)
    fake.init = spy_init  # type: ignore[attr-defined]

    # 3 failed online attempts, 4th should fall back to offline
    for _ in range(3):
        assert up._try_init() is False
    assert up._try_init() is True
    assert captured_kwargs.get("mode") == "offline"
    assert up._offline_mode is True


def test_rotate_run_finishes_and_resets(tmp_path):
    """_rotate_run() must finish the current run and reset state so a fresh
    wandb run is initialized on the next attempt (new day -> new run)."""
    fake = FakeWandb(init_fails=0)
    rec = _DummyRecorder(str(tmp_path / "f.csv"))
    open(rec.current_outpath, "w").close()
    up, _, _ = _make_uploader(tmp_path, fake, rec)

    # Day 1: connect online and exercise the resilience state.
    up._try_init()
    day1_run = up.run
    assert day1_run is not None
    up._upload_failures = 5  # pretend things went wrong yesterday
    up._log_enabled = False
    up._offline_mode = True

    # Midnight rollover.
    up._rotate_run()
    assert day1_run.finished is True
    assert up.run is None
    assert up._init_failures == 0
    assert up._offline_mode is False
    assert up._upload_failures == 0
    assert up._log_enabled is True

    # Day 2: a brand-new run is created.
    up._try_init()
    day2_run = up.run
    assert day2_run is not None
    assert day2_run is not day1_run


def test_rotate_run_no_op_when_never_connected(tmp_path):
    """If wandb never connected, _rotate_run() just resets the retry counters
    (no run.finish call to fail on)."""
    fake = FakeWandb(init_fails=100)
    rec = _DummyRecorder(str(tmp_path / "f.csv"))
    open(rec.current_outpath, "w").close()
    up, _, _ = _make_uploader(tmp_path, fake, rec, wandb_offline_after=999)

    # Several failed online inits accumulate.
    for _ in range(5):
        up._try_init()
    assert up._init_failures == 5
    assert up.run is None

    up._rotate_run()
    assert up._init_failures == 0  # ready to retry online from scratch
    assert up.run is None


def test_shutdown_reports_undelivered_files(tmp_path, capsys):
    """If files are still pending at shutdown, their paths are printed."""
    fake = FakeWandb(init_fails=0)
    rec = _DummyRecorder(str(tmp_path / "current.csv"))
    open(rec.current_outpath, "w").close()
    up, _, _ = _make_uploader(tmp_path, fake, rec)
    up._try_init()

    # Force every upload attempt to fail (including final)
    def always_fail(art):
        raise RuntimeError("net never came back")
    up.run.log_artifact = always_fail

    leftover = str(tmp_path / "leftover.csv")
    open(leftover, "w").close()
    up._enqueue(leftover, final=False)

    up.shutdown()
    captured = capsys.readouterr()
    assert "NOT uploaded" in captured.err
    assert leftover in captured.err

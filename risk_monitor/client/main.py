"""Headless pressure-risk monitoring client for Raspberry Pi 5.

Reads pressure frames over serial (same protocol as sensor recorder),
accumulates per-cell risk over real elapsed time, and reports cells that
stay risky past `critical_time` to a server, subject to a per-cell alert
cooldown. Each warning is accompanied by a PNG of the current frame (not
an accumulated one) with the risky cells overlaid, POSTed to /image. The
three risk thresholds (calibration_factor, critical_pressure,
critical_time) are polled periodically from the server, as are pending
monitor commands (start/pause/stop/reset/state).

Every warning is also persisted locally regardless of server
reachability (see client/warning_log.py): the warning payload and a copy
of the sent image go to <warning-log-dir>, and every event/state/image
send attempt's success/failure is logged to send.log.

Usage:
    python -m client.main [--port /dev/ttyUSB0] [--baud 921600]
                           [--cols 32] [--rows 64] [--header A55A]
                           [--pre 6] [--post 2]
                           [--server-url http://localhost:5000]
                           [--poll-interval 60] [--command-poll-interval 2]
                           [--http-timeout 5] [--alert-cooldown 300]
                           [--warning-log-dir logs/client_warnings]
                           [--dry-run] [--dry-fps 30]
"""
import argparse
import logging
import logging.handlers
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_APP_ROOT)
for _p in (_APP_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

from common.frame_parser import FrameParser  # noqa: E402
from common.serial_reader import SerialReader  # noqa: E402

from client.config import ClientConfig, RuntimeConfig  # noqa: E402
from client.http_client import (  # noqa: E402
    CommandPoller, ConfigPoller, EventSender, ImageSender, StateSender,
)
from client.image import render_risk_image  # noqa: E402
from client.monitor import MonitorState  # noqa: E402
from client.risk import RiskAccumulator  # noqa: E402
from client.warning_log import WarningLog  # noqa: E402

DEFAULT_PORT = "/dev/ttyUSB0"

logger = logging.getLogger("client.main")

_APP_LOG_HANDLER_MARKER = "_pressure_app_log_handler"
APP_LOG_BACKUP_DAYS = 14


def configure_app_log_file(warning_log_dir):
    """Routes every "client.*" logger (main.py, warning_log.py, ...) into
    <warning_log_dir>/app.log -- a running record of serial connect/
    disconnect, monitor start/pause/stop/reset, and config-poll/command-
    poll/event/state/image send status, independent of warnings.log
    (which only holds risk-warning events).

    Unlike the server (a Windows desktop app, restarted often), this
    client runs unattended 24/7 on a Raspberry Pi with limited SD-card
    space, so app.log rotates at midnight and keeps only the last
    APP_LOG_BACKUP_DAYS days instead of growing forever."""
    root = logging.getLogger("client")
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        if getattr(h, _APP_LOG_HANDLER_MARKER, False):
            root.removeHandler(h)
            h.close()
    log_path = Path(warning_log_dir) / "app.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=APP_LOG_BACKUP_DAYS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    setattr(handler, _APP_LOG_HANDLER_MARKER, True)
    root.addHandler(handler)


def build_parser():
    p = argparse.ArgumentParser(prog="client.main", description="Pressure risk monitoring client")
    p.add_argument("--client-id", default="",
                    help="identifies this client to the server (default: this machine's hostname)")
    p.add_argument("--port", default=DEFAULT_PORT, help=f"Serial port (default: {DEFAULT_PORT})")
    p.add_argument("--baud", type=int, default=ClientConfig.baud)
    p.add_argument("--cols", type=int, default=ClientConfig.cols)
    p.add_argument("--rows", type=int, default=ClientConfig.rows)
    p.add_argument("--header", default=ClientConfig.header_hex, help="hex string, e.g. A55A")
    p.add_argument("--pre", type=int, default=ClientConfig.pre_skip)
    p.add_argument("--post", type=int, default=ClientConfig.post_skip)
    p.add_argument("--server-url", default=ClientConfig.server_base_url)
    p.add_argument("--config-path", default=ClientConfig.config_path)
    p.add_argument("--event-path", default=ClientConfig.event_path)
    p.add_argument("--event-clear-path", default=ClientConfig.event_clear_path)
    p.add_argument("--image-path", default=ClientConfig.image_path)
    p.add_argument("--warning-log-dir", default=ClientConfig.warning_log_dir,
                    help="directory to log warnings.log, send.log, and images/ into")
    p.add_argument("--command-path", default=ClientConfig.command_path)
    p.add_argument("--state-path", default=ClientConfig.state_path)
    p.add_argument("--poll-interval", type=float, default=ClientConfig.poll_interval_s)
    p.add_argument("--command-poll-interval", type=float,
                    default=ClientConfig.command_poll_interval_s)
    p.add_argument("--http-timeout", type=float, default=ClientConfig.http_timeout_s)
    p.add_argument("--http-retry-delay", type=float, default=ClientConfig.http_retry_delay_s)
    p.add_argument("--alert-cooldown", type=float, default=ClientConfig.alert_cooldown_s)
    p.add_argument("--pressure-mask-threshold", type=int,
                    default=ClientConfig.pressure_mask_threshold)
    p.add_argument("--dry-run", action="store_true",
                    help="generate synthetic frames instead of reading the serial port")
    p.add_argument("--dry-fps", type=float, default=30.0)
    return p


class FakeReader:
    """Synthetic frame producer for --dry-run. Mimics SerialReader.start/stop.
    A small hot-spot region is always saturated so --dry-run exercises the
    full risk -> alert path without needing real hardware."""

    def __init__(self, cols, rows, fps, on_frame, on_status=None):
        self.cols = cols
        self.rows = rows
        self.period = 1.0 / max(fps, 1e-3)
        self.on_frame = on_frame
        self.on_status = on_status or (lambda connected, msg: None)
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
        while not self._stop.is_set():
            frame = np.random.randint(0, 40, size=(self.rows, self.cols), dtype=np.uint8)
            frame[0:2, 0:2] = 255  # persistent hot spot
            try:
                self.on_frame(time.time(), frame)
            except Exception:
                pass
            if self._stop.wait(self.period):
                break
        self.on_status(False, "Dry-run: stopped")


class LatestFrame:
    """Thread-safe holder for the most recently received (timestamp,
    pressure_vector) pair, used to answer `state` commands regardless of
    whether risk accumulation is currently active."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ts = None
        self._pressure_vector = None

    def set(self, ts, pressure_vector):
        with self._lock:
            self._ts = ts
            self._pressure_vector = pressure_vector

    def get(self):
        with self._lock:
            return self._ts, self._pressure_vector


class ClientApp:
    def __init__(self, args):
        self.args = args
        self.stop_event = threading.Event()
        n_cells = args.cols * args.rows

        configure_app_log_file(args.warning_log_dir)
        self.runtime_config = RuntimeConfig()
        self.warning_log = WarningLog(args.warning_log_dir)
        self.risk_acc = RiskAccumulator(n_cells, alert_cooldown_s=args.alert_cooldown)
        self.monitor = MonitorState(self.risk_acc)
        self.latest_frame = LatestFrame()

        client_id = args.client_id or socket.gethostname()
        self.client_id = client_id

        self.poller = ConfigPoller(
            args.server_url, args.config_path, args.poll_interval,
            self.runtime_config, timeout_s=args.http_timeout, n_cells=n_cells,
            on_status=self.on_poll_status, client_id=client_id,
        )
        self.command_poller = CommandPoller(
            args.server_url, args.command_path, args.command_poll_interval,
            self.on_command, timeout_s=args.http_timeout, on_status=self.on_command_status,
            client_id=client_id,
        )
        self.sender = EventSender(
            args.server_url, args.event_path, timeout_s=args.http_timeout,
            retry_delay_s=args.http_retry_delay, on_status=self.on_send_status,
            client_id=client_id,
        )
        self.clear_sender = EventSender(
            args.server_url, args.event_clear_path, timeout_s=args.http_timeout,
            retry_delay_s=args.http_retry_delay, on_status=self.on_clear_send_status,
            client_id=client_id,
        )
        self.image_sender = ImageSender(
            args.server_url, args.image_path, timeout_s=args.http_timeout,
            retry_delay_s=args.http_retry_delay, on_status=self.on_image_send_status,
            client_id=client_id,
        )
        self.state_sender = StateSender(
            args.server_url, args.state_path, timeout_s=args.http_timeout,
            retry_delay_s=args.http_retry_delay, on_status=self.on_state_send_status,
            client_id=client_id,
        )

        if args.dry_run:
            self.reader = FakeReader(args.cols, args.rows, args.dry_fps,
                                      self.on_frame, on_status=self.on_serial_status)
        else:
            header = bytes.fromhex(args.header.strip().replace(" ", ""))
            parser = FrameParser(args.cols, args.rows, header, args.pre, args.post, self.on_frame)
            self.reader = SerialReader(args.port, args.baud, parser, on_status=self.on_serial_status)

    def on_frame(self, ts, frame):
        pressure_vector = frame.reshape(-1)
        self.latest_frame.set(ts, pressure_vector)

        if not self.monitor.is_active():
            return

        calib, crit_p, crit_t, detection_mask = self.runtime_config.get()
        fired_idx, _risk_mask, cleared_idx = self.risk_acc.update(
            pressure_vector, calib, crit_p, crit_t, detection_mask=detection_mask
        )

        if cleared_idx.size:
            clear_payload = {"cleared_idx": cleared_idx.tolist()}
            self.warning_log.log_warning(clear_payload)
            self.clear_sender.enqueue(clear_payload)

        if fired_idx.size == 0:
            return
        pressure_mask_idx = np.nonzero(
            pressure_vector > self.args.pressure_mask_threshold
        )[0]
        event_payload = {
            "accumulated_time": crit_t,
            "risky_idx": fired_idx.tolist(),
            "risky_pressure": pressure_vector[fired_idx].tolist(),
            "pressure_mask_idx": pressure_mask_idx.tolist(),
        }
        # Logged immediately, independent of whether the send below
        # succeeds or the server is even reachable.
        self.warning_log.log_warning(event_payload)
        self.sender.enqueue(event_payload)

        # Current frame + risk-point overlay, not an accumulated image.
        image_bytes = render_risk_image(pressure_vector, self.args.rows, self.args.cols, fired_idx)
        self.warning_log.save_image(image_bytes, ts)
        self.image_sender.enqueue({"image": image_bytes, "timestamp": ts})

    def on_command(self, command):
        if command == "state":
            ts, pressure_vector = self.latest_frame.get()
            self.state_sender.enqueue({
                "timestamp": ts,
                "pressure": pressure_vector.tolist() if pressure_vector is not None else [],
            })
            return
        try:
            new_state = self.monitor.apply(command)
        except ValueError as e:
            logger.info("[monitor] %s", e)
            return
        logger.info("[monitor] %s -> %s", command, new_state)

    def on_serial_status(self, connected, msg):
        logger.info("[serial] %s", msg)

    def on_poll_status(self, ok, msg):
        logger.info("[config] %s", msg)

    def on_command_status(self, ok, msg):
        logger.info("[command] %s", msg)

    def on_send_status(self, ok, msg):
        logger.info("[event] %s", msg)
        self.warning_log.log_send("event", ok, msg)

    def on_clear_send_status(self, ok, msg):
        logger.info("[event_clear] %s", msg)
        self.warning_log.log_send("event_clear", ok, msg)

    def on_state_send_status(self, ok, msg):
        logger.info("[state] %s", msg)
        self.warning_log.log_send("state", ok, msg)

    def on_image_send_status(self, ok, msg):
        logger.info("[image] %s", msg)
        self.warning_log.log_send("image", ok, msg)

    def run(self):
        def handle_sig(signum, frame):
            self.stop_event.set()
        signal.signal(signal.SIGINT, handle_sig)
        signal.signal(signal.SIGTERM, handle_sig)

        self.reader.start()
        self.poller.start()
        self.command_poller.start()
        self.sender.start()
        self.clear_sender.start()
        self.state_sender.start()
        self.image_sender.start()
        print("[client] running... press Ctrl+C to stop", flush=True)
        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(0.5)
        finally:
            self.reader.stop()
            self.poller.stop()
            self.command_poller.stop()
            self.sender.stop()
            self.clear_sender.stop()
            self.state_sender.stop()
            self.image_sender.stop()
            print("[client] stopped", flush=True)


# 라즈베리파이 OS 를 갓 설치했을 때의 호스트네임.
# SD 카드를 복제해 여러 대를 만들면 전부 이 이름이 된다.
STOCK_HOSTNAMES = {"raspberrypi", "raspberry", "localhost"}


def resolve_client_id(args):
    """서버에 자기를 뭐라고 알릴지 정한다.

    client_id 가 겹치면 서버는 그 둘을 **같은 기기 하나**로 본다. 침대마다
    라즈베리파이를 두는데 SD 카드를 복제해 만들면 전부 'raspberrypi' 가
    되어, 침대 네 개가 조용히 감시에서 빠진다. 대시보드에는 한 대가 멀쩡히
    붙어 있는 것으로 보이므로 알아채기도 어렵다.

    그래서 갓 설치한 이름 그대로면 시작하지 않는다. 고치는 데 몇 초면 되는
    일이고, 놓치면 보고 있어야 할 침대를 아무도 안 보게 된다."""
    if args.client_id:
        return args.client_id

    hostname = socket.gethostname()
    if hostname.strip().lower() in STOCK_HOSTNAMES:
        raise SystemExit(
            # 안내문에는 - 와 · 만 쓴다. em dash 같은 글자는 한국어 윈도우
            # 콘솔(cp949)에서 인코딩에 걸려, 정작 읽어야 할 안내가 안 뜬다.
            f"\n이 기기의 이름이 '{hostname}' 입니다. 라즈베리파이 기본값이라\n"
            "그대로 두면 다른 기기와 겹칩니다. 겹치면 서버가 둘을 한 대로 묶어서,\n"
            "침대 하나가 감시에서 조용히 빠집니다.\n"
            "\n"
            "둘 중 하나로 이름을 정해 주세요.\n"
            f"  이번만    : python -m client.main --client-id 421호 --port {args.port}\n"
            "  기기 이름 : sudo hostnamectl set-hostname pi-421   (재부팅 뒤 적용)\n"
        )
    return hostname


def main(argv=None):
    args = build_parser().parse_args(argv)
    # 시리얼 포트를 열기 전에 막는다. 한참 돌다가 죽으면 원인을 찾기 어렵다.
    args.client_id = resolve_client_id(args)
    ClientApp(args).run()


if __name__ == "__main__":
    main()

"""HTTP REST communication with the server: config polling and event
reporting. Both run on their own background threads so neither blocks the
serial frame-processing callback."""
import math
import queue
import threading
import time

import requests

from client.config import build_detection_mask


class ConfigPoller:
    """Periodically GETs the server config and pushes it into RuntimeConfig.

    Failures are swallowed and simply retried on the next cycle -- the
    polling loop itself is the retry mechanism, matching SerialReader's
    reconnect-on-failure pattern.
    """

    def __init__(self, base_url, config_path, interval_s, runtime_config,
                 timeout_s=5.0, n_cells=None, on_status=None, session=None,
                 client_id=""):
        self.url = base_url.rstrip("/") + config_path
        self.interval_s = interval_s
        self.runtime_config = runtime_config
        self.n_cells = n_cells
        self.timeout_s = timeout_s
        self.on_status = on_status or (lambda ok, msg: None)
        self.session = session or requests.Session()
        self.client_id = client_id
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

    def poll_once(self):
        resp = self.session.get(
            self.url, params={"client_id": self.client_id}, timeout=self.timeout_s
        )
        resp.raise_for_status()
        data = resp.json()
        calibration_factor = float(data["calibration_factor"])
        critical_pressure = float(data["critical_pressure"])
        critical_time = float(data["critical_time"])
        # Keep the previous thresholds rather than adopt values that would
        # break (or silently disable) detection on every frame.
        if not (math.isfinite(calibration_factor) and calibration_factor > 0):
            raise ValueError(f"invalid calibration_factor: {calibration_factor}")
        if not (math.isfinite(critical_pressure) and critical_pressure >= 0):
            raise ValueError(f"invalid critical_pressure: {critical_pressure}")
        if not (math.isfinite(critical_time) and critical_time > 0):
            raise ValueError(f"invalid critical_time: {critical_time}")
        detection_mask = None
        if self.n_cells is not None:
            detection_mask = build_detection_mask(
                self.n_cells, data.get("mask_excluded_idx", [])
            )
        self.runtime_config.update(
            calibration_factor, critical_pressure, critical_time, detection_mask,
        )
        self.on_status(True, f"config updated: {_summarize(data)}")

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:
                self.on_status(False, f"config poll failed: {e}")
            if self._stop.wait(self.interval_s):
                break


class CommandPoller:
    """Periodically GETs a pending monitor command (start/pause/stop/reset/
    state) and invokes `on_command(command)`. Polls on its own short
    interval, separate from ConfigPoller, since commands need to take
    effect promptly.

    Expected response shape: {"command": "start"} or {"command": null}
    when nothing is pending. Any falsy/absent command is treated as a
    no-op tick.
    """

    def __init__(self, base_url, command_path, interval_s, on_command,
                 timeout_s=5.0, on_status=None, session=None, client_id=""):
        self.url = base_url.rstrip("/") + command_path
        self.interval_s = interval_s
        self.on_command = on_command
        self.timeout_s = timeout_s
        self.on_status = on_status or (lambda ok, msg: None)
        self.session = session or requests.Session()
        self.client_id = client_id
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

    def poll_once(self):
        resp = self.session.get(
            self.url, params={"client_id": self.client_id}, timeout=self.timeout_s
        )
        resp.raise_for_status()
        data = resp.json()
        command = data.get("command")
        if not command:
            return
        self.on_status(True, f"command received: {command}")
        self.on_command(command)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:
                self.on_status(False, f"command poll failed: {e}")
            if self._stop.wait(self.interval_s):
                break


def _summarize(payload):
    """Compact description of a payload for the logs: long lists (e.g. the
    2048-value pressure frame of a state report) are replaced by their
    length so app.log/send.log don't grow by ~10 KB per send on the Pi's
    SD card."""
    parts = []
    for key, value in payload.items():
        if isinstance(value, (list, tuple)) and len(value) > 16:
            parts.append(f"{key}=<{len(value)} items>")
        else:
            parts.append(f"{key}={value!r}")
    return "{" + ", ".join(parts) + "}"


class _QueuedPoster:
    """Base class for queue + worker-thread POSTers. Payloads are enqueued
    from whatever thread produces them (frame callback, command poller) and
    sent from a dedicated worker thread so the caller is never blocked by
    network I/O.

    If the queue is full, the oldest pending payload is dropped in favor of
    the new one -- reporting must never back up onto the producer.
    """

    MAX_ATTEMPTS = 3
    QUEUE_FULL_MSG = "event queue full; dropped oldest pending event"

    def __init__(self, url, timeout_s=5.0, retry_delay_s=5.0,
                 maxsize=64, on_status=None, session=None, client_id=""):
        self.url = url
        self.timeout_s = timeout_s
        self.retry_delay_s = retry_delay_s
        self.on_status = on_status or (lambda ok, msg: None)
        self.session = session or requests.Session()
        self.client_id = client_id
        self._queue = queue.Queue(maxsize=maxsize)
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

    def enqueue(self, payload):
        payload = dict(payload, client_id=self.client_id)
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                pass
            self.on_status(False, self.QUEUE_FULL_MSG)

    def _send(self, payload):
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                resp = self.session.post(self.url, json=payload, timeout=self.timeout_s)
                resp.raise_for_status()
                self.on_status(True, f"event sent: {_summarize(payload)}")
                return
            except Exception as e:
                if attempt >= self.MAX_ATTEMPTS:
                    self.on_status(False, f"event send failed, dropping: {e}")
                    return
                self.on_status(False, f"event send failed (attempt {attempt}): {e}")
                if self._stop.wait(self.retry_delay_s):
                    return

    def _run(self):
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._send(payload)


class EventSender(_QueuedPoster):
    def __init__(self, base_url, event_path, timeout_s=5.0, retry_delay_s=5.0,
                 maxsize=64, on_status=None, session=None, client_id=""):
        super().__init__(base_url.rstrip("/") + event_path, timeout_s=timeout_s,
                          retry_delay_s=retry_delay_s, maxsize=maxsize,
                          on_status=on_status, session=session, client_id=client_id)


class StateSender(_QueuedPoster):
    """POSTs the latest pressure snapshot to /state in response to a
    `state` command. Only the most recent snapshot matters, so a small
    queue (maxsize=1) is enough -- an older pending state report is
    superseded by a newer one."""

    def __init__(self, base_url, state_path, timeout_s=5.0, retry_delay_s=5.0,
                 maxsize=1, on_status=None, session=None, client_id=""):
        super().__init__(base_url.rstrip("/") + state_path, timeout_s=timeout_s,
                          retry_delay_s=retry_delay_s, maxsize=maxsize,
                          on_status=on_status, session=session, client_id=client_id)


class ImageSender(_QueuedPoster):
    """POSTs a risk-warning snapshot PNG (multipart/form-data, field name
    "image") whenever a risk warning fires. A small queue is enough --
    images are large and only the most recent unsent one is worth keeping."""

    QUEUE_FULL_MSG = "image queue full; dropped oldest pending image"

    def __init__(self, base_url, image_path, timeout_s=5.0, retry_delay_s=5.0,
                 maxsize=4, on_status=None, session=None, client_id="", prepare=None):
        """prepare: optional callable(payload) -> payload, run on the worker
        thread before sending -- lets the caller enqueue the raw frame and
        have the (slow) PNG rendering/disk write happen here instead of in
        the serial frame callback."""
        super().__init__(base_url.rstrip("/") + image_path, timeout_s=timeout_s,
                          retry_delay_s=retry_delay_s, maxsize=maxsize,
                          on_status=on_status, session=session, client_id=client_id)
        self.prepare = prepare

    def _send(self, payload):
        if self.prepare is not None:
            try:
                payload = self.prepare(payload)
            except Exception as e:
                self.on_status(False, f"image prepare failed, dropping: {e}")
                return
        image_bytes = payload["image"]
        fields = {k: str(v) for k, v in payload.items() if k != "image"}
        files = {"image": ("risk.png", image_bytes, "image/png")}
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                resp = self.session.post(
                    self.url, data=fields, files=files, timeout=self.timeout_s
                )
                resp.raise_for_status()
                self.on_status(True, f"image sent ({len(image_bytes)} bytes)")
                return
            except Exception as e:
                if attempt >= self.MAX_ATTEMPTS:
                    self.on_status(False, f"image send failed, dropping: {e}")
                    return
                self.on_status(False, f"image send failed (attempt {attempt}): {e}")
                if self._stop.wait(self.retry_delay_s):
                    return

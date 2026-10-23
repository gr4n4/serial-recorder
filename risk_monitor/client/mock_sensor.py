"""Streams recorded pressure frames onto a virtual serial device, so
`client/main.py --port ...` (or the sensor recorder GUI) can be exercised
without real hardware attached.

Data source: a recorded CSV in the CsvLogger wide format
(`timestamp, MAT_0, MAT_1, ..., MAT_{cols*rows-1}` — one row per frame,
row-major flatten), e.g. `client/dumpy_data/Calib31.CSV`. Each run picks a
random starting row and streams sequentially from there (wrapping around),
so repeated runs exercise different playback windows of the recording.

With --warning, playback instead repeats that one randomly chosen row
forever -- a fixed, unchanging pressure pattern -- so a client watching
this stream sees the same cells stay over threshold indefinitely and its
accumulated_risk climbs past critical_time, producing a real risk-warning
POST /event to the server (useful for exercising the dashboard).

Requires `socat` on PATH. It creates a pty pair; one end is symlinked at
--link (default /dev/ttyUSB0, which on most systems requires root/sudo to
create) for the consumer to open, and this script writes raw sensor
packets to the other end.

Usage:
    python -m client.mock_sensor [--csv client/dumpy_data/Calib31.CSV]
                                  [--link /dev/ttyUSB0] [--baud 921600]
                                  [--cols 32] [--rows 64]
                                  [--header A55A01060801]
                                  [--fps 30] [--seed 1234]
"""
import argparse
import csv
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np
import serial

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.config import DEFAULT_COLS, DEFAULT_HEADER_HEX, DEFAULT_ROWS  # noqa: E402

DEFAULT_CSV = os.path.join(os.path.dirname(__file__), "dumpy_data", "Calib31.CSV")


def load_frames(csv_path, n_cells):
    """Read a recorded CSV (timestamp, MAT_0..MAT_{n_cells-1}) and return an
    (n_frames, n_cells) uint8 array, row-major (matches FrameParser's
    reshape(rows, cols).flatten() layout)."""
    frames = []
    with open(csv_path, newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        if len(header) - 1 != n_cells:
            raise ValueError(
                f"CSV has {len(header) - 1} MAT columns, expected {n_cells} "
                f"(cols*rows={n_cells}). Check --cols/--rows."
            )
        for row in reader:
            if not row:
                continue
            values = np.clip(np.array(row[1:], dtype=np.float64), 0, 255).astype(np.uint8)
            frames.append(values)
    if not frames:
        raise ValueError(f"No data rows found in {csv_path}")
    return np.stack(frames)


def next_row_index(start_row, sent, n_frames, warning):
    """Which CSV row to send for the `sent`-th packet. Normal mode advances
    sequentially from start_row (wrapping); --warning mode pins playback to
    start_row forever."""
    return start_row if warning else (start_row + sent) % n_frames


def build_packet(header, payload):
    """header: bytes-like, payload: uint8 ndarray. Packet = header + payload
    + 2-byte checksum (little-endian 16-bit sum of every preceding byte), as
    FrameParser expects."""
    body = bytes(header) + payload.tobytes()
    return body + (sum(body) & 0xFFFF).to_bytes(2, "little")


class SocatPtyBridge:
    """Spawns `socat` to create a pty pair; one end is symlinked at `link`
    for a consumer (SerialReader) to open, the other end (`own_link`) is
    where this script writes frames."""

    def __init__(self, link, own_link=None):
        self.link = link
        self.own_link = own_link or tempfile.mktemp(prefix="mock_sensor_")
        self._proc = None

    def start(self, timeout=5.0):
        for path in (self.link, self.own_link):
            if os.path.islink(path) or os.path.exists(path):
                os.remove(path)
        self._proc = subprocess.Popen(
            ["socat", "-d", "-d",
             f"pty,raw,echo=0,link={self.own_link}",
             f"pty,raw,echo=0,link={self.link}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.link) and os.path.exists(self.own_link):
                return
            if self._proc.poll() is not None:
                out = self._proc.stdout.read() if self._proc.stdout else ""
                raise RuntimeError(
                    f"socat exited before creating {self.link} (check permissions -- "
                    f"creating a symlink under /dev usually requires sudo): {out}"
                )
            time.sleep(0.05)
        self.stop()
        raise TimeoutError(f"socat did not create {self.link} within {timeout}s")

    def stop(self):
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        for path in (self.own_link, self.link):
            try:
                if os.path.islink(path):
                    os.unlink(path)
            except OSError:
                pass


def build_parser():
    p = argparse.ArgumentParser(
        prog="client.mock_sensor",
        description="Stream a recorded pressure CSV onto a virtual serial device",
    )
    p.add_argument("--csv", default=DEFAULT_CSV,
                    help=f"recorded frames CSV (default: {DEFAULT_CSV})")
    p.add_argument("--link", default="/dev/ttyUSB0",
                    help="device path the consumer (SerialReader) will open")
    p.add_argument("--baud", type=int, default=921600)
    p.add_argument("--cols", type=int, default=DEFAULT_COLS)
    p.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    p.add_argument("--header", default=DEFAULT_HEADER_HEX, help="hex string, e.g. A55A01060801")
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--seed", type=int, default=None,
                    help="random seed for the starting row (default: OS entropy)")
    p.add_argument("--warning", action="store_true",
                    help="repeat the randomly chosen starting row forever instead of "
                         "advancing through the recording, simulating a persistent "
                         "risk (accumulated_risk climbs until a warning fires)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    n_cells = args.cols * args.rows
    header = bytes.fromhex(args.header.strip().replace(" ", ""))

    print(f"[mock_sensor] loading {args.csv} ...", flush=True)
    frames = load_frames(args.csv, n_cells)
    n_frames = frames.shape[0]
    print(f"[mock_sensor] loaded {n_frames} frames ({n_cells} cells each)", flush=True)

    rng = np.random.default_rng(args.seed)
    start_row = int(rng.integers(0, n_frames))
    if args.warning:
        print(f"[mock_sensor] WARNING mode: repeating row {start_row}/{n_frames} forever",
              flush=True)
    else:
        print(f"[mock_sensor] starting playback at row {start_row}/{n_frames}", flush=True)

    bridge = SocatPtyBridge(args.link)
    bridge.start()
    print(f"[mock_sensor] {args.link} ready (backed by {bridge.own_link})", flush=True)

    stop_event = threading.Event()

    def handle_sig(signum, frame):
        stop_event.set()
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    ser = serial.Serial(bridge.own_link, args.baud, timeout=0)
    period = 1.0 / max(args.fps, 1e-3)
    sent = 0
    try:
        while not stop_event.is_set():
            row = next_row_index(start_row, sent, n_frames, args.warning)
            frame = frames[row]
            packet = build_packet(header, frame)
            ser.write(packet)
            sent += 1
            if stop_event.wait(period):
                break
    finally:
        ser.close()
        bridge.stop()
        print(f"[mock_sensor] stopped after sending {sent} frames", flush=True)


if __name__ == "__main__":
    main()

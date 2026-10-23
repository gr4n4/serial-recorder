"""Capture a baseline frame for calibration.

Connects to the sensor, averages N frames, and saves the result as
    ~/sensor_logs/baseline_<timestamp>.csv

Usage:
    python cmd/calibration.py [--port /dev/ttyUSB0] [--samples 10]
                              [--baud 921600] [--cols 32] [--rows 64]
                              [--header A55A01060801]
                              [--outpath baseline_name]
"""
import argparse
import os
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
    p = argparse.ArgumentParser(prog="calibration",
                                description="Capture baseline frame for Sensor Recorder")
    p.add_argument("--port", default=DEFAULT_PORT)
    p.add_argument("--baud", type=int, default=config.DEFAULT_BAUD)
    p.add_argument("--cols", type=int, default=config.DEFAULT_COLS)
    p.add_argument("--rows", type=int, default=config.DEFAULT_ROWS)
    p.add_argument("--header", default=config.DEFAULT_HEADER_HEX)
    p.add_argument("--samples", type=int, default=10,
                   help="number of frames to average (default 10)")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="seconds to wait for frames (default 10)")
    p.add_argument("--outpath", default=None,
                   help=f"output path (default: {DEFAULT_OUT_DIR}/baseline_<timestamp>.csv)")
    return p


def resolve_outpath(raw):
    if raw is None:
        fname = time.strftime("baseline_%Y%m%d_%H%M%S.csv")
        return os.path.join(DEFAULT_OUT_DIR, fname)
    path = os.path.expanduser(raw)
    root, ext = os.path.splitext(path)
    if not ext:
        path = root + ".csv"
    if not os.path.isabs(path):
        path = os.path.join(DEFAULT_OUT_DIR, path)
    return path


class Collector:
    def __init__(self, target):
        self.target = target
        self.frames = []
        self.done = threading.Event()
        self.lock = threading.Lock()

    def on_frame(self, ts, frame):
        with self.lock:
            if len(self.frames) < self.target:
                self.frames.append(frame.copy())
                n = len(self.frames)
                print(f"  captured {n}/{self.target}  "
                      f"min={int(frame.min())} max={int(frame.max())} "
                      f"mean={frame.mean():.2f}", flush=True)
                if n >= self.target:
                    self.done.set()

    def on_status(self, connected, msg):
        print(f"[serial] {msg}", flush=True)


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.port == DEFAULT_PORT and not os.path.exists(DEFAULT_PORT):
        raise FileNotFoundError(f"{DEFAULT_PORT} not found. Plug in the device or pass --port.")
    if not os.path.exists(args.port):
        raise FileNotFoundError(f"Serial port not found: {args.port}")

    header = bytes.fromhex(args.header.strip().replace(" ", ""))
    if not header:
        raise ValueError("--header must not be empty")

    outpath = resolve_outpath(args.outpath)
    print(f"[cal] target: {args.samples} frames averaged")
    print(f"[cfg] port={args.port} baud={args.baud} "
          f"cols={args.cols} rows={args.rows}")

    collector = Collector(args.samples)
    parser = FrameParser(args.cols, args.rows, header, collector.on_frame)
    reader = SerialReader(args.port, args.baud, parser, on_status=collector.on_status)
    reader.start()
    try:
        if not collector.done.wait(args.timeout):
            raise TimeoutError(
                f"Only captured {len(collector.frames)}/{args.samples} frames "
                f"within {args.timeout}s. Check sensor connection."
            )
    finally:
        reader.stop()

    stack = np.stack(collector.frames).astype(np.float32)
    baseline = np.clip(np.round(stack.mean(axis=0)), 0, 255).astype(np.uint8)
    print(f"[cal] averaged {len(collector.frames)} frames  "
          f"min={int(baseline.min())} max={int(baseline.max())} "
          f"mean={baseline.mean():.2f}")

    logger = CsvLogger(outpath, args.cols, args.rows)
    logger.open()
    logger.write(time.time(), baseline)
    logger.close()
    print(f"[cal] baseline saved -> {outpath}")


if __name__ == "__main__":
    main()

"""Diagnose why packets fail checksum on a given machine.

Captures raw bytes from the serial port for a few seconds, then reports:
  - throughput (bytes/s) vs. what the baud rate allows
  - gaps between consecutive headers (2056 expected; anything else means
    bytes were dropped/inserted in transit, not just corrupted)
  - checksum pass/fail per packet, and for failures the computed-vs-received
    checksum so the kind of corruption is visible
  - the raw capture is saved so the same file can be analysed on another PC

Usage:
    python cmd/serial_check.py [--port /dev/ttyUSB0] [--baud 921600]
                               [--cols 32] [--rows 64] [--header A55A01060801]
                               [--seconds 5] [--save capture.bin]
"""
import argparse
import os
import sys
import time
from collections import Counter

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import serial  # noqa: E402

from common.config import DEFAULT_COLS, DEFAULT_HEADER_HEX, DEFAULT_ROWS  # noqa: E402


def analyse(data, header, packet_len):
    positions = []
    i = data.find(header)
    while i >= 0:
        positions.append(i)
        i = data.find(header, i + 1)

    gaps = Counter(b - a for a, b in zip(positions, positions[1:]))
    print(f"headers found: {len(positions)}")
    print("header gaps (bytes -> count), expected %d:" % packet_len)
    for gap, n in gaps.most_common(8):
        print(f"  {gap:6d} -> {n}{'  <-- OK' if gap == packet_len else ''}")

    ok = bad = short = 0
    deltas = Counter()
    for p in positions:
        pkt = data[p:p + packet_len]
        if len(pkt) < packet_len:
            short += 1
            continue
        calc = sum(pkt[:-2]) & 0xFFFF
        recv = int.from_bytes(pkt[-2:], "little")
        if calc == recv:
            ok += 1
        else:
            bad += 1
            deltas[(calc - recv) & 0xFFFF] += 1
    print(f"checksum: ok={ok} bad={bad} incomplete={short}")
    if deltas:
        print("computed - received (mod 65536) -> count (top 5):")
        for d, n in deltas.most_common(5):
            print(f"  {d:6d} (0x{d:04X}) -> {n}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=921600)
    p.add_argument("--cols", type=int, default=DEFAULT_COLS)
    p.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    p.add_argument("--header", default=DEFAULT_HEADER_HEX)
    p.add_argument("--seconds", type=float, default=5.0)
    p.add_argument("--save", default="")
    args = p.parse_args()

    header = bytes.fromhex(args.header.replace(" ", ""))
    packet_len = len(header) + args.cols * args.rows + 2

    ser = serial.Serial(args.port, args.baud, timeout=0.1)
    print(f"{ser.name}: bytesize={ser.bytesize} parity={ser.parity} stopbits={ser.stopbits} "
          f"baud={ser.baudrate}")
    ser.reset_input_buffer()
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < args.seconds:
        buf.extend(ser.read(4096))
    elapsed = time.time() - t0
    ser.close()

    print(f"received {len(buf)} bytes in {elapsed:.1f}s = {len(buf) / elapsed:.0f} B/s "
          f"(line limit ~{args.baud / 10:.0f} B/s)")
    if args.save:
        with open(args.save, "wb") as f:
            f.write(buf)
        print(f"saved raw capture -> {args.save}")
    analyse(bytes(buf), header, packet_len)


if __name__ == "__main__":
    main()

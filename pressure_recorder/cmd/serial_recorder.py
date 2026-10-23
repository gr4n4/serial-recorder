"""Prints the raw bytes coming off a serial port, unparsed -- for checking
that the sensor is actually sending something before reaching for
cmd/start.py's frame parsing. Bytes are split into packets on the header
marker (default A5 5A, matching cmd/start.py's --header), so each printed
line is one packet rather than an arbitrary read-sized chunk.

Reading happens on its own thread, decoupled from printing via a queue:
at 921600 baud (~92KB/s) a slow terminal (SSH, or a full hex dump of a
multi-KB payload on every packet) can make print() the bottleneck, and if
the main thread is blocked in print() instead of calling ser.read(),
pyserial's OS-level read buffer can overflow and silently drop bytes --
which then desyncs the header splitter too. Printing is also summarized
by default (--full for the complete payload) to keep the print side cheap
enough not to fall behind in the first place.

Usage:
    python cmd/serial_recorder.py [--port /dev/ttyUSB0] [--baud 921600]
                                   [--header A55A01060801] [--hex] [--full]
                                   [--every 1]
"""
import argparse
import os
import queue
import sys
import threading
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import serial

from common.config import DEFAULT_HEADER_HEX

RECONNECT_DELAY_S = 1.0
READ_CHUNK = 4096
READ_TIMEOUT_S = 0.1
QUEUE_GET_TIMEOUT_S = 0.2
PREVIEW_BYTES = 32  # payload bytes shown per packet unless --full


class HeaderSplitter:
    """Buffers incoming bytes and yields one packet per call to feed() for
    every *complete* header-to-next-header span found so far (the last,
    still-incomplete tail is held back until more data arrives or the
    stream stops)."""

    def __init__(self, header: bytes):
        self.header = header
        self._buf = bytearray()

    def feed(self, data: bytes):
        self._buf.extend(data)
        packets = []
        while True:
            start = self._buf.find(self.header)
            if start < 0:
                self._buf.clear()
                break
            next_start = self._buf.find(self.header, start + len(self.header))
            if next_start < 0:
                if start > 0:
                    del self._buf[:start]
                break
            packets.append(bytes(self._buf[start:next_start]))
            del self._buf[:next_start]
        return packets

    def flush(self):
        """Call on disconnect: whatever's left in the buffer is one final
        (possibly truncated) packet, if it starts with the header."""
        if self._buf.startswith(self.header):
            packet = bytes(self._buf)
            self._buf.clear()
            return [packet]
        self._buf.clear()
        return []


class SerialReaderThread:
    """Continuously reads raw bytes off `port` and pushes them onto an
    unbounded queue, so the OS-level serial buffer is drained as fast as
    possible regardless of how slow the consumer (printing) is. An
    unbounded queue means the process's own memory is the only limit --
    unlike the UART/driver buffer, it won't silently drop bytes if the
    consumer briefly falls behind."""

    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self.chunks = queue.Queue()
        self.status = queue.Queue()  # (connected: bool, message: str)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self):
        while not self._stop.is_set():
            try:
                ser = serial.Serial(self.port, self.baud, timeout=READ_TIMEOUT_S)
            except Exception as e:
                self.status.put((False, f"connect failed: {e}. Retrying..."))
                self._sleep(RECONNECT_DELAY_S)
                continue

            self.status.put((True, f"connected to {self.port}"))
            try:
                while not self._stop.is_set():
                    data = ser.read(READ_CHUNK)
                    if data:
                        self.chunks.put(data)
            except Exception as e:
                self.status.put((False, f"disconnected: {e}. Reconnecting..."))
            finally:
                try:
                    ser.close()
                except Exception:
                    pass

            if not self._stop.is_set():
                self._sleep(RECONNECT_DELAY_S)

    def _sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set():
            time.sleep(0.05)


def build_parser():
    p = argparse.ArgumentParser(prog="serial_recorder", description="Print raw serial packets")
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=921600)
    p.add_argument("--header", default=DEFAULT_HEADER_HEX, help="hex string, e.g. A55A01060801")
    p.add_argument("--hex", action="store_true",
                    help="print as hex bytes instead of raw repr")
    p.add_argument("--full", action="store_true",
                    help=f"print the full packet instead of the first {PREVIEW_BYTES}B "
                         "(slower -- can fall behind at high packet rates)")
    p.add_argument("--every", type=int, default=1,
                    help="only print 1 out of every N packets (default: 1, print all)")
    return p


def _format_bytes(data: bytes, as_hex: bool):
    return data.hex(" ") if as_hex else repr(data)


def _print_packet(packet: bytes, as_hex: bool, full: bool):
    ts = time.strftime("%H:%M:%S")
    if full or len(packet) <= PREVIEW_BYTES:
        body = _format_bytes(packet, as_hex)
    else:
        body = f"{_format_bytes(packet[:PREVIEW_BYTES], as_hex)} ...(+{len(packet) - PREVIEW_BYTES}B)"
    print(f"[{ts}] ({len(packet)}B) {body}")


def main():
    args = build_parser().parse_args()
    header = bytes.fromhex(args.header.strip().replace(" ", ""))
    splitter = HeaderSplitter(header)
    print(f"[serial_recorder] port={args.port} baud={args.baud} header={header.hex(' ')} "
          f"(Ctrl+C to stop)")

    reader = SerialReaderThread(args.port, args.baud)
    reader.start()
    packet_count = 0
    try:
        while True:
            while not reader.status.empty():
                connected, msg = reader.status.get_nowait()
                print(f"[serial_recorder] {msg}")
            try:
                data = reader.chunks.get(timeout=QUEUE_GET_TIMEOUT_S)
            except queue.Empty:
                continue
            for packet in splitter.feed(data):
                packet_count += 1
                if packet_count % args.every == 0:
                    _print_packet(packet, args.hex, args.full)
    except KeyboardInterrupt:
        print("\n[serial_recorder] stopped")
    finally:
        reader.stop()


if __name__ == "__main__":
    main()

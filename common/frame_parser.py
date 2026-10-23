import time
import numpy as np

CHECKSUM_LEN = 2


class FrameParser:
    """Fixed-length packet parser with checksum verification.

    Packet layout: header + cols*rows payload + 2-byte checksum, where the
    checksum is a little-endian 16-bit sum of every preceding byte (header
    included). A packet that fails the check is dropped and counted in
    checksum_errors; the parser then resyncs on the next header, so a
    header-like byte pattern inside the payload can't desync the framing."""

    def __init__(self, cols, rows, header_bytes, on_frame):
        self.cols = cols
        self.rows = rows
        self.header = bytes(header_bytes)
        self.on_frame = on_frame
        self.payload_len = cols * rows
        self.packet_len = len(self.header) + self.payload_len + CHECKSUM_LEN
        self.frames_ok = 0
        self.checksum_errors = 0
        self._buf = bytearray()

    def feed(self, data: bytes):
        if not data:
            return
        self._buf.extend(data)
        self._parse()

    def _parse(self):
        hdr = self.header
        hdr_len = len(hdr)
        n = self.packet_len
        while True:
            idx = self._buf.find(hdr)
            if idx < 0:
                # keep last (hdr_len - 1) bytes in case header straddles boundary
                if len(self._buf) > hdr_len - 1:
                    del self._buf[: len(self._buf) - (hdr_len - 1)]
                return
            if idx > 0:
                del self._buf[:idx]
            if len(self._buf) < n:
                return
            pkt = bytes(self._buf[:n])
            expected = int.from_bytes(pkt[-CHECKSUM_LEN:], "little")
            if (sum(pkt[:-CHECKSUM_LEN]) & 0xFFFF) != expected:
                self.checksum_errors += 1
                # not a real packet start (or corrupted): skip this header
                del self._buf[:1]
                continue
            payload = pkt[hdr_len:hdr_len + self.payload_len]
            frame = np.frombuffer(payload, dtype=np.uint8).reshape(self.rows, self.cols)
            self.frames_ok += 1
            try:
                self.on_frame(time.time(), frame)
            except Exception:
                pass
            del self._buf[:n]

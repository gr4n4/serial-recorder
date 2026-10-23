RECONNECT_DELAY_S = 1.0
SERIAL_READ_CHUNK = 4096
SERIAL_READ_TIMEOUT_S = 0.1
DEFAULT_ROWS = 32
DEFAULT_COLS = 64
# 6 bytes rather than the original 2 (A5 5A) -- a 2-byte marker collides with
# random pressure payload bytes often enough (~3% of frames, at cols*rows=2048)
# to desync FrameParser's framing and make the read grid appear to jitter/shift.
DEFAULT_HEADER_HEX = "A55A01060801"
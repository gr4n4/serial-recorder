import os
import sys

import numpy as np
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.frame_parser import FrameParser

HEADER = bytes.fromhex("A55A01060801")


def make_packet(header, payload):
    body = bytes(header) + bytes(payload)
    return body + (sum(body) & 0xFFFF).to_bytes(2, "little")


def make_parser(cols, rows, frames, header=HEADER):
    return FrameParser(cols, rows, header, lambda ts, f: frames.append(f))


def test_single_clean_frame():
    cols, rows = 4, 3
    payload = bytes(range(1, cols * rows + 1))
    frames = []
    p = make_parser(cols, rows, frames)
    p.feed(make_packet(HEADER, payload))
    assert len(frames) == 1 and p.frames_ok == 1 and p.checksum_errors == 0
    assert frames[0].shape == (rows, cols)
    assert np.array_equal(frames[0].flatten(), np.frombuffer(payload, dtype=np.uint8))


def test_noise_before_header():
    frames = []
    p = make_parser(2, 2, frames)
    p.feed(b"\x00\x01\xff" + make_packet(HEADER, bytes([1, 2, 3, 4])))
    assert len(frames) == 1
    assert frames[0].flatten().tolist() == [1, 2, 3, 4]


def test_split_across_feeds():
    frames = []
    p = make_parser(2, 2, frames)
    packet = make_packet(HEADER, bytes([10, 20, 30, 40]))
    p.feed(packet[:5])
    assert frames == []
    p.feed(packet[5:])
    assert len(frames) == 1


def test_multiple_frames():
    frames = []
    p = make_parser(2, 1, frames)
    p.feed(make_packet(HEADER, bytes([1, 2])) + make_packet(HEADER, bytes([3, 4])))
    assert len(frames) == 2
    assert frames[0][0, 0] == 1 and frames[1][0, 1] == 4


def test_header_straddles_boundary():
    frames = []
    p = make_parser(1, 1, frames)
    packet = make_packet(HEADER, bytes([77]))
    p.feed(packet[:3])
    p.feed(packet[3:])
    assert len(frames) == 1
    assert frames[0][0, 0] == 77


def test_checksum_mismatch_is_dropped_and_resyncs():
    good = make_packet(HEADER, bytes([1, 2, 3, 4]))
    bad = bytearray(make_packet(HEADER, bytes([9, 9, 9, 9])))
    bad[7] ^= 0xFF  # corrupt a payload byte
    frames = []
    p = make_parser(2, 2, frames)
    p.feed(bytes(bad) + good)
    assert len(frames) == 1 and p.checksum_errors == 1
    assert frames[0].flatten().tolist() == [1, 2, 3, 4]


def test_header_inside_payload_does_not_desync():
    payload = HEADER[:4] + bytes([1, 2, 3, 4])
    frames = []
    p = make_parser(8, 1, frames)
    p.feed(make_packet(HEADER, payload) + make_packet(HEADER, bytes(range(8))))
    assert len(frames) == 2
    assert frames[0].flatten().tolist() == list(payload)


def test_real_capture_has_no_checksum_errors():
    path = os.path.join(_REPO_ROOT, "data2_split.txt")
    if not os.path.exists(path):
        pytest.skip("data2_split.txt not present")
    frames = []
    p = make_parser(32, 64, frames)
    with open(path) as f:
        for line in f:
            p.feed(bytes.fromhex(line.strip().replace(" ", "")))
    assert p.checksum_errors == 0 and len(frames) == 82

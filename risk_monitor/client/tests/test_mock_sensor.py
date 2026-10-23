import csv
import os
import shutil
import time

import numpy as np
import pytest
import serial

from client.mock_sensor import SocatPtyBridge, build_packet, load_frames, next_row_index


def write_csv(path, rows, n_cells):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp"] + [f"MAT_{i}" for i in range(n_cells)])
        for i, row in enumerate(rows):
            w.writerow([f"ts{i}"] + list(row))


def test_load_frames_shape_and_values(tmp_path):
    csv_path = tmp_path / "data.csv"
    rows = [[0, 1, 2, 3], [10, 20, 30, 40], [255, 0, 128, 64]]
    write_csv(csv_path, rows, n_cells=4)

    frames = load_frames(str(csv_path), n_cells=4)
    assert frames.shape == (3, 4)
    assert frames.dtype == np.uint8
    assert frames.tolist() == rows


def test_load_frames_clips_out_of_range_values(tmp_path):
    csv_path = tmp_path / "data.csv"
    write_csv(csv_path, [[-5, 300, 100, 0]], n_cells=4)
    frames = load_frames(str(csv_path), n_cells=4)
    assert frames.tolist() == [[0, 255, 100, 0]]


def test_load_frames_rejects_column_count_mismatch(tmp_path):
    csv_path = tmp_path / "data.csv"
    write_csv(csv_path, [[0, 1, 2, 3]], n_cells=4)
    with pytest.raises(ValueError):
        load_frames(str(csv_path), n_cells=8)


def test_load_frames_rejects_empty_csv(tmp_path):
    csv_path = tmp_path / "data.csv"
    write_csv(csv_path, [], n_cells=4)
    with pytest.raises(ValueError):
        load_frames(str(csv_path), n_cells=4)


def test_build_packet_layout():
    header = bytes.fromhex("A55A")
    payload = np.array([1, 2, 3, 4], dtype=np.uint8)
    packet = build_packet(header, payload)
    assert packet[:2] == header
    assert packet[2:6] == payload.tobytes()
    assert packet[6:8] == (sum(packet[:6]) & 0xFFFF).to_bytes(2, "little")
    assert len(packet) == 2 + 4 + 2


def test_next_row_index_normal_mode_advances_and_wraps():
    n_frames = 5
    start_row = 3
    rows = [next_row_index(start_row, sent, n_frames, warning=False) for sent in range(7)]
    assert rows == [3, 4, 0, 1, 2, 3, 4]


def test_next_row_index_warning_mode_repeats_start_row():
    n_frames = 5
    start_row = 3
    rows = [next_row_index(start_row, sent, n_frames, warning=True) for sent in range(7)]
    assert rows == [3, 3, 3, 3, 3, 3, 3]


@pytest.mark.skipif(shutil.which("socat") is None, reason="socat not installed")
def test_socat_bridge_streams_bytes_between_ends(tmp_path):
    link = str(tmp_path / "ttyMOCK")
    own_link = str(tmp_path / "ttyMOCK_own")
    bridge = SocatPtyBridge(link, own_link=own_link)
    bridge.start()
    try:
        assert os.path.exists(link)
        assert os.path.exists(own_link)

        writer = serial.Serial(own_link, 921600, timeout=1)
        reader = serial.Serial(link, 921600, timeout=1)
        try:
            writer.write(b"\xa5\x5a\x01\x02\x03")
            time.sleep(0.2)
            data = reader.read(5)
            assert data == b"\xa5\x5a\x01\x02\x03"
        finally:
            writer.close()
            reader.close()
    finally:
        bridge.stop()
    assert not os.path.exists(link)

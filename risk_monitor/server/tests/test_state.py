from server.state import ServerState
from common.config import DEFAULT_COLS, DEFAULT_ROWS


def make_clock(start=1000.0):
    box = {"t": start}

    def clock():
        return box["t"]

    def advance(dt):
        box["t"] += dt

    return clock, advance


def test_default_config():
    state = ServerState()
    cfg = state.get_config()
    assert cfg["calibration_factor"] == 0.4755
    assert cfg["critical_pressure"] == 32.0
    assert cfg["critical_time"] == 90.0
    assert cfg["cols"] == DEFAULT_COLS
    assert cfg["rows"] == DEFAULT_ROWS


def test_update_config_merges():
    state = ServerState()
    updated = state.update_config({"critical_time": 45.0})
    assert updated["critical_time"] == 45.0
    assert updated["calibration_factor"] == 0.4755  # untouched


def test_command_is_consumed_once():
    state = ServerState()
    state.set_command("pause")
    assert state.consume_command() == "pause"
    assert state.consume_command() is None


def test_record_event_stamps_received_at_and_updates_history():
    clock, advance = make_clock()
    state = ServerState(clock=clock)
    record = state.record_event({"accumulated_time": 90.0, "risky_idx": [1], "pressure_mask_idx": [1]})
    assert record["received_at"] == 1000.0

    advance(5.0)
    state.record_event({"accumulated_time": 90.0, "risky_idx": [2], "pressure_mask_idx": [2]})
    assert len(state.event_history) == 2
    # Still-active cell 1 is kept; the client only reports newly fired cells.
    assert state.latest_event["risky_idx"] == [1, 2]
    assert state.latest_event["received_at"] == 1005.0
    assert state.event_history[-1]["risky_idx"] == [2]


def test_clearing_newer_cells_keeps_earlier_still_active_cells():
    state = ServerState()
    state.record_event({"risky_idx": [1], "risky_pressure": [200]})
    state.record_event({"risky_idx": [2], "risky_pressure": [210]})
    state.clear_event([2])
    assert state.has_warning()
    assert state.latest_event["risky_idx"] == [1]
    assert state.latest_event["risky_pressure"] == [200]
    state.clear_event([1])
    assert not state.has_warning()


def test_event_history_is_bounded():
    state = ServerState()
    for i in range(60):
        state.record_event({"accumulated_time": 90.0, "risky_idx": [i], "pressure_mask_idx": []})
    assert len(state.event_history) == 50
    assert state.event_history[-1]["risky_idx"] == [59]


def test_record_state_updates_latest_and_history():
    state = ServerState()
    state.record_state({"timestamp": 1.0, "pressure": [1, 2, 3]})
    assert state.latest_state == {"timestamp": 1.0, "pressure": [1, 2, 3]}
    assert state.state_reports == [{"timestamp": 1.0, "pressure": [1, 2, 3]}]


def test_snapshot_has_no_warning_before_any_event():
    state = ServerState()
    snap = state.snapshot()
    assert snap["latest_event"] is None
    assert snap["cols"] == DEFAULT_COLS
    assert snap["rows"] == DEFAULT_ROWS


def test_snapshot_reflects_latest_event_and_state():
    state = ServerState()
    state.record_event({"accumulated_time": 90.0, "risky_idx": [5], "pressure_mask_idx": [5]})
    state.record_state({"timestamp": 1.0, "pressure": [0] * 10})
    snap = state.snapshot()
    assert snap["latest_event"]["risky_idx"] == [5]
    assert snap["latest_state"]["pressure"] == [0] * 10


def test_clear_event_drops_latest_image_when_warning_resolved():
    state = ServerState(cols=4, rows=4)
    state.record_event({"risky_idx": [1, 2], "risky_pressure": [255, 255]})
    state.record_image(b"png")
    state.clear_event([1])
    assert state.get_latest_image() == b"png"  # still warning on cell 2
    state.clear_event([2])
    assert state.get_latest_image() is None

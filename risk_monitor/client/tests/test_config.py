import threading

from client.config import RuntimeConfig


def test_get_returns_initial_defaults():
    rc = RuntimeConfig(calibration_factor=0.5, critical_pressure=32.0, critical_time=90.0)
    assert rc.get() == (0.5, 32.0, 90.0, None)


def test_update_replaces_snapshot_atomically():
    rc = RuntimeConfig()
    rc.update(0.8, 40.0, 60.0)
    assert rc.get() == (0.8, 40.0, 60.0, None)


def test_concurrent_update_and_get_never_raises_or_tears():
    rc = RuntimeConfig(0.0, 0.0, 0.0)
    stop = threading.Event()
    errors = []

    def writer(i):
        for _ in range(500):
            rc.update(float(i), float(i), float(i))

    def reader():
        while not stop.is_set():
            try:
                a, b, c, _mask = rc.get()
                # A torn read would show up as fields from different writers.
                if not (a == b == c):
                    errors.append((a, b, c))
            except Exception as e:
                errors.append(e)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    writers = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join(timeout=2.0)

    assert errors == []


def test_build_detection_mask_ignores_out_of_range_indices():
    from client.config import build_detection_mask
    mask = build_detection_mask(4, [1, 4, 99, -1])
    assert mask.tolist() == [True, False, True, True]

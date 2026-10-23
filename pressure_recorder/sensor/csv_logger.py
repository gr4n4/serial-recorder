import csv
import os
import queue
import threading
from datetime import datetime

_STOP = object()  # sentinel: tells the writer thread to flush, close, and exit


class CsvLogger:
    """Appends one CSV row per recorded frame.

    write() only builds the row (cheap: numpy .tolist(), no per-cell
    int() calls) and hands it to a background thread over a queue --
    the actual `csv.writer.writerow` + periodic flush (disk I/O) never
    runs on the caller's thread. write() is called from the GUI's tick
    loop (see sensor/app.py's _tick, on Tkinter's main thread), so keeping
    it non-blocking matters: a write() that blocks on disk I/O would stall
    the whole UI and the frame queue draining behind it, the same class of
    problem as printing blocking a serial reader (see
    cmd/serial_recorder.py's SerialReaderThread)."""

    def __init__(self, path, cols, rows):
        self.path = os.path.expanduser(path)
        self.cols = cols
        self.rows = rows
        self._fh = None
        self._writer = None
        self._count = 0
        self._queue = None
        self._thread = None

    def open(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        self._fh = open(self.path, "w", newline="")
        self._writer = csv.writer(self._fh)
        header = ["timestamp"] + [f"c{i}" for i in range(self.cols * self.rows)]
        self._writer.writerow(header)
        self._fh.flush()
        self._count = 0
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def write(self, timestamp, frame):
        if self._queue is None:
            return
        ts = datetime.fromtimestamp(timestamp).isoformat(timespec="milliseconds")
        # .tolist() converts the whole array to native Python ints in one
        # C call -- much cheaper than `int(v) for v in frame.flatten()`,
        # and still fast enough to do here since it's just building the
        # row, not writing it.
        row = [ts] + frame.flatten().tolist()
        self._queue.put(row)

    def _run(self):
        while True:
            row = self._queue.get()
            if row is _STOP:
                break
            try:
                self._writer.writerow(row)
                self._count += 1
                if self._count % 30 == 0:
                    self._fh.flush()
            except Exception:
                pass

    def close(self):
        if self._queue is not None:
            self._queue.put(_STOP)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
        self._queue = None
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
        self._fh = None
        self._writer = None

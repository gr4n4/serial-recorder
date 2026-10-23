import os
import queue
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont

import numpy as np
import serial.tools.list_ports

from . import config
from .colormap_view import ColormapView
from .csv_logger import CsvLogger
from common.frame_parser import FrameParser
from common.serial_reader import SerialReader


BG         = "#1e1f26"
PANEL      = "#272833"
PANEL_ALT  = "#2f3140"
FG         = "#e6e6e6"
MUTED      = "#8a8d99"
ACCENT     = "#4aa3ff"
ACCENT_DN  = "#2f6fbf"
OK         = "#4ade80"
WARN       = "#facc15"
ERR        = "#f87171"
BORDER     = "#3a3c4a"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Sensor Recorder")
        self.root.minsize(960, 640)
        self.root.configure(bg=BG)

        self._init_style()

        self.frame_queue = queue.Queue(maxsize=config.QUEUE_MAXSIZE)
        self.reader = None
        self.parser = None
        self.logger = None
        self.baseline = None
        self.connected = False
        self.recording = False
        self.rotation_k = 0
        self._last_save_ts = 0.0
        self._save_interval = 1.0

        self._frame_count = 0
        self._fps_t0 = time.time()
        self._fps = 0.0
        self._latest_frame = None
        self._tick_scheduled = False

        self._build_ui()
        self._refresh_ports()
        self._update_indicators()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- Style ----------------
    def _init_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # Resize Tk named fonts so built-in entry/combobox/listbox text scale up.
        for name, family, size in (
            ("TkDefaultFont", "Helvetica", 12),
            ("TkTextFont", "Helvetica", 12),
            ("TkMenuFont", "Helvetica", 12),
            ("TkHeadingFont", "Helvetica", 12),
            ("TkCaptionFont", "Helvetica", 12),
            ("TkSmallCaptionFont", "Helvetica", 11),
            ("TkTooltipFont", "Helvetica", 11),
            ("TkFixedFont", "Consolas", 12),
            ("TkIconFont", "Helvetica", 12),
        ):
            try:
                tkfont.nametofont(name).configure(family=family, size=size)
            except tk.TclError:
                pass

        base_font = ("Helvetica", 12)
        bold_font = ("Helvetica", 12, "bold")
        header_font = ("Helvetica", 18, "bold")
        mono_font = ("Consolas", 12)
        group_font = ("Helvetica", 12, "bold")

        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL_ALT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        font=base_font)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG, font=base_font)
        style.configure("Panel.TLabel", background=PANEL, foreground=FG, font=base_font)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=base_font)
        style.configure("Header.TLabel", background=BG, foreground=FG, font=header_font)
        style.configure("Status.TLabel", background=BG, foreground=MUTED, font=base_font)
        style.configure("Stats.TLabel", background=BG, foreground=FG, font=mono_font)

        style.configure("TLabelframe", background=PANEL, foreground=ACCENT,
                        bordercolor=BORDER, relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=PANEL, foreground=ACCENT,
                        font=group_font)

        style.configure("TEntry", fieldbackground=PANEL_ALT, foreground=FG,
                        bordercolor=BORDER, insertcolor=FG, padding=5, font=base_font)
        style.configure("TCombobox", fieldbackground=PANEL_ALT, background=PANEL_ALT,
                        foreground=FG, arrowcolor=FG, bordercolor=BORDER, padding=4,
                        font=base_font)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL_ALT)],
                  foreground=[("readonly", FG)])

        style.configure("TButton", background=PANEL_ALT, foreground=FG,
                        bordercolor=BORDER, padding=(14, 8), relief="flat", font=base_font)
        style.map("TButton",
                  background=[("active", "#3a3c4a"), ("disabled", "#22232c")],
                  foreground=[("disabled", MUTED)])

        style.configure("Accent.TButton", background=ACCENT, foreground="#0b1220",
                        padding=(16, 9), font=bold_font, relief="flat")
        style.map("Accent.TButton",
                  background=[("active", ACCENT_DN), ("disabled", "#22232c")],
                  foreground=[("disabled", MUTED)])

        style.configure("Danger.TButton", background="#3a2730", foreground=ERR,
                        padding=(14, 8), relief="flat", font=base_font)
        style.map("Danger.TButton",
                  background=[("active", "#4a2a34"), ("disabled", "#22232c")],
                  foreground=[("disabled", MUTED)])

        self.root.option_add("*TCombobox*Listbox.background", PANEL_ALT)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#0b1220")

    # ---------------- UI ----------------
    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(outer, text="Sensor Recorder", style="Header.TLabel")
        title.pack(anchor="w", pady=(0, 8))

        # top: two columns of group boxes
        top = ttk.Frame(outer)
        top.pack(fill=tk.X)
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)

        # --- Serial group ---
        serial_box = ttk.Labelframe(top, text="Serial", padding=10)
        serial_box.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        ttk.Label(serial_box, text="Port", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.var_port = tk.StringVar()
        self.cb_port = ttk.Combobox(serial_box, textvariable=self.var_port, width=22, state="readonly")
        self.cb_port.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(serial_box, text="Refresh", command=self._refresh_ports).grid(row=0, column=2, padx=4, pady=4)

        ttk.Label(serial_box, text="Baud Rate", style="Panel.TLabel").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.var_baud = tk.StringVar(value=str(config.DEFAULT_BAUD))
        ttk.Combobox(
            serial_box, textvariable=self.var_baud, width=12, state="readonly",
            values=["9600", "19200", "38400", "57600", "115200", "230400",
                    "460800", "921600", "1000000", "2000000"],
        ).grid(row=1, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(serial_box, text="Data bits", style="Panel.TLabel").grid(row=2, column=0, sticky="w", padx=4, pady=4)
        ttk.Label(serial_box, text="8-bit (fixed)", style="Muted.TLabel",
                  background=PANEL).grid(row=2, column=1, sticky="w", padx=4, pady=4)

        serial_box.columnconfigure(1, weight=1)

        # --- Packet group ---
        pkt_box = ttk.Labelframe(top, text="Packet", padding=10)
        pkt_box.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        ttk.Label(pkt_box, text="Cols", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.var_cols = tk.StringVar(value=str(config.DEFAULT_COLS))
        ttk.Entry(pkt_box, textvariable=self.var_cols, width=8).grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(pkt_box, text="Rows", style="Panel.TLabel").grid(row=0, column=2, sticky="w", padx=4, pady=4)
        self.var_rows = tk.StringVar(value=str(config.DEFAULT_ROWS))
        ttk.Entry(pkt_box, textvariable=self.var_rows, width=8).grid(row=0, column=3, sticky="w", padx=4, pady=4)

        ttk.Label(pkt_box, text="Header(hex)", style="Panel.TLabel").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.var_header = tk.StringVar(value=config.DEFAULT_HEADER_HEX)
        ttk.Entry(pkt_box, textvariable=self.var_header, width=10).grid(row=1, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(pkt_box, text="Colormap", style="Panel.TLabel").grid(row=1, column=2, sticky="w", padx=4, pady=4)
        self.var_cmap = tk.StringVar(value="jet")
        cmap_cb = ttk.Combobox(pkt_box, textvariable=self.var_cmap, width=10, state="readonly",
                               values=["jet", "viridis", "gray", "inferno", "magma"])
        cmap_cb.grid(row=1, column=3, sticky="w", padx=4, pady=4)
        cmap_cb.bind("<<ComboboxSelected>>", lambda e: self._on_cmap_change())

        # --- Recording group ---
        rec_box = ttk.Labelframe(outer, text="Recording", padding=10)
        rec_box.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(rec_box, text="CSV file", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.var_csv = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "sensor_logs", "pressure_log.csv")
        )
        ttk.Entry(rec_box, textvariable=self.var_csv).grid(row=0, column=1, columnspan=3, sticky="ew", padx=4, pady=4)
        ttk.Button(rec_box, text="Browse", command=self._browse_csv).grid(row=0, column=4, padx=4, pady=4)

        ttk.Label(rec_box, text="Interval (s)", style="Panel.TLabel").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.var_interval = tk.StringVar(value="1.0")
        ttk.Entry(rec_box, textvariable=self.var_interval, width=8).grid(row=1, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(rec_box, text="(0 = every frame)", style="Muted.TLabel",
                  background=PANEL).grid(row=1, column=2, sticky="w", padx=4, pady=4)

        rec_box.columnconfigure(1, weight=1)

        # --- Controls row ---
        ctrl = ttk.Frame(outer)
        ctrl.pack(fill=tk.X, pady=(10, 0))

        self.btn_connect = ttk.Button(ctrl, text="Connect", style="Accent.TButton", command=self._on_connect)
        self.btn_connect.pack(side=tk.LEFT, padx=(0, 4))
        self.btn_disconnect = ttk.Button(ctrl, text="Disconnect", style="Danger.TButton",
                                         command=self._on_disconnect, state=tk.DISABLED)
        self.btn_disconnect.pack(side=tk.LEFT, padx=4)

        ttk.Frame(ctrl, width=20).pack(side=tk.LEFT)

        self.btn_start_rec = ttk.Button(ctrl, text="● Start Recording", command=self._on_start_rec, state=tk.DISABLED)
        self.btn_start_rec.pack(side=tk.LEFT, padx=4)
        self.btn_stop_rec = ttk.Button(ctrl, text="■ Stop Recording", command=self._on_stop_rec, state=tk.DISABLED)
        self.btn_stop_rec.pack(side=tk.LEFT, padx=4)

        ttk.Frame(ctrl, width=20).pack(side=tk.LEFT)

        self.btn_cal = ttk.Button(ctrl, text="Calibrate", command=self._on_calibrate, state=tk.DISABLED)
        self.btn_cal.pack(side=tk.LEFT, padx=4)
        self.btn_cal_reset = ttk.Button(ctrl, text="Reset Calibration", command=self._on_reset_cal, state=tk.DISABLED)
        self.btn_cal_reset.pack(side=tk.LEFT, padx=4)

        ttk.Frame(ctrl, width=20).pack(side=tk.LEFT)

        ttk.Button(ctrl, text="↺ CCW", command=self._on_rotate_ccw).pack(side=tk.LEFT, padx=4)
        ttk.Button(ctrl, text="↻ CW", command=self._on_rotate_cw).pack(side=tk.LEFT, padx=4)

        # --- Status bar ---
        status = ttk.Frame(outer)
        status.pack(fill=tk.X, pady=(10, 4))

        self.lbl_conn_dot = tk.Label(status, text="●", bg=BG, fg=ERR, font=("Helvetica", 18))
        self.lbl_conn_dot.pack(side=tk.LEFT)
        self.var_status = tk.StringVar(value="Disconnected")
        ttk.Label(status, textvariable=self.var_status, style="Status.TLabel").pack(side=tk.LEFT, padx=(4, 0))

        self.var_stats = tk.StringVar(value="FPS  0.0    Rec  —    Cal  —")
        ttk.Label(status, textvariable=self.var_stats, style="Stats.TLabel").pack(side=tk.RIGHT)

        # --- Colormap ---
        view_wrap = ttk.Frame(outer, style="Panel.TFrame", padding=6)
        view_wrap.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.view = ColormapView(view_wrap, config.DEFAULT_ROWS, config.DEFAULT_COLS)
        self.view.widget.pack(fill=tk.BOTH, expand=True)
        try:
            self.view.figure.set_facecolor(PANEL)
            self.view.ax.set_facecolor(PANEL_ALT)
            for spine in self.view.ax.spines.values():
                spine.set_color(BORDER)
            self.view.ax.tick_params(colors=MUTED)
            self.view.ax.xaxis.label.set_color(FG)
            self.view.ax.yaxis.label.set_color(FG)
        except Exception:
            pass

    # ---------------- Handlers ----------------
    def _refresh_ports(self):
        ports = sorted(p.device for p in serial.tools.list_ports.comports())
        self.cb_port["values"] = ports
        if self.var_port.get() not in ports:
            self.var_port.set(ports[0] if ports else "")

    def _browse_csv(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
            initialfile=self.var_csv.get() or "pressure_log.csv",
        )
        if path:
            self.var_csv.set(path)

    def _parse_inputs(self):
        port = self.var_port.get().strip()
        if not port:
            raise ValueError("Port not selected")
        baud = int(self.var_baud.get())
        cols = int(self.var_cols.get())
        rows = int(self.var_rows.get())
        if cols <= 0 or rows <= 0:
            raise ValueError("cols/rows must be positive")
        header_hex = self.var_header.get().strip().replace(" ", "")
        header = bytes.fromhex(header_hex)
        if not header:
            raise ValueError("Header must not be empty")
        return port, baud, cols, rows, header

    def _on_connect(self):
        try:
            port, baud, cols, rows, header = self._parse_inputs()
        except Exception as e:
            messagebox.showerror("Invalid input", str(e))
            return

        self.cols = cols
        self.rows = rows
        self.baseline = None
        self._frame_count = 0
        self._fps_t0 = time.time()

        self.view.resize_grid(rows, cols)

        self.parser = FrameParser(cols, rows, header, self._on_frame)
        self.reader = SerialReader(port, baud, self.parser, on_status=self._on_status)
        self.reader.start()

        self.connected = True
        self._update_indicators()

        if not self._tick_scheduled:
            self._tick_scheduled = True
            self.root.after(config.TICK_MS, self._tick)

    def _on_disconnect(self):
        if self.recording:
            self._on_stop_rec()
        if self.reader is not None:
            self.reader.stop()
            self.reader = None
        self.connected = False
        self.var_status.set("Disconnected")
        self._update_indicators()

    def _on_start_rec(self):
        path = self.var_csv.get().strip()
        if not path:
            messagebox.showerror("CSV", "CSV file path required")
            return
        try:
            interval = float(self.var_interval.get())
            if interval < 0:
                raise ValueError("must be >= 0")
        except Exception as e:
            messagebox.showerror("CSV", f"Invalid interval: {e}")
            return
        try:
            self.logger = CsvLogger(path, self.cols, self.rows)
            self.logger.open()
        except Exception as e:
            messagebox.showerror("CSV", f"Cannot open file: {e}")
            self.logger = None
            return
        self._save_interval = interval
        self._last_save_ts = 0.0
        self.recording = True
        self._update_indicators()

    def _on_stop_rec(self):
        if self.logger is not None:
            self.logger.close()
            self.logger = None
        self.recording = False
        self._update_indicators()

    def _on_calibrate(self):
        if self._latest_frame is None:
            messagebox.showinfo("Calibrate", "No frame received yet")
            return
        self.baseline = self._latest_frame.astype(np.int16).copy()
        self._update_indicators()

    def _on_reset_cal(self):
        self.baseline = None
        self._update_indicators()

    def _on_cmap_change(self):
        self.view.set_cmap(self.var_cmap.get())

    def _on_rotate_cw(self):
        self.rotation_k = (self.rotation_k - 1) % 4
        self._redraw_latest()

    def _on_rotate_ccw(self):
        self.rotation_k = (self.rotation_k + 1) % 4
        self._redraw_latest()

    def _redraw_latest(self):
        if self._latest_frame is None:
            return
        if self.baseline is not None:
            display = np.clip(self._latest_frame.astype(np.int16) - self.baseline, 0, 255).astype(np.uint8)
        else:
            display = self._latest_frame
        self.view.update(np.rot90(display, k=self.rotation_k))

    def _update_indicators(self):
        self.btn_connect.config(state=tk.DISABLED if self.connected else tk.NORMAL)
        self.btn_disconnect.config(state=tk.NORMAL if self.connected else tk.DISABLED)
        self.btn_start_rec.config(
            state=tk.NORMAL if (self.connected and not self.recording) else tk.DISABLED
        )
        self.btn_stop_rec.config(state=tk.NORMAL if self.recording else tk.DISABLED)
        self.btn_cal.config(state=tk.NORMAL if self.connected else tk.DISABLED)
        self.btn_cal_reset.config(state=tk.NORMAL if self.baseline is not None else tk.DISABLED)
        self.lbl_conn_dot.config(fg=OK if self.connected else ERR)

    # ---------------- Background hooks ----------------
    def _on_frame(self, ts, frame):
        try:
            self.frame_queue.put_nowait((ts, frame))
        except queue.Full:
            try:
                self.frame_queue.get_nowait()
                self.frame_queue.put_nowait((ts, frame))
            except queue.Empty:
                pass

    def _on_status(self, connected, msg):
        self.root.after(0, lambda: self.var_status.set(msg))

    # ---------------- Main loop tick ----------------
    def _tick(self):
        latest = None
        drained = 0
        while True:
            try:
                latest = self.frame_queue.get_nowait()
                drained += 1
            except queue.Empty:
                break

        if latest is not None:
            ts, raw = latest
            self._latest_frame = raw
            if self.baseline is not None:
                display = np.clip(raw.astype(np.int16) - self.baseline, 0, 255).astype(np.uint8)
            else:
                display = raw
            self.view.update(np.rot90(display, k=self.rotation_k))
            if self.recording and self.logger is not None:
                if self._save_interval <= 0 or (ts - self._last_save_ts) >= self._save_interval:
                    try:
                        self.logger.write(ts, display)
                        self._last_save_ts = ts
                    except Exception:
                        pass
            self._frame_count += drained

        now = time.time()
        dt = now - self._fps_t0
        if dt >= 0.5:
            self._fps = self._frame_count / dt if dt > 0 else 0.0
            self._fps_t0 = now
            self._frame_count = 0
        rec = "●" if self.recording else "—"
        cal = "●" if self.baseline is not None else "—"
        self.var_stats.set(f"FPS {self._fps:5.1f}    Rec {rec}    Cal {cal}")

        if self.connected:
            self.root.after(config.TICK_MS, self._tick)
        else:
            self._tick_scheduled = False

    def _on_close(self):
        try:
            self._on_disconnect()
        finally:
            self.root.destroy()

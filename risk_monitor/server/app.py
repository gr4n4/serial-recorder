r"""Real server for the pressure risk monitoring client.

Supports multiple device clients (Raspberry Pi 5 units) connected at the
same time (see CLAUDE.md). Every client identifies itself with a
`client_id` (hostname by default, see client/main.py's --client-id) on
every request; the server keeps a fully independent ServerState +
WarningStore + ConfigStore per client_id (see server/state.py's
ClientRegistry and _StoreRegistry below), so concurrent clients never
overwrite each other's config/events/state. A request with no client_id
(older client builds) falls back to a single "default" client_id.

Implements the same REST contract as client/mock_server/server.py (GET/PUT
/config, POST /event, GET/PUT /command, POST /state) plus a dashboard that
visualizes the latest risk warning: pressure_mask_idx (cells under body
pressure -- the mattress "silhouette") shaded as background, risky_idx
(cells that exceeded critical_time) highlighted on top.

Every risk warning is persisted to disk (see server/warning_store.py):
each POST /event appends a JSON line to <warning-dir>/<client_id>/warnings.log,
and each POST /image saves the snapshot PNG under
<warning-dir>/<client_id>/images/.

Client connection status is tracked from how recently each client last
contacted the server (GET /config, GET /command, POST /event|/state|
/image) -- see server/connection_monitor.py. If a client goes quiet
for longer than --client-timeout, it's marked disconnected (shown on the
dashboard) and an alert fires, scoped to that client_id.

Endpoints (all accept an optional "client_id" -- query param on GET,
JSON/form field on POST; defaults to "default" if omitted):
  GET  /config      -> {"cols", "rows", "calibration_factor", "critical_pressure",
                        "critical_time", "mask_excluded_idx"}
  PUT  /config      -> update thresholds/grid size/mask, returns the new config.
                        mask_excluded_idx is a list of flat cell indices excluded
                        from risk detection on the client (empty = whole grid
                        detected).
  POST /event       -> client risk warning: {"accumulated_time", "risky_idx", "pressure_mask_idx"}
  POST /event/clear -> client warning-cleared notice: {"cleared_idx"}; removes those
                        cells from the current warning, dropping it (has_warning -> False)
                        once no risky cells remain
  GET  /command     -> {"command": "start"|"pause"|"stop"|"reset"|"state"|null}, consumed once
  PUT  /command     -> queue a pending command: {"command": "..."}
  POST /state       -> client's answer to a "state" command: {"timestamp", "pressure"}
  POST /image       -> risk-warning snapshot PNG (multipart field "image"): current
                        frame + risky-cell overlay, not accumulated
  GET  /image/latest -> the most recently received warning snapshot PNG
  GET  /dashboard   -> HTML dashboard
  GET  /api/clients -> [{"client_id", "display_name", "connected", "last_seen",
                        "has_warning"}, ...] for every client that has ever
                        contacted the server -- powers the dashboard's client grid
  POST /api/clients/rename -> {"name"}: sets (or, if empty, clears) the
                        dashboard-assigned display name for this client_id
  GET  /api/latest  -> JSON snapshot for the dashboard poller, including
                        "connected"/"last_seen" client connection status
  GET  /api/meta    -> {"warning_dir", "server_ip"}, read-only server-side
                        info for the dashboard (server_ip is a best-effort
                        guess, for typing into the client's SERVER_IP)
  GET  /api/defaults -> hardcoded default config values, for the dashboard's
                        "기본값으로 초기화" (reset to defaults) button
  GET  /api/config/pending -> {"pending", "config"}: a config restored from disk
                        for this client_id, awaiting dashboard confirmation before
                        it takes effect (see _StoreRegistry.get_or_create)
  POST /api/config/pending/resolve -> {"action": "restore"|"default"}: applies
                        the pending config (or the hardcoded defaults) and
                        clears the pending state

Usage:
    python -m server.app [--host 0.0.0.0] [--port 5000]
                          [--cols 32] [--rows 64]
                          [--warning-dir %APPDATA%\carerobot\press_warnings]
                          [--client-timeout 10.0]
"""
import argparse
import io
import json
import logging
import re
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from client.image import render_risk_image
from server import nrcarec_alert
from server.alert import fire_alert
from server.client_names import ClientNameStore
from server.config_store import ConfigStore
from server.connection_monitor import DEFAULT_TIMEOUT_S as DEFAULT_CLIENT_TIMEOUT_S
from server.connection_monitor import ConnectionMonitor
from server.grid import idx_to_rowcol
from server.state import (
    DEFAULT_CALIBRATION_FACTOR,
    DEFAULT_COLS,
    DEFAULT_CRITICAL_PRESSURE,
    DEFAULT_CRITICAL_TIME,
    DEFAULT_ROWS,
    ClientRegistry,
    ServerState,
)
from server.warning_store import DEFAULT_DIR as DEFAULT_WARNING_DIR
from server.warning_store import WarningStore

DEFAULT_CLIENT_ID = "default"
_UNSAFE_CLIENT_ID_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _sanitize_client_id(client_id):
    """A client_id becomes a directory name (<warning_dir>/<client_id>/),
    so strip anything that isn't safe there."""
    return _UNSAFE_CLIENT_ID_CHARS.sub("_", client_id)


MOCK_WARNING_DATA_PATH = Path(__file__).resolve().parent / "mock_warning_data.json"


def _load_mock_warning_data():
    with open(MOCK_WARNING_DATA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _get_local_ip():
    """Best-effort local IP address for the dashboard's "서버 IP" display
    (client/.env's SERVER_IP is set to this by hand, per set_server_ip.sh).
    Opens a UDP "connection" to a public address -- no packet is actually
    sent -- purely so the OS picks the outbound route/interface for us;
    that's normally the active Wi-Fi adapter on a machine with no wired
    connection. Falls back to the hostname's resolved address if routing
    can't be determined (e.g. no network at all)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None


_APP_LOG_HANDLER_MARKER = "_pressure_app_log_handler"


def _configure_app_log_file(warning_dir):
    """Routes every "server.*" logger (app.py, connection_monitor.py,
    warning_store.py, ...) into <warning_dir>/app.log, in addition to the
    console -- a running record of client connect/disconnect times,
    START/PAUSE/STOP commands, and config changes, independent of
    warnings.log (which only holds risk-warning events).
    Re-running create_app (e.g. once per test) replaces the previous
    handler instead of stacking a new one on the shared "server" logger."""
    logger = logging.getLogger("server")
    logger.setLevel(logging.INFO)
    for h in list(logger.handlers):
        if getattr(h, _APP_LOG_HANDLER_MARKER, False):
            logger.removeHandler(h)
            h.close()
    log_path = Path(warning_dir) / "app.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    setattr(handler, _APP_LOG_HANDLER_MARKER, True)
    logger.addHandler(handler)


def _template_folder():
    """Resolve the templates/ dir both when run normally and when frozen
    into a PyInstaller exe, where files live under sys._MEIPASS instead
    of next to this source file."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    if hasattr(sys, "_MEIPASS"):
        return str(base / "server" / "templates")
    return str(base / "templates")


def create_app(state=None, warning_dir=DEFAULT_WARNING_DIR,
                client_timeout_s=DEFAULT_CLIENT_TIMEOUT_S):
    """`state`, if given, is only used as a template for the defaults
    (cols/rows/thresholds) that every newly-connected client's own
    ServerState starts from -- it is not shared between clients."""
    app = Flask(__name__, template_folder=_template_folder())
    app.logger.setLevel(logging.INFO)
    _configure_app_log_file(warning_dir)
    template = state or ServerState()
    registry = ClientRegistry(
        cols=template.config["cols"],
        rows=template.config["rows"],
        calibration_factor=template.config["calibration_factor"],
        critical_pressure=template.config["critical_pressure"],
        critical_time=template.config["critical_time"],
    )
    connection_monitor = ConnectionMonitor(registry, timeout_s=client_timeout_s)
    connection_monitor.start()
    name_store = ClientNameStore(warning_dir)
    # 경고를 NRCarec(간호 기록 앱) 으로도 올린다. NRCAREC_SERVICE_ACCOUNT
    # 가 비어 있으면 아무것도 하지 않으므로, 안 쓰는 설치에는 영향이 없다.
    nrcarec_alert.configure(registry, name_store)

    def _client_id():
        client_id = request.args.get("client_id")
        if not client_id and request.method != "GET":
            if request.content_type and "multipart/form-data" in request.content_type:
                client_id = request.form.get("client_id")
            else:
                client_id = (request.get_json(silent=True) or {}).get("client_id")
        return client_id or DEFAULT_CLIENT_ID

    # Per-client_id WarningStore + ConfigStore + pending-restore-confirmation
    # state, created lazily on that client's first-ever contact (analogous
    # to the single-client startup restore logic this replaces: each client
    # gets its own config.json under <warning_dir>/<client_id>/, and its
    # own decision of whether to restore it or start from defaults).
    _client_lock = threading.Lock()
    _client_entries = {}

    def _apply_config(state, config_store, updates, reason, client_id, persist=True):
        snapshot = state.update_config(updates)
        app.logger.info("client %s: config %s -> %s", client_id, reason, snapshot)
        if persist:
            config_store.save(snapshot)
        return snapshot

    def _get_client_entry(client_id):
        with _client_lock:
            entry = _client_entries.get(client_id)
            if entry is not None:
                return entry
            client_dir = Path(warning_dir) / _sanitize_client_id(client_id)
            entry = {
                "warning_store": WarningStore(client_dir),
                "config_store": ConfigStore(client_dir),
                "pending": {"config": None},
            }
            _client_entries[client_id] = entry
        saved_config = entry["config_store"].load()
        if saved_config is not None:
            entry["pending"]["config"] = saved_config
            app.logger.info(
                "client %s: config restore pending dashboard confirmation -> %s",
                client_id, saved_config,
            )
        else:
            # Hardcoded defaults, not a user-saved config -- don't persist,
            # or the next restart would find this file and wrongly prompt
            # to restore/default again (see
            # test_default_does_not_reprompt_after_a_later_restart_with_no_changes).
            state = registry.get_or_create(client_id)
            app.logger.info("client %s: config initialized -> %s", client_id, state.get_config())
        return entry

    @app.get("/api/config/pending")
    def get_pending_config():
        entry = _get_client_entry(_client_id())
        with _client_lock:
            cfg = entry["pending"]["config"]
        return jsonify({"pending": cfg is not None, "config": cfg})

    @app.post("/api/config/pending/resolve")
    def resolve_pending_config():
        client_id = _client_id()
        entry = _get_client_entry(client_id)
        state = registry.get_or_create(client_id)
        body = request.get_json(force=True)
        action = body.get("action")
        with _client_lock:
            cfg = entry["pending"]["config"]
            if cfg is None:
                return jsonify({"status": "error", "message": "no pending config"}), 400
            if action == "restore":
                snapshot = _apply_config(
                    state, entry["config_store"], cfg, "restored (confirmed)", client_id
                )
            elif action == "default":
                snapshot = _apply_config(
                    state, entry["config_store"], state.get_config(),
                    "initialized (user chose default)", client_id, persist=False,
                )
                entry["config_store"].delete()
            else:
                return jsonify({"status": "error", "message": "invalid action"}), 400
            entry["pending"]["config"] = None
        return jsonify(snapshot)

    @app.get("/config")
    def get_config():
        client_id = _client_id()
        state = registry.get_or_create(client_id)
        # The dashboard UI also reads /config (to populate its settings
        # panel), but that's a browser request, not the client device
        # checking in -- it must not count as contact, or the connection
        # monitor thinks a client has connected when none ever has (see
        # X-Dashboard-Request usage in dashboard.html).
        if request.headers.get("X-Dashboard-Request") != "1":
            state.touch()
        return jsonify(state.get_config())

    @app.put("/config")
    def put_config():
        client_id = _client_id()
        entry = _get_client_entry(client_id)
        state = registry.get_or_create(client_id)
        body = request.get_json(force=True)
        snapshot = _apply_config(state, entry["config_store"], body, "updated", client_id)
        return jsonify(snapshot)

    def _handle_event(state, warning_store, client_id, data, log_prefix="EVENT", is_mock=False):
        if is_mock:
            data = dict(data, is_mock=True)
        record = state.record_event(data)
        app.logger.info(
            "%s client=%s accumulated_time=%s risky_idx=%s pressure_mask_idx=%d cells",
            log_prefix,
            client_id,
            data.get("accumulated_time"),
            data.get("risky_idx"),
            len(data.get("pressure_mask_idx", [])),
        )
        risky_idx = data.get("risky_idx", [])
        fire_alert(
            f"압력 위험 경고({client_id}): {len(risky_idx)}개 셀이 critical_time을 초과했습니다."
        )
        warning_store.log_event(data, record["received_at"])
        # 디스크에 적은 **뒤에** 올린다. 이 순서라야 인터넷이 끊겨도 로컬
        # 기록은 남는다. (올리는 쪽은 예외를 내지 않고 바로 돌아온다.)
        nrcarec_alert.notify_event(
            client_id, name_store.get(client_id), state.get_config(), data
        )
        return record["received_at"]

    @app.post("/event")
    def post_event():
        client_id = _client_id()
        entry = _get_client_entry(client_id)
        state = registry.get_or_create(client_id)
        state.touch()
        data = request.get_json(force=True)
        received_at = _handle_event(state, entry["warning_store"], client_id, data)
        return jsonify({"status": "ok", "received_at": received_at}), 200

    @app.post("/event/clear")
    def post_event_clear():
        client_id = _client_id()
        state = registry.get_or_create(client_id)
        state.touch()
        data = request.get_json(force=True)
        cleared_idx = data.get("cleared_idx", [])
        updated = state.clear_event(cleared_idx)
        app.logger.info(
            "EVENT CLEAR client=%s cleared_idx=%s -> %s",
            client_id, cleared_idx,
            "resolved" if updated is None else f"remaining={updated['risky_idx']}",
        )
        return jsonify({"status": "ok"}), 200

    @app.post("/api/mock-warning")
    def post_mock_warning():
        """모의 경고 발생: Calib31.CSV 1000번째 행에서 뽑아 둔 실측 압력
        데이터(mock_warning_data.json)를 현재 config 임계값에 대입해 실제
        /event와 동일한 형태의 위험 경고를 만들어낸다."""
        client_id = _client_id()
        entry = _get_client_entry(client_id)
        state = registry.get_or_create(client_id)
        state.touch()
        mock = _load_mock_warning_data()
        pressure = mock["pressure"]
        config = state.get_config()
        threshold = config["critical_pressure"] / config["calibration_factor"]
        mask_excluded_idx = set(config.get("mask_excluded_idx", []))
        pressure_mask_idx = [i for i, p in enumerate(pressure) if p > 0]
        risky_idx = [
            i for i, p in enumerate(pressure)
            if p > threshold and i not in mask_excluded_idx
        ]
        data = {
            "accumulated_time": config["critical_time"],
            "risky_idx": risky_idx,
            "risky_pressure": [pressure[i] for i in risky_idx],
            "pressure_mask_idx": pressure_mask_idx,
        }
        received_at = _handle_event(
            state, entry["warning_store"], client_id, data,
            log_prefix="MOCK EVENT", is_mock=True,
        )
        image_bytes = render_risk_image(pressure, mock["rows"], mock["cols"], risky_idx)
        entry["warning_store"].save_image(image_bytes, received_at, is_mock=True)
        # 모의 경고는 POST /image 를 거치지 않는다. 여기서 직접 붙여 주지
        # 않으면 "모의 경고로 확인한다"는 절차가 정작 스냅샷만 빼고 확인하는
        # 셈이 된다.
        nrcarec_alert.notify_image(client_id, image_bytes)
        entry["warning_store"].save_status(
            pressure, mock["cols"], mock["rows"], received_at, is_mock=True
        )
        return jsonify({"status": "ok", "received_at": received_at}), 200

    @app.post("/api/mock-warning/clear")
    def clear_mock_warnings():
        client_id = _client_id()
        entry = _get_client_entry(client_id)
        state = registry.get_or_create(client_id)
        removed = entry["warning_store"].clear_mock_events()
        state.clear_mock_latest_event()
        app.logger.info("mock warnings cleared client=%s -> %d removed", client_id, removed)
        return jsonify({"status": "ok", "removed": removed}), 200

    @app.get("/command")
    def get_command():
        client_id = _client_id()
        state = registry.get_or_create(client_id)
        state.touch()
        return jsonify({"command": state.consume_command()})

    @app.put("/command")
    def put_command():
        client_id = _client_id()
        state = registry.get_or_create(client_id)
        body = request.get_json(force=True)
        command = body.get("command")
        state.set_command(command)
        app.logger.info("command queued client=%s -> %s", client_id, command)
        return jsonify({"status": "ok", "command": command})

    @app.post("/state")
    def post_state():
        client_id = _client_id()
        entry = _get_client_entry(client_id)
        state = registry.get_or_create(client_id)
        state.touch()
        data = request.get_json(force=True)
        state.record_state(data)
        app.logger.info(
            "STATE client=%s timestamp=%s cells=%d",
            client_id,
            data.get("timestamp"),
            len(data.get("pressure", [])),
        )
        pressure = data.get("pressure")
        config = state.get_config()
        if pressure and len(pressure) == config["cols"] * config["rows"]:
            received_at = time.time()
            image_bytes = render_risk_image(pressure, config["rows"], config["cols"], [])
            entry["warning_store"].save_image(image_bytes, received_at, is_status=True)
            entry["warning_store"].save_status(
                pressure, config["cols"], config["rows"], received_at, is_status=True
            )
        return jsonify({"status": "ok"}), 200

    @app.post("/image")
    def post_image():
        client_id = _client_id()
        entry = _get_client_entry(client_id)
        state = registry.get_or_create(client_id)
        state.touch()
        file = request.files.get("image")
        if file is None:
            return jsonify({"status": "error", "message": "missing 'image' file"}), 400
        image_bytes = file.read()
        state.record_image(image_bytes)
        received_at = time.time()
        saved_path = entry["warning_store"].save_image(image_bytes, received_at)
        app.logger.info(
            "IMAGE client=%s received (%d bytes) -> %s", client_id, len(image_bytes), saved_path
        )
        # 방금 올린 경고에 이 그림을 붙인다. 올린 경고가 없으면(재전송 간격에
        # 걸려 안 올렸으면) 조용히 버린다.
        nrcarec_alert.notify_image(client_id, image_bytes)
        latest_state = state.get_latest_state()
        if latest_state and latest_state.get("pressure"):
            config = state.get_config()
            entry["warning_store"].save_status(
                latest_state["pressure"], config["cols"], config["rows"], received_at
            )
        return jsonify({"status": "ok", "received_at": received_at}), 200

    @app.get("/image/latest")
    def get_latest_image():
        state = registry.get(_client_id())
        image_bytes = state.get_latest_image() if state else None
        if image_bytes is None:
            return jsonify({"status": "error", "message": "no image yet"}), 404
        return send_file(io.BytesIO(image_bytes), mimetype="image/png")

    @app.get("/dashboard")
    def dashboard():
        return render_template("dashboard.html")

    @app.get("/api/clients")
    def api_clients():
        clients = []
        for client_id in sorted(registry.all_ids()):
            client_state = registry.get(client_id)
            if client_state is None:
                continue
            clients.append({
                "client_id": client_id,
                "display_name": name_store.get(client_id),
                "connected": client_state.is_connected(),
                "last_seen": client_state.get_last_seen(),
                "has_warning": client_state.has_warning(),
            })
        return jsonify({"clients": clients})

    @app.post("/api/clients/rename")
    def rename_client():
        client_id = _client_id()
        body = request.get_json(force=True)
        display_name = name_store.set(client_id, body.get("name", ""))
        return jsonify({"status": "ok", "client_id": client_id, "display_name": display_name})

    @app.get("/api/meta")
    def api_meta():
        return jsonify({
            "warning_dir": str(Path(warning_dir).resolve()),
            "server_ip": _get_local_ip(),
        })

    @app.get("/api/defaults")
    def api_defaults():
        return jsonify({
            "cols": DEFAULT_COLS,
            "rows": DEFAULT_ROWS,
            "calibration_factor": DEFAULT_CALIBRATION_FACTOR,
            "critical_pressure": DEFAULT_CRITICAL_PRESSURE,
            "critical_time": DEFAULT_CRITICAL_TIME,
            "mask_excluded_idx": [],
        })

    @app.get("/api/history")
    def api_history():
        client_id = _client_id()
        entry = _get_client_entry(client_id)
        state = registry.get_or_create(client_id)
        limit = request.args.get("limit", default=50, type=int)
        records = entry["warning_store"].read_history(limit=limit)
        cols = state.snapshot()["cols"]
        for record in records:
            record["risky_cells"] = idx_to_rowcol(record.get("risky_idx", []), cols)
        return jsonify({"history": records})

    @app.get("/api/history/image/<received_at>")
    def api_history_image(received_at):
        entry = _get_client_entry(_client_id())
        try:
            received_at = float(received_at)
        except ValueError:
            return jsonify({"status": "error", "message": "invalid received_at"}), 400
        is_mock = request.args.get("mock") == "1"
        prefix = "mock_" if is_mock else ""
        filename = f"{prefix}{received_at:.6f}.png"
        path = entry["warning_store"].image_dir / filename
        if not path.exists():
            return jsonify({"status": "error", "message": "no image for this warning"}), 404
        return send_file(str(path), mimetype="image/png")

    @app.post("/api/history/site")
    def api_history_site():
        entry = _get_client_entry(_client_id())
        body = request.get_json(force=True)
        received_at = body.get("received_at")
        site = body.get("site", "")
        if received_at is None:
            return jsonify({"status": "error", "message": "received_at required"}), 400
        found = entry["warning_store"].annotate_site(received_at, site)
        if not found:
            return jsonify({"status": "error", "message": "no matching warning"}), 404
        return jsonify({"status": "ok"})

    @app.get("/api/latest")
    def api_latest():
        client_id = _client_id()
        state = registry.get_or_create(client_id)
        snap = state.snapshot()
        cols = snap["cols"]
        latest_event = snap["latest_event"]
        if latest_event is not None:
            latest_event["risky_cells"] = idx_to_rowcol(latest_event.get("risky_idx", []), cols)
            latest_event["pressure_mask_cells"] = idx_to_rowcol(
                latest_event.get("pressure_mask_idx", []), cols
            )
        return jsonify({
            "cols": cols,
            "display_name": name_store.get(client_id),
            "rows": snap["rows"],
            "mask_excluded_cells": idx_to_rowcol(snap.get("mask_excluded_idx", []), cols),
            "has_warning": latest_event is not None,
            "latest_event": latest_event,
            "latest_state": snap["latest_state"],
            "has_image": snap["has_image"],
            "connected": snap["connected"],
            "last_seen": snap["last_seen"],
        })

    app.config["_registry"] = registry
    app.config["_connection_monitor"] = connection_monitor
    return app


def build_parser():
    p = argparse.ArgumentParser(prog="server.app", description="Pressure risk monitoring server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--cols", type=int, default=32)
    p.add_argument("--rows", type=int, default=64)
    p.add_argument("--warning-dir", default=DEFAULT_WARNING_DIR,
                    help="directory to log warnings.log and images/ into")
    p.add_argument("--client-timeout", type=float, default=DEFAULT_CLIENT_TIMEOUT_S,
                    help="seconds of silence before the client is considered disconnected")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    app = create_app(
        ServerState(cols=args.cols, rows=args.rows),
        warning_dir=args.warning_dir,
        client_timeout_s=args.client_timeout,
    )
    print(f" * Dashboard: http://localhost:{args.port}/dashboard")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()

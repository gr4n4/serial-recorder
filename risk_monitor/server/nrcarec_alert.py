"""압력 위험 경고를 NRCarec 으로 보낸다.

NRCarec 은 Firebase Hosting 에 올라간 정적 웹앱이라 서버가 없다. POST 를
받을 창구가 없으므로 이쪽에서 Firestore 에 직접 쓴다. 레이더 경보를 보내는
emfit_server 와 같은 방식이고, 문서 모양도 같게 맞춘다.

반드시 지킬 것 두 가지.

  1. 여기서 난 예외가 절대 밖으로 나가면 안 된다.
     app.py 는 이 호출 **뒤에** 경고를 디스크에 적는다(warning_store).
     인터넷이 끊겼다고 로컬 기록까지 날아가면, 정작 지켜야 할 것을 잃는다.

  2. Flask 요청을 붙잡지 않는다.
     Firestore 쓰기와 FCM 발송은 몇 초씩 걸린다. 라즈베리파이는 5초만
     기다리고(--http-timeout) 실패로 친다. 센서가 여러 대면 실제로 밀린다.
     그래서 큐에 넣고 바로 돌아오고, 보내는 일은 작업 스레드가 맡는다.

보내는 주기가 로컬과 다르다. 클라이언트는 위험이 이어지면 5분마다 다시
보내지만(--alert-cooldown), NRCarec 에는 기본 15분에 한 번만 올린다.
병동 대시보드는 촘촘히 봐야 하지만, Firestore 는 무료 할당량(하루 쓰기
2만 건·읽기 5만 건)이 있고 문서 하나가 여러 기기로 퍼져 읽히기 때문이다.

쓰지 않으려면 NRCAREC_SERVICE_ACCOUNT 를 비워 두면 된다. 그러면 이 모듈은
아무것도 하지 않고, 서버는 지금까지처럼 그대로 돈다.
"""
import base64
import json
import logging
import os
import queue
import threading
import time

logger = logging.getLogger(__name__)

# 서비스 계정 JSON 경로. 없으면 이 모듈 전체가 잠잠해진다.
ENV_KEY = "NRCAREC_SERVICE_ACCOUNT"

# 연습 모드. 키가 맞는지, 무슨 문구가 갈지만 로그로 보여 주고 실제로는
# 아무것도 쓰지 않는다.
#
# 이게 없으면 설치를 확인할 방법이 "진짜로 한 번 쏴 보기"뿐인데, 그러면
# 등록된 간호사 폰이 전부 울린다. 한밤중에 설정을 손볼 수도 있는 노릇이라
# 확인과 발송을 갈라 둔다.
ENV_DRY_RUN = "NRCAREC_DRY_RUN"

# NRCarec 으로 같은 센서의 경고를 다시 올리기까지 기다리는 시간(초).
DEFAULT_COOLDOWN_S = 900.0

# 경고를 보낸 뒤 그 스냅샷이 따라오기를 기다리는 시간(초).
# 클라이언트는 /event 직후 /image 를 보내므로 실제로는 1초 안쪽이다.
IMAGE_WINDOW_S = 30.0

# 설정 거울(settings/pressure_status)을 얼마나 자주 살펴볼지(초).
# 값이 바뀌었을 때만 실제로 쓴다.
MIRROR_POLL_S = 60.0

KIND = "pressure"
TITLE = "욕창 위험 감지"
ICON = "/icons/notify-pressure.png"


class _Sender:
    """Firestore 로 보내는 쪽 전부. 실패는 로그로만 남기고 삼킨다."""

    def __init__(self):
        self._q = queue.Queue(maxsize=200)
        self._lock = threading.Lock()
        self._app = None
        self._started = False
        self._registry = None
        self._name_store = None

        # client_id -> 마지막으로 NRCarec 에 올린 시각
        self._last_sent = {}
        # client_id -> (문서 ID, 보낸 시각). 뒤따라오는 스냅샷을 붙일 곳.
        self._recent = {}
        # 거울 문서에 마지막으로 쓴 내용. 같으면 다시 쓰지 않는다.
        self._mirror_sig = None

    # ---------- 준비 ----------

    @property
    def enabled(self):
        return bool(os.environ.get(ENV_KEY, "").strip())

    @property
    def dry_run(self):
        return os.environ.get(ENV_DRY_RUN, "").strip().lower() in (
            "1", "true", "yes", "y", "on"
        )

    def configure(self, registry, name_store):
        """app.py 가 만든 것들을 빌려 둔다. 설정 거울을 쓸 때 필요하다."""
        self._registry = registry
        self._name_store = name_store
        if self.enabled:
            self._ensure_worker()
        else:
            logger.info(
                "NRCarec 연동 꺼짐 (%s 가 비어 있음). 경고는 로컬에만 남는다.",
                ENV_KEY,
            )

    def _ensure_worker(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._worker, name="nrcarec", daemon=True).start()
        threading.Thread(target=self._mirror_loop, name="nrcarec-mirror",
                         daemon=True).start()
        logger.info("NRCarec 연동 켜짐 (재전송 간격 %.0f분)",
                    _cooldown() / 60.0)

    def _db(self):
        """firebase-admin 을 늦게 불러온다. 안 깔려 있어도 서버는 떠야 한다."""
        if self._app is not None:
            return self._firestore.client(app=self._app)
        import firebase_admin
        from firebase_admin import credentials, firestore

        self._firestore = firestore
        self._app = firebase_admin.initialize_app(
            credentials.Certificate(os.environ[ENV_KEY]), name="nrcarec"
        )
        return firestore.client(app=self._app)

    # ---------- app.py 가 부르는 곳 ----------

    def notify_event(self, client_id, display_name, config, data):
        """위험 경고 한 건. 절대 예외를 내지 않고 즉시 돌아온다."""
        if not self.enabled:
            return
        try:
            self._ensure_worker()
            risky = data.get("risky_idx") or []
            self._put(("event", {
                "client_id": client_id,
                "display_name": (display_name or client_id or "").strip(),
                "cells": len(risky),
                "accumulated_time": data.get("accumulated_time"),
                "critical_time": (config or {}).get("critical_time"),
            }))
        except Exception as e:
            logger.warning("[NRCarec] 경고 담기 실패: %s", e)

    def notify_image(self, client_id, image_bytes):
        """방금 보낸 경고에 붙일 스냅샷. 붙일 것이 없으면 조용히 버린다."""
        if not self.enabled or not image_bytes:
            return
        try:
            with self._lock:
                recent = self._recent.get(client_id)
            if not recent or (time.time() - recent[1]) > IMAGE_WINDOW_S:
                return
            self._put(("image", {"doc_id": recent[0], "png": image_bytes}))
        except Exception as e:
            logger.warning("[NRCarec] 스냅샷 담기 실패: %s", e)

    def _put(self, item):
        try:
            self._q.put_nowait(item)
        except queue.Full:
            # 밀렸으면 버린다. 쌓아 두면 한참 지난 경고가 뒤늦게 뜬다.
            logger.warning("[NRCarec] 보낼 것이 밀려 한 건 버림")

    # ---------- 작업 스레드 ----------

    def _worker(self):
        while True:
            kind, payload = self._q.get()
            try:
                if kind == "event":
                    self._send_event(payload)
                elif kind == "image":
                    self._send_image(payload)
            except Exception as e:
                # 여기서 죽으면 이후 경고가 전부 막힌다. 무슨 일이 있어도 계속.
                logger.warning("[NRCarec] 전송 실패(%s): %s", kind, e)
            finally:
                self._q.task_done()

    def _send_event(self, p):
        client_id = p["client_id"]

        now = time.time()
        with self._lock:
            last = self._last_sent.get(client_id, 0.0)
        if now - last < _cooldown():
            return

        db = self._db()
        if not self._wanted(db):
            return

        who = p["display_name"] or client_id
        mins = p["critical_time"]
        mins_text = f"{int(mins)}분" if isinstance(mins, (int, float)) else "기준 시간"
        body = f"{who} · {p['cells']}개 셀이 {mins_text}을 넘겼습니다."

        doc_id = f"{KIND}_{_safe(client_id)}_{int(now)}"

        if self.dry_run:
            # 여기까지 왔으면 키도 맞고 설정도 켜져 있다는 뜻이다.
            # 실제로 쓰지 않으므로 간호사 폰은 울리지 않는다.
            logger.info(
                "[NRCarec] 연습 모드 — 보내지 않음. 실제로는 이렇게 갔을 것:\n"
                "          문서 %s\n          본문 %s", doc_id, body,
            )
            with self._lock:
                self._last_sent[client_id] = now
            return

        db.collection("notification_log").document(doc_id).set({
            "sentAt": self._firestore.SERVER_TIMESTAMP,
            "kind": KIND,
            "title": TITLE,
            "body": body,
            # 센서에 붙인 이름을 그대로 쓴다. 병동에서 '421호 김복순' 처럼
            # 지어 두면 그대로 보인다. 환자가 누구인지는 이쪽이 모른다.
            "room": who,
            "patientName": "",
            "deviceId": client_id,
            "cellCount": p["cells"],
            "accumulatedTime": p.get("accumulated_time"),
        })

        with self._lock:
            self._last_sent[client_id] = now
            self._recent[client_id] = (doc_id, now)

        self._push(db, doc_id, body)
        self._write_mirror(db, force=True)
        logger.info("[NRCarec] 경고 보냄 client=%s cells=%d doc=%s",
                    client_id, p["cells"], doc_id)

    def _send_image(self, p):
        """스냅샷은 경보 문서와 떼어 놓는다.

        notification_log 는 앱의 다섯 군데가 실시간으로 구독한다. 거기에
        그림을 넣으면 목록을 열 때마다 100건어치 그림을 같이 받는다.
        따로 두면 간호사가 눌러 볼 때만 3KB 가 오간다."""
        png = p["png"]
        db = self._db()
        db.collection("pressure_snapshots").document(p["doc_id"]).set({
            "png": base64.b64encode(png).decode("ascii"),
            "at": self._firestore.SERVER_TIMESTAMP,
        })

    # ---------- 알림 설정 ----------

    def _wanted(self, db):
        """앱의 '알림 설정 → 센서 경보 → 욕창 위험' 을 따른다.

        앱도 같은 값을 보고 팝업을 거르지만, 꺼 둔 알림은 애초에 보내지
        않는 것이 맞다. 보내 놓고 받는 쪽에서 버리면 할당량만 쓴다."""
        try:
            doc = db.collection("settings").document("notifications").get()
            s = (doc.to_dict() or {}).get("sensorAlerts", {}) if doc.exists else {}
            return s.get(KIND, True) is not False
        except Exception as e:
            # 설정을 못 읽었다고 경보를 막지는 않는다. 못 가는 쪽이 더 나쁘다.
            logger.warning("[NRCarec] 알림 설정 읽기 실패, 보내기로 함: %s", e)
            return True

    # ---------- 푸시 ----------

    def _push(self, db, doc_id, body):
        from firebase_admin import messaging

        tokens = [d.id for d in db.collection("push_tokens").stream()]
        if not tokens:
            return

        res = messaging.send_each_for_multicast(
            messaging.MulticastMessage(
                tokens=tokens,
                # data 만 보낸다. notification 을 같이 실으면 브라우저가 한 번,
                # 서비스워커가 또 한 번 띄워 알림이 두 번 뜬다.
                data={
                    "title": TITLE,
                    "body": body,
                    "kind": KIND,
                    "icon": ICON,
                    "tag": doc_id,
                    # 어느 문서인지 알려 준다. 이게 있어야 폰에서 확인했을 때
                    # 다른 기기들도 같이 조용해진다.
                    "logId": doc_id,
                    "url": "/",
                },
                webpush=messaging.WebpushConfig(
                    headers={"Urgency": "high", "TTL": "600"},
                ),
            ),
            app=self._app,
        )

        # 만료된 토큰 정리. 그냥 두면 발송 실패가 계속 쌓인다.
        for token, r in zip(tokens, res.responses):
            if (not r.success and r.exception
                    and "registration-token-not-registered" in str(r.exception)):
                db.collection("push_tokens").document(token).delete()

    # ---------- 설정 거울 ----------

    def _mirror_loop(self):
        while True:
            time.sleep(MIRROR_POLL_S)
            try:
                self._write_mirror(self._db())
            except Exception as e:
                logger.warning("[NRCarec] 설정 거울 갱신 실패: %s", e)

    def _write_mirror(self, db, force=False):
        """임계값과 센서 상태를 NRCarec 이 읽기만 하도록 비춰 둔다.

        바꾸는 곳은 여전히 압력 대시보드다. 원본을 둘로 만들면 어느 쪽이
        맞는지 정할 수 없어진다. 다만 간호사가 '지금 몇으로 돼 있나'를
        NRCarec 한 곳에서 볼 수 있어야 해서, 값만 흘려 보낸다."""
        # 연습 모드에서는 아무것도 쓰지 않는다. 확인하러 켰는데 뭔가 남으면
        # 그것대로 헷갈린다.
        if self._registry is None or self.dry_run:
            return

        sensors = []
        critical_pressure = None
        critical_time = None
        for client_id in sorted(self._registry.all_ids()):
            st = self._registry.get(client_id)
            if st is None:
                continue
            cfg = st.get_config()
            critical_pressure = cfg.get("critical_pressure")
            critical_time = cfg.get("critical_time")
            name = self._name_store.get(client_id) if self._name_store else None
            sensors.append({
                "id": client_id,
                "name": name or client_id,
                "connected": st.is_connected(),
            })

        if not sensors:
            return

        # lastSeen 은 늘 달라지므로 빼고 견준다. 이것까지 넣으면 1분마다 쓴다.
        sig = json.dumps([critical_pressure, critical_time, sensors],
                         sort_keys=True, ensure_ascii=False)
        if not force and sig == self._mirror_sig:
            return

        db.collection("settings").document("pressure_status").set({
            "updatedAt": self._firestore.SERVER_TIMESTAMP,
            "criticalPressure": critical_pressure,
            "criticalTime": critical_time,
            "sensors": sensors,
        })
        self._mirror_sig = sig


def _cooldown():
    try:
        return float(os.environ.get("NRCAREC_COOLDOWN_SEC", DEFAULT_COOLDOWN_S))
    except ValueError:
        return DEFAULT_COOLDOWN_S


def _safe(client_id):
    """문서 ID 에 들어가도 되는 글자만 남긴다."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(client_id))


_sender = _Sender()

configure = _sender.configure
notify_event = _sender.notify_event
notify_image = _sender.notify_image

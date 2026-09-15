# NRCarec 연동

압력 위험 경고를 간호 기록 앱 **NRCarec** 으로도 올린다. 원래 동작은
그대로다 — 대시보드 팝업, 소리, `warnings.log`, 스냅샷 PNG 모두 지금처럼
남고, 거기에 한 갈래가 더해질 뿐이다.

```
라즈베리파이 ──POST /event──▶ 윈도우 서버
                                 ├──▶ warnings.log + images/   (지금까지 하던 것)
                                 ├──▶ 대시보드 팝업 + 소리       (지금까지 하던 것)
                                 └──▶ Firestore                 (여기가 새로 붙은 것)
                                        └─▶ NRCarec 폰·스테이션
```

## 왜 서버가 보내는가

NRCarec 은 Firebase Hosting 에 올라간 정적 웹앱이라 **서버가 없다.** POST 를
받을 창구가 없으므로 보내는 쪽이 Firestore 에 직접 쓴다. 레이더 경보를 보내는
`emfit_server` 와 같은 방식이다.

## 준비

```bash
pip install -r server/requirements.txt
```

Firebase 콘솔 → 프로젝트 설정 → 서비스 계정 → **새 비공개 키 생성**(JSON).

> **비공개 키다. 저장소에 커밋하지 말 것.** 경로만 환경변수로 넣는다.

```bat
setx NRCAREC_SERVICE_ACCOUNT "%USERPROFILE%\pressure-server\nrcarec-key.json"
```

환경변수를 **비워 두면 이 기능 전체가 꺼진다.** 그때는 지금까지와 똑같이
로컬에만 기록한다. 안 쓰는 설치에는 아무 영향이 없다.

## 보내는 주기가 로컬과 다르다

| | 주기 | 왜 |
|---|---|---|
| 클라이언트 → 서버 | 5분 (`--alert-cooldown`) | 병동 대시보드는 촘촘히 봐야 한다 |
| 서버 → NRCarec | **15분** | Firestore 무료 할당량 |

문서 하나가 쓰이면 앱의 여러 화면·여러 기기가 그것을 읽는다. 5분 간격으로
센서 8대면 하루 읽기가 2만을 넘어 무료 한도(5만)를 절반 넘게 먹는다.
15분이면 여유롭고, 90분 누적으로 잡는 욕창에는 충분히 촘촘하다.

바꾸려면:

```bat
setx NRCAREC_COOLDOWN_SEC 900
```

## 지킨 것 두 가지

**1. 로컬 기록이 우선이다.** `app.py` 는 `warning_store.log_event()` 로 디스크에
적은 **뒤에** NRCarec 으로 올린다. 인터넷이 끊겨도 로컬 기록은 그대로 남는다.
올리는 쪽은 어떤 예외도 밖으로 내지 않는다.

**2. 요청을 붙잡지 않는다.** Firestore 쓰기와 FCM 발송은 몇 초씩 걸린다.
라즈베리파이는 5초만 기다리고(`--http-timeout`) 실패로 친다. 그래서 큐에
넣고 바로 돌아오며, 실제 발송은 작업 스레드가 맡는다.

## 쓰는 곳

| 컬렉션 | 내용 |
|---|---|
| `notification_log/{id}` | 경보 한 건. 제목·본문·센서 이름·셀 수 |
| `pressure_snapshots/{id}` | 그 순간 스냅샷 PNG(base64, 약 3KB). 경보와 같은 문서 ID |
| `settings/pressure_status` | 임계값·센서 목록을 **읽기 전용으로 비춰 둔 것** |

스냅샷을 경보 문서와 떼어 놓은 이유: `notification_log` 는 앱의 다섯 군데가
실시간 구독한다. 그림을 같이 넣으면 목록을 열 때마다 100건어치 그림이
따라온다. 따로 두면 간호사가 눌러 볼 때만 오간다.

`settings/pressure_status` 는 **비추기만** 한다. 임계값을 바꾸는 곳은 여전히
이 대시보드다. 원본을 둘로 만들면 어느 쪽이 맞는지 정할 수 없어진다. 다만
간호사가 "지금 몇으로 돼 있나"를 NRCarec 한 곳에서 볼 수 있어야 해서 값만
흘려 보낸다.

## 알림을 끄는 곳

NRCarec 앱의 **알림 설정 → 센서 경보 → 욕창 위험**. 꺼 두면 서버가 아예
보내지 않는다(`settings/notifications` 의 `sensorAlerts.pressure`).
설정을 못 읽으면 보내는 쪽으로 판단한다 — 못 가는 편이 더 나쁘다.

## 확인 방법

서버를 띄우고 대시보드에서 **모의 경고**를 누른다.

```
[NRCarec] 경고 보냄 client=pi-a cells=105 doc=pressure_pi-a_1789462220
```

이 줄이 `app.log` 에 뜨면 올라간 것이다. NRCarec 알림 기록에도 같은 건이
보인다. 안 뜨면 같은 파일에서 `[NRCarec]` 로 시작하는 실패 줄을 찾는다.

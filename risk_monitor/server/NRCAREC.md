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

받은 파일을 저장소 밖에 두고 경로만 넘긴다.

**PowerShell**

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.nrcarec"
Move-Item "$env:USERPROFILE\Downloads\*firebase-adminsdk*.json" "$env:USERPROFILE\.nrcarec\nrcarec-key.json"
setx NRCAREC_SERVICE_ACCOUNT "$env:USERPROFILE\.nrcarec\nrcarec-key.json"
```

**명령 프롬프트(cmd)**

```bat
mkdir "%USERPROFILE%\.nrcarec"
move "%USERPROFILE%\Downloads\*firebase-adminsdk*.json" "%USERPROFILE%\.nrcarec\nrcarec-key.json"
setx NRCAREC_SERVICE_ACCOUNT "%USERPROFILE%\.nrcarec\nrcarec-key.json"
```

> **둘을 섞지 말 것.** `%USERPROFILE%` 는 cmd 문법이라 PowerShell 에서는
> 글자 그대로 저장된다(`setx` 는 저장할 때 풀어 주지 않는다). 그러면 경로를
> 못 찾아 연동이 조용히 꺼진다. PowerShell 에서는 `$env:USERPROFILE` 를 쓴다.
>
> `setx` 는 **다음에 새로 여는 창부터** 적용된다. 지금 창에서 바로 쓰려면
> PowerShell 은 `$env:NRCAREC_SERVICE_ACCOUNT = "..."`, cmd 는
> `set NRCAREC_SERVICE_ACCOUNT=...` 를 한 번 더 친다.
>
> 잘 들어갔는지는 **새 창**에서 확인한다 — PowerShell `echo $env:NRCAREC_SERVICE_ACCOUNT`.

환경변수를 **비워 두면 이 기능 전체가 꺼진다.** 그때는 지금까지와 똑같이
로컬에만 기록한다. 안 쓰는 설치에는 아무 영향이 없다.

## 먼저 연습 모드로 확인한다

설치가 맞는지 보려고 진짜 경고를 쏘면 **등록된 간호사 폰이 전부 울린다.**
한밤중에 설정을 손볼 수도 있으니, 확인과 발송을 갈라 두었다.

```powershell
$env:NRCAREC_DRY_RUN = "1"
python -m server.app
```

이 상태로 대시보드에서 **모의 경고**를 누르면 `app.log` 에 이렇게 뜬다.

```
[NRCarec] 연습 모드 — 보내지 않음. 실제로는 이렇게 갔을 것:
          문서 pressure_pi-421_1789606694
          본문 421호 김복순 · 105개 셀이 90분을 넘겼습니다.
```

여기까지 왔으면 키도 맞고 알림 설정도 켜져 있다는 뜻이다. Firestore 에는
아무것도 쓰지 않았고 폰도 울리지 않았다.

본문 앞이 `pi-421` 처럼 나오면 그 센서에 **이름표를 아직 안 지은 것**이다.
대시보드에서 `421호 김복순` 으로 바꾸면 그대로 나온다 — 환자가 누구인지는
이쪽이 모르므로 지어 준 이름을 그대로 쓴다.

확인이 끝나면 연습 모드를 끈다. 창을 닫았다 새로 열어도 된다.

```powershell
Remove-Item Env:\NRCAREC_DRY_RUN
```

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

## 진짜로 보내기

연습 모드를 끄고 같은 절차를 되풀이한다. 이번에는 이렇게 뜬다.

```
[NRCarec] 경고 보냄 client=pi-421 cells=105 doc=pressure_pi-421_1789606694
```

NRCarec 알림 기록에도 같은 건이 보이고, 등록된 기기로 푸시가 간다.
안 뜨면 같은 파일에서 `[NRCarec]` 로 시작하는 실패 줄을 찾는다.

| 로그 | 뜻 |
|---|---|
| `NRCarec 연동 꺼짐 (...가 비어 있음)` | 환경변수가 안 잡혔다. 창을 새로 열었는지 확인 |
| `알림 설정 읽기 실패, 보내기로 함` | Firestore 에 못 닿았다. 그래도 경보는 보낸다 |
| `보낼 것이 밀려 한 건 버림` | 네트워크가 오래 막혀 큐가 찼다 |
| 아무 줄도 없음 | 재전송 간격(15분)에 걸렸거나 앱에서 욕창 알림을 꺼 두었다 |

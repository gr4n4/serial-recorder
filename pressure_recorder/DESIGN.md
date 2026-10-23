# Sensor Recorder — 프로그램 설계

매트리스 압력 센서의 데이터를 시리얼로 읽어 실시간 colormap으로 표시하고, CSV로 저장하는 Python 프로그램.

## 1. 목표 / 요구사항

- **입력**: 시리얼 포트(USB-UART 등)로부터 매트리스 센서의 압력 프레임 수신
- **표시**: 실시간 2D heatmap (colormap) 디스플레이
- **저장**: 프레임 단위 CSV 로깅 (한 행 = 한 프레임)
- **제어**:
  - 포트 선택 + 새로고침
  - **Connect / Disconnect** (시리얼 연결 — 연결만으로 실시간 표시 동작)
  - **Start Recording / Stop Recording** (CSV 기록은 별도 토글)
  - **Calibration** (현재 프레임을 baseline으로 저장 → 이후 프레임에서 차감)
  - **Reset Calibration** (baseline 제거)
  - **Rotate CW / CCW** (디스플레이 90° 단위 회전, 표시 전용)
  - 해상도/헤더/파일명 설정
- **자동 재연결**: 연결 상태에서 시리얼이 끊기면 백그라운드에서 자동 재연결 시도

## 2. 사양 (확정)

| 항목              | 값                                    | 비고                        |
| ----------------- | ------------------------------------- | --------------------------- |
| 센서 해상도       | **cols × rows = 32 × 64** (기본)      | GUI에서 변경 가능           |
| 셀당 비트         | **8-bit unsigned**                    | 1 byte/cell                 |
| Baudrate          | **921600**                            | 변경 가능                   |
| 프레임 헤더       | **`A5 5A 01 06 08 01`** (6 bytes)     | 변경 가능                   |
| 페이로드          | `cols × rows` bytes (기본 2048)       |                             |
| 체크섬            | **2 bytes**, little-endian uint16     | 헤더+페이로드의 합 & 0xFFFF |
| 한 패킷 전체 길이 | `6 + (cols×rows) + 2` (기본 2056)     | 고정 길이                   |

### 2.1 패킷 레이아웃

```
offset  size  field
0       6     HEADER   (A5 5A 01 06 08 01)
6       N     PAYLOAD  (cols × rows bytes, 압력값)
6+N     2     CHECKSUM (little-endian uint16 = sum(앞의 모든 바이트) & 0xFFFF)
```

`N = cols × rows`. 페이로드는 row-major로 해석 — `FrameParser`에서 `reshape(rows, cols)`.

## 3. 아키텍처

```
┌────────────────┐   bytes    ┌──────────────┐  frame   ┌──────────────┐
│ SerialReader   │ ─────────► │ FrameParser  │ ───────► │ FrameQueue   │
│ (thread)       │            │ (헤더 동기화)│          │ (thread-safe)│
└────────────────┘            └──────────────┘          └──────┬───────┘
                                                                │
                                       ┌────────────────────────┴────────┐
                                       ▼                                 ▼
                               ┌───────────────┐                 ┌──────────────┐
                               │ ColormapView  │                 │ CsvLogger    │
                               │ (GUI thread)  │                 │              │
                               └───────────────┘                 └──────────────┘
```

- **Producer/Consumer**: 시리얼 I/O는 별도 스레드, GUI는 메인 스레드.
- 단일 소비자(GUI 타이머 콜백)가 큐에서 프레임을 꺼내 view 갱신 + CSV 기록을 모두 호출.
- 큐는 `queue.Queue(maxsize=...)`로 백프레셔 시 오래된 프레임 drop.

## 4. GUI 프레임워크

가벼움 우선 → **Tkinter + matplotlib** (`FigureCanvasTkAgg` + `imshow`).

- 표준 라이브러리 Tkinter라 추가 의존성 최소
- `imshow`의 `set_data()` + `canvas.draw_idle()`로 30 Hz 갱신 충분
- 32×64 정도면 matplotlib로 충분히 부드러움
- **다크 테마**: `clam` 기반 ttk 커스텀 스타일 (패널/보더/액센트 컬러), LabelFrame으로 Serial/Packet/Recording 그룹핑
- 연결 상태는 좌하단 컬러 도트(녹색/빨강)로 표시
- 기본 폰트 12pt, 헤더 18pt — 가독성 우선

## 5. 모듈 구조

```
pressure_recorder/
├── DESIGN.md
├── requirements.txt
├── main.py                  # 진입점
├── sensor/
│   ├── __init__.py
│   ├── config.py            # 기본값 (../common/config.py 재노출)
│   ├── csv_logger.py        # CSV 저장 (wide format)
│   ├── colormap_view.py     # Tk + matplotlib 위젯
│   └── app.py               # Tk 메인 윈도우, 컨트롤
├── cmd/                     # CLI 도구 (start / display / calibration)
└── tests/
    └── test_frame_parser.py

../common/                   # risk_monitor/client와 공유
├── config.py                # DEFAULT_COLS=32, DEFAULT_ROWS=64, DEFAULT_HEADER_HEX 등
├── serial_reader.py         # SerialReader 스레드
└── frame_parser.py          # 헤더 동기화 + 체크섬 검증 + 프레임 추출
```

## 6. 주요 컴포넌트

### 6.1 `config.py`

```python
DEFAULT_COLS = 32
DEFAULT_ROWS = 64
DEFAULT_HEADER_HEX = "A55A01060801"     # bytes.fromhex() 로 변환
```

`sensor/config.py`는 `../common/config.py`의 값을 그대로 가져옵니다. baud 기본값(921600)은 GUI/CLI 쪽에서 지정합니다.

### 6.2 `SerialReader` ([common/serial_reader.py](../common/serial_reader.py))

- `pyserial.Serial(port, baudrate, timeout=...)`
- 백그라운드 스레드 루프:
  1. 포트 열기 (실패 시 1초 대기 후 재시도)
  2. `read(N)` → `parser.feed(bytes)`
  3. `SerialException` 또는 read 실패 시 포트 close → 1단계로 복귀 (**자동 재연결**)
- `stop()` 플래그로 graceful shutdown
- 상태 콜백(`on_status(connected: bool, msg: str)`)으로 GUI에 연결/끊김/재연결 알림

### 6.3 `FrameParser` ([common/frame_parser.py](../common/frame_parser.py))

- 내부 바이트 버퍼(`bytearray`) 유지
- 알고리즘:
  1. 버퍼에서 헤더(`A5 5A 01 06 08 01`) 검색
  2. 헤더 발견 시, 패킷 전체 길이(`헤더 + cols*rows + 2`) 바이트가 모일 때까지 대기
  3. 체크섬 검증: 마지막 2바이트(little-endian) == 앞의 모든 바이트 합 & 0xFFFF
  4. 일치하면 페이로드만 잘라 `np.frombuffer(..., dtype=np.uint8).reshape(rows, cols)` (**row-major**)로 `on_frame` 호출, 패킷 전체를 버퍼에서 제거
  5. 불일치하면 `checksum_errors`를 올리고 헤더 첫 바이트만 건너뛴 뒤 다음 헤더부터 재탐색 (페이로드 안에 헤더와 같은 바이트열이 있어도 동기화가 깨지지 않음)
- 파라미터(`cols, rows, header, on_frame`)는 생성자 주입 → 런타임 변경 시 재생성
- 카운터: `frames_ok`, `checksum_errors`
- **단위 테스트 대상**: 정상 프레임, 헤더 중간 끊김, 잡음 섞임, 부분 수신, 체크섬 불일치 후 재동기화, 페이로드 내 헤더 패턴

### 6.4 `CsvLogger` ([sensor/csv_logger.py](sensor/csv_logger.py))

- 파일명은 사용자 지정
- 헤더 행: `timestamp, c0, c1, ..., c{R*C-1}`
- 데이터 행: `2026-04-13T12:34:56.789, v0, v1, ..., v{R*C-1}` (ISO 8601, ms 단위)
- `frame.flatten()` 후 한 줄로 기록 (한 행 = 한 프레임, row-major 원본 기준 — 회전 무시)
- Calibration 활성 시 **보정값**이 기록됨
- `csv.writer` + 주기적 `flush()`, Stop 시 close

### 6.5 `ColormapView` ([sensor/colormap_view.py](sensor/colormap_view.py))

- `matplotlib.figure.Figure` + `FigureCanvasTkAgg`
- `imshow(frame, cmap='viridis', vmin=0, vmax=255, aspect='equal')` (**범위 0–255 고정**, **셀 정사각형**)
- `update(frame)`: shape 변경 감지 → `set_data` + `set_extent` (회전으로 rows/cols가 바뀌어도 자동 처리)
- `set_cmap(name)`: 런타임 colormap 변경 (`viridis / gray / jet / inferno / magma`)

### 6.5.2 Rotation

- `App.rotation_k ∈ {0,1,2,3}` 상태 보유 (90° 단위)
- **↻ CW**: `k = (k - 1) % 4`, **↺ CCW**: `k = (k + 1) % 4`
- 매 tick에서 `np.rot90(display, k=rotation_k)`으로 회전 후 view 업데이트
- 회전 직후 마지막 프레임을 즉시 재렌더 (신규 프레임 대기 없이 반영)
- **표시 전용** — CSV는 원본(row-major) 순서로 저장

### 6.5.1 Calibration

- `App`이 `baseline: np.ndarray | None` 상태 보유 (shape `rows × cols`, dtype `int16`)
- **Calibration 버튼**: 다음 도착 프레임(또는 직전 프레임)을 캡처하여 `baseline = frame.astype(int16)` 저장
- 매 tick 처리:
  ```python
  display = frame.astype(np.int16)
  if baseline is not None:
      display = np.clip(display - baseline, 0, 255).astype(np.uint8)
  ```
- **표시 + CSV 기록 모두 보정 후 값 사용** (raw 저장이 필요하면 옵션화 가능)
- **Reset Calibration**: `baseline = None`
- 해상도(cols/rows) 변경 시 baseline 자동 무효화

### 6.6 `App` ([sensor/app.py](sensor/app.py))

Tk 윈도우 레이아웃:

```
┌──────────────────────────────────────────────────────────────────┐
│ Sensor Recorder                                                   │
│ ┌─ Serial ───────────────┐ ┌─ Packet ──────────────────────────┐ │
│ │ Port  [COM3 ▼] Refresh │ │ Cols [32]     Rows [64]           │ │
│ │ Baud Rate [921600]     │ │ Header [A55A01060801]             │ │
│ │ Data bits  8-bit(fixed)│ │ Colormap [jet ▼]                  │ │
│ └────────────────────────┘ └───────────────────────────────────┘ │
│ ┌─ Recording ──────────────────────────────────────────────────┐ │
│ │ CSV file [/path/to/out.csv]                        [Browse]  │ │
│ └──────────────────────────────────────────────────────────────┘ │
│ [Connect] [Disconnect]  [● Start Rec.] [■ Stop Rec.]             │
│ [Calibrate] [Reset Cal.]  [↺ CCW] [↻ CW]                         │
│ ● Connected      FPS  28.4   Rec  ●   Cal  ●                     │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│              Colormap (imshow, viridis, 0–255)                   │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

**두 개의 독립 상태 머신**:

| 상태 | 버튼                   | 효과                               |
| ---- | ---------------------- | ---------------------------------- |
| 연결 | Connect / Disconnect   | 시리얼 스트림 + 실시간 표시 on/off |
| 기록 | Start Rec. / Stop Rec. | CSV 쓰기 on/off (연결 중에만 가능) |

- **Refresh**: `serial.tools.list_ports.comports()`로 포트 콤보박스 갱신
- **Connect**: 입력값 검증 → `FrameParser`, `SerialReader` 시작 → `after(33, tick)` 시작
- **Disconnect**: `SerialReader.stop()`, 큐 비우기, 기록 중이면 자동 Stop Recording
- **Start Recording**: 파일명 검증 → `CsvLogger` 오픈 (연결 안 되어 있으면 비활성)
- **Stop Recording**: `CsvLogger.close()`
- `tick()`: 큐에서 최신 프레임 drain → view 업데이트, 기록 중이면 CSV 쓰기 → 다음 `after` 예약
- 자동 재연결 중에는 status 라벨에 "Reconnecting..." 표시, 기록은 그대로 유지(재연결 후 이어서 기록)

## 7. 데이터 흐름

1. 사용자: 포트/해상도/헤더 입력 → **Connect**
2. `SerialReader` 스레드: 포트 열기 → 바이트 수신 → `parser.feed()` (실패 시 자동 재시도)
3. `FrameParser`: 헤더 동기화 → 체크섬 검증 통과 시 프레임을 큐에 push (불일치 패킷은 폐기)
4. GUI `tick()` (≈30 Hz): 큐에서 프레임 pop → `ColormapView.update()` + (기록 중이면) `CsvLogger.write()`
5. (선택) **Start Recording** → CSV 파일 오픈 → tick에서 자동 기록
6. **Stop Recording** → CSV close (연결은 유지)
7. **Disconnect** → 스레드 정지, 기록 중이면 자동 close

## 8. 의존성 (`requirements.txt`)

```
pyserial
numpy
matplotlib
```

(Tkinter는 표준 라이브러리)

### 8.1 배포 빌드 (PyInstaller)

- 빌드 의존성: `pip install pyinstaller`
- spec 파일: [SensorRecorder.spec](SensorRecorder.spec)
  - `hiddenimports`: `serial.tools.list_ports_*` 플랫폼별 백엔드
  - `datas`: `matplotlib` 데이터 파일 자동 수집
  - `excludes`: PyQt/scipy/pandas 등 미사용 대용량 패키지 제외
  - `console=False`, `windowed` 빌드 (GUI 전용)
- 빌드 명령:
  ```bash
  pyinstaller SensorRecorder.spec            # 최초/재빌드
  pyinstaller SensorRecorder.spec --clean    # 캐시 정리 후 빌드
  ```
- 결과물: `dist/SensorRecorder/` 폴더 (실행 파일 + 런타임). 이 폴더를 통째로 배포.
- **플랫폼별로 각자 빌드** 해야 함 (Windows exe는 Windows에서, Linux 바이너리는 Linux에서).
- 아이콘 지정: spec의 `EXE(..., icon='sensor.ico')` 추가.

## 9. 마일스톤

1. [ ] `config.py` + `requirements.txt` 셋업
2. [ ] `FrameParser` 구현 + 단위 테스트 (가짜 바이트 스트림)
3. [ ] `SerialReader` + 콘솔 dump 검증
4. [ ] `ColormapView` 단독 데모 (랜덤 데이터)
5. [ ] `CsvLogger` 구현
6. [ ] 통합 `app.py` (Start/Stop, 입력 검증, FPS 표시)
7. [ ] 장시간 안정성 / 프레임 drop 통계

## 10. 결정 사항 (확정 완료)

- 페이로드 픽셀 순서: **row-major** (`reshape(rows, cols)`)
- Colormap 범위: **0–255 고정**
- 시리얼 자동 재연결: **필요** (`SerialReader` 내부 루프)
- 연결/기록 버튼 분리: **Connect/Disconnect** + **Start Recording/Stop Recording**
- COM 포트 선택 + **Refresh** 버튼

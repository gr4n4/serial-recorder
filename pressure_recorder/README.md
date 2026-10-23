# pressure_recorder

매트리스 압력 센서의 데이터를 시리얼 포트로 수신하여 실시간 colormap으로 표시하고 CSV로 저장하는 프로그램.
**GUI** (`main.py`)와 **CLI 도구 모음** (`cmd/`)을 함께 제공합니다.

## Install & Run

```bash
git clone <repo-url> pressure_recorder_repo
cd pressure_recorder_repo/pressure_recorder
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# GUI 실행 (리포 루트가 필요하므로 pressure_recorder/ 안에서 실행)
python main.py

# CLI 헤드리스 녹화
python cmd/start.py
```

> `sensor/frame_parser.py`, `sensor/serial_reader.py`는 `../common/`으로 옮겨져 [risk_monitor](../risk_monitor)의 client와 공유됩니다. `main.py`, `cmd/*.py`, `tests/*.py`는 실행/임포트 시 리포 루트를 자동으로 `sys.path`에 추가하므로 별도 설정 없이 동작합니다.

## Requirements

- Python 3.10+
- Tkinter (`sudo apt install python3-tk` on Debian/Ubuntu)
- 시리얼 포트 접근 권한: `sudo usermod -aG dialout $USER` 후 재로그인
- (선택) `--upload` 사용 시 wandb: `pip install wandb && wandb login`

## Features

### GUI ([main.py](main.py))

- 시리얼 포트 자동 탐지 + 새로고침, 자동 재연결
- 실시간 2D colormap 디스플레이 (viridis / gray / jet / inferno / magma)
- 프레임 단위 CSV 저장 (한 행 = 한 프레임, ISO 타임스탬프 포함)
- Calibration (현재 프레임을 baseline으로 차감)
- 디스플레이 90° 단위 회전 (CW / CCW)
- 다크 테마 Tk GUI

### CLI ([cmd/](cmd/))

| 스크립트                                 | 용도                                                |
| ---------------------------------------- | ---------------------------------------------------- |
| [cmd/start.py](cmd/start.py)             | 헤드리스 녹화 (선택적 wandb 업로드, 날짜 기반 회전) |
| [cmd/display.py](cmd/display.py)         | 저장된 CSV를 matplotlib 애니메이션으로 재생         |
| [cmd/calibration.py](cmd/calibration.py) | baseline 프레임을 측정해 CSV로 저장                 |

자세한 사용법은 [cmd/README.md](cmd/README.md) 참고.

## Packet Format

```
offset  size   field
0       6      HEADER    (A5 5A 01 06 08 01)
6       N      PAYLOAD   (cols × rows bytes, 8-bit unsigned, row-major)
6+N     2      CHECKSUM  (little-endian uint16, 앞의 모든 바이트(헤더 포함)의 합 & 0xFFFF)
```

기본값: `cols=32, rows=64, baud=921600` (패킷 전체 2056 bytes). GUI에서 cols/rows/header 변경 가능.

체크섬이 맞지 않거나 길이가 부족한 패킷은 버려지고 다음 헤더에서 재동기화합니다 (`FrameParser.checksum_errors`로 집계).

## GUI Controls

| 버튼                          | 동작                                           |
| ------------------------------ | ----------------------------------------------- |
| Connect / Disconnect          | 시리얼 연결 토글 (연결만으로 실시간 표시 동작) |
| Start / Stop Recording        | CSV 기록 토글                                  |
| Calibrate / Reset Calibration | 현재 프레임을 baseline으로 저장 / 해제         |
| ↺ CCW / ↻ CW                  | 디스플레이 90° 회전 (CSV에는 영향 없음)        |
| Refresh                       | 시리얼 포트 목록 갱신                          |

## CSV Format

```
timestamp,               c0, c1, c2, ..., c{rows*cols-1}
2026-04-13T14:23:45.123, 0,  5,  12, ..., 0
```

- 한 행 = 한 프레임 (row-major flatten)
- Calibration 활성 시 보정값이 저장됨

## CLI 핵심 동작 (요약)

[cmd/start.py](cmd/start.py)는 다음을 수행합니다:

- 기본 저장 경로: `~/sensor_logs/log_<YYYYMMDD_HHMMSS>.csv` (첫 파일)
- 시스템 날짜가 바뀌면 자동으로 `log_<YYYYMMDD>.csv`로 회전 (충돌 시 `(2)`, `(3)` …)
- `--upload` 사용 시 wandb로 실시간 메트릭(min/max/mean) + CSV 아티팩트 업로드
- 네트워크 단절 회복력:
  - init 실패 시 30초 간격 재시도
  - 장기 단절 시 자동 offline 모드 전환 (RAM 보호)
  - 업로드 실패 파일은 큐에 보관 후 복구 시 일괄 재시도
- `--dry-run`: 시리얼 장비 없이 합성 프레임으로 동작 검증

## Project Structure

```
pressure_recorder/
├── main.py                  # GUI 진입점
├── requirements.txt
├── DESIGN.md                 # 설계 문서
├── sensor/
│   ├── config.py             # GUI 기본값 상수
│   ├── csv_logger.py         # CSV 저장
│   ├── colormap_view.py      # matplotlib 디스플레이
│   └── app.py                # Tk GUI (../common/frame_parser.py, serial_reader.py 사용)
├── cmd/
│   ├── start.py               # CLI 녹화 (wandb upload, dry-run)
│   ├── display.py             # CSV 재생
│   ├── calibration.py         # baseline 측정
│   └── README.md              # CLI 상세 사용법
└── tests/
    ├── test_frame_parser.py
    └── test_dry_run.py        # 회귀 테스트
```

공유 모듈(`../common/frame_parser.py`, `../common/serial_reader.py`, `../common/config.py`)은 [risk_monitor/client](../risk_monitor/client)와 함께 씁니다.

## Tests

```bash
pip install pytest
python -m pytest tests/ -v
```

## License

TBD

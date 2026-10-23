import re
import sys
import time

import serial
import serial.tools.list_ports

# =========================
# Serial 설정
# =========================
PORT = "COM5"       # 실제 COM 포트로 변경
BAUDRATE = 921600

OPEN_WAIT_SEC = 2.0       # 포트 오픈 후 명령 전송 전 대기 시간 (1~2초)
COLLECT_SEC = 1.0         # 명령 전송 후 RX 수집 시간
IDLE_CHECK_SEC = 0.5      # 스트림 중지 확인용 추가 수신 시간
READ_CHUNK_TIMEOUT = 0.5  # 개별 read 타임아웃 (0.5~1초)


def check_com_port(port):
    """Windows에서 실제 COM 포트(COM3, COM4 ...)인지 확인"""
    if not re.fullmatch(r"COM\d+", port.upper()):
        print(f"오류: '{port}'는 COM 포트 형식(COM3, COM4 등)이 아닙니다.")
        return False

    available = [p.device.upper() for p in serial.tools.list_ports.comports()]
    if port.upper() not in available:
        print(f"오류: {port} 포트를 찾을 수 없습니다. 사용 가능한 포트: {available or '없음'}")
        return False

    return True


if not check_com_port(PORT):
    sys.exit(1)

ser = serial.Serial(
    port=PORT,
    baudrate=BAUDRATE,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_ONE,
    timeout=READ_CHUNK_TIMEOUT
)

# 포트를 연 직후에는 바로 명령을 보내지 않고 대기
print(f"{PORT} 오픈 완료. {OPEN_WAIT_SEC}초 대기...")
time.sleep(OPEN_WAIT_SEC)

# 대기 중 쌓인 스트림 데이터 제거
ser.reset_input_buffer()
ser.reset_output_buffer()


def read_for(duration):
    """in_waiting / 길이에 의존하지 않고 duration 동안 수신된 모든 바이트를 수집"""
    buf = bytearray()
    end = time.monotonic() + duration
    while time.monotonic() < end:
        chunk = ser.read(4096)  # timeout 만료 시 빈 bytes 반환
        if chunk:
            buf += chunk
    return bytes(buf)


def send_command(name, command_hex, expected_hex):
    """HEX 명령 전송 후 timeout 기준으로 전체 RX 수집, expected 시퀀스 검색"""

    # 기존 수신 버퍼 제거
    ser.reset_input_buffer()

    command = bytes.fromhex(command_hex)
    expected = bytes.fromhex(expected_hex)

    print(f"\n[{name}]")
    print(f"TX : {command.hex(' ').upper()}")

    # 명령 전송
    ser.write(command)
    ser.flush()

    # 응답 수신 (고정 시간 동안 전체 수집)
    response = read_for(COLLECT_SEC)

    # 데이터 스트림이 실제로 멈췄는지 확인 (추가 수신 여부)
    tail = read_for(IDLE_CHECK_SEC)
    stream_stopped = len(tail) == 0
    response += tail

    # print(f"RX ({len(response)} bytes) : {response.hex(' ').upper()}")
    print(f"스트림 상태: {'중지됨' if stream_stopped else '아직 수신 중'}")

    # 수신된 전체 버퍼에서 expected 시퀀스 검색
    pos = response.find(expected)
    found = pos >= 0
    print(f"수신 총 {len(response)} bytes")
    if found:
        print(f"결과: 정상 (응답 시퀀스 확인, 오프셋 {pos}, 이후 {len(response) - pos - len(expected)} bytes)")
    else:
        print("결과: 응답 불일치")
        # print(f"기대: {expected.hex(' ').upper()}")

    return found, stream_stopped


try:
    # 1. 데이터 전송 중지 (스트림이 멈출 때까지 재시도)
    for attempt in range(1, 10):
        found, stopped = send_command(
            f"데이터 전송 중지 (시도 {attempt})",
            "A55AABBAFF00",
            "A55AABBAFF"
        )
        if found and stopped:
            break
    else:
        print("경고: 데이터 스트림 중지를 확인하지 못했습니다.")

    # # 2. 이미지 방향 270도 회전
    # send_command(
    #     "이미지 방향 270도 회전",
    #     "A55A0FF08403",
    #     "A55A0FF084"
    # )

    # # 3. 설정 저장
    # send_command(
    #     "설정 저장",
    #     "A55A0FF0FE",
    #     "A55A0FF0FE"
    # )

    print("\n모든 설정이 완료되었습니다.")

finally:
    ser.close()
    print("\nSerial port closed.")

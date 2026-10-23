import re
import sys
from collections import Counter

MARKER = "A55A01060801"


def split_packets(hex_text: str, marker: str = MARKER) -> list[str]:
    """공백/개행을 제거한 HEX 문자열을 marker 기준으로 분할한다.

    각 패킷은 marker로 시작한다. 첫 marker 이전의 데이터(잡음)는 첫 항목으로
    따로 반환되지 않고 버려진다.
    """
    hex_text = re.sub(r"\s+", "", hex_text).upper()
    parts = hex_text.split(marker)
    # parts[0]은 첫 marker 이전 데이터이므로 제외
    return [marker + p for p in parts[1:]]


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "data.txt"
    dst = sys.argv[2] if len(sys.argv) > 2 else "data_split.txt"

    with open(src, "r", encoding="utf-8", errors="ignore") as f:
        packets = split_packets(f.read())

    with open(dst, "w", encoding="utf-8") as f:
        for p in packets:
            # 바이트 단위로 공백 구분하여 한 줄에 한 패킷
            f.write(" ".join(p[i:i + 2] for i in range(0, len(p), 2)) + "\n")

    lengths = Counter(len(p) // 2 for p in packets)
    print(f"패킷 수: {len(packets)}")
    print("패킷 길이(byte)별 개수:", dict(sorted(lengths.items())))
    print(f"저장: {dst}")


if __name__ == "__main__":
    main()

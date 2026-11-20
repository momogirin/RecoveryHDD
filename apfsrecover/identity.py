"""디스크 식별(지문). 검사했던 디스크와 같은 디스크인지 확인해서, 다른 디스크에서 엉뚱한 데이터를
꺼내는 사고를 막는다. 장치 번호(disk4/disk5)는 다시 꽂으면 바뀌므로 번호로는 판단하지 않는다."""
import hashlib

from .blockdev import ReadOnlyDevice

HEAD = 64 * 1024


def fingerprint(path, size):
    """디스크 앞부분 64KB 와 크기의 해시. 읽을 수 없으면 OSError."""
    with ReadOnlyDevice(path) as d:
        head = d.read_at(0, HEAD)
    if not head:
        return None
    return hashlib.sha256(head + b"|" + str(size).encode()).hexdigest()[:32]

"""읽기 전용 블록 장치 접근.

이 모듈은 장치를 O_RDONLY 로만 연다. write/truncate/ioctl 등 쓰기 동작은
의도적으로 제공하지 않는다.
"""
import os


class ReadOnlyDevice:
    def __init__(self, path: str):
        self.path = path
        self._fd = os.open(path, os.O_RDONLY)

    SECTOR = 512

    def read_at(self, offset: int, length: int) -> bytes:
        """offset 에서 length 바이트를 읽는다. 짧게 읽히면 있는 만큼만 반환.

        raw 장치는 섹터 정렬 읽기만 허용하므로 내부에서 정렬해 읽고 잘라 낸다.
        """
        a = offset - offset % self.SECTOR
        end = offset + length
        aend = (end + self.SECTOR - 1) // self.SECTOR * self.SECTOR
        out = bytearray()
        while len(out) < aend - a:
            chunk = os.pread(self._fd, aend - a - len(out), a + len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out[offset - a:offset - a + length])

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

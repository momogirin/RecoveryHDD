"""읽기 전용 블록 장치 접근.

이 모듈은 장치를 O_RDONLY 로만 연다. write/truncate/ioctl 등 쓰기 동작은
의도적으로 제공하지 않는다.
"""
import os


class ReadOnlyDevice:
    def __init__(self, path: str):
        self.path = path
        self._fd = os.open(path, os.O_RDONLY)

    def read_at(self, offset: int, length: int) -> bytes:
        """offset 에서 length 바이트를 읽는다. 짧게 읽히면 있는 만큼만 반환."""
        out = bytearray()
        while len(out) < length:
            chunk = os.pread(self._fd, length - len(out), offset + len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

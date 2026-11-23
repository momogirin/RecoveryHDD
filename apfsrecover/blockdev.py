"""읽기 전용 블록 장치 접근.

이 모듈은 장치를 O_RDONLY 로만 연다. write/truncate/ioctl 등 쓰기 동작은
의도적으로 제공하지 않는다.

Windows: 장치는 `\\\\.\\PhysicalDriveN` (디스크 전체)로만 열 수 있고 파티션은 따로 열 수 없다.
그래서 `\\\\.\\PhysicalDrive2@210763776` 처럼 `@파티션 시작 바이트` 를 붙이면 그 위치를 0 으로 보고 읽는다.
raw 디스크는 4096 바이트 정렬 읽기만 허용될 수 있어(4Kn 디스크) 정렬 단위를 4096 으로 한다.
"""
import errno
import os
import threading

RAW_PREFIX = "\\\\.\\"
MAX_READ = 16 << 20


# Windows 오류 코드: 21 장치 준비 안 됨, 55 장치 없음, 483 컨트롤러 오류, 1117 I/O 장치 오류, 1167 장치 연결 안 됨
WIN_GONE = {21, 55, 483, 1117, 1167}


def device_gone(e):
    """읽기 중 난 오류가 '장치가 사라짐(케이블 분리 등)'인지. 맥/리눅스는 errno, 윈도우는 winerror 로 본다.
    윈도우에서 파이썬이 winerror 를 채워 주지 않는 경우(C 런타임 오류)는 구분할 수 없으므로 False 이고,
    그때는 연속 실패 중단 규칙이 대신 멈춘다(실제 분리 시험은 아직 못 했다)."""
    return e.errno in (errno.ENXIO, errno.ENODEV) or getattr(e, "winerror", None) in WIN_GONE


def split_device_path(path: str):
    """`\\\\.\\PhysicalDriveN@오프셋` 을 (경로, 오프셋)으로 나눈다. 그 외 경로는 (경로, 0)."""
    if path.startswith(RAW_PREFIX) and "@" in path:
        base, _, off = path.rpartition("@")
        if off.isdigit():
            return base, int(off)
    return path, 0


class ReadOnlyDevice:
    SECTOR = 512

    def __init__(self, path: str, base=None, align=None):
        self.path, parsed_base = split_device_path(path)
        self._base = parsed_base if base is None else base
        self._align = align or (4096 if self.path.startswith(RAW_PREFIX) else self.SECTOR)
        # Windows 는 O_BINARY 가 없으면 텍스트 모드로 열려 바이트가 변한다
        self._fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        self._lock = threading.Lock()  # os.pread 가 없는 OS(Windows)에서 seek+read 를 묶는다

    def _pread(self, n: int, pos: int) -> bytes:
        n = min(n, MAX_READ)
        if hasattr(os, "pread"):
            return os.pread(self._fd, n, pos)
        with self._lock:
            os.lseek(self._fd, pos, os.SEEK_SET)
            return os.read(self._fd, n)

    def read_at(self, offset: int, length: int) -> bytes:
        """offset 에서 length 바이트를 읽는다. 짧게 읽히면 있는 만큼만 반환.

        raw 장치는 섹터 정렬 읽기만 허용하므로 내부에서 정렬해 읽고 잘라 낸다.
        """
        pos = self._base + offset
        a = pos - pos % self._align
        end = pos + length
        aend = (end + self._align - 1) // self._align * self._align
        out = bytearray()
        while len(out) < aend - a:
            chunk = self._pread(aend - a - len(out), a + len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out[pos - a:pos - a + length])

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

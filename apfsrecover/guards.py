"""저장 위치 가드 (절대 규칙 2, 3). 문제가 있으면 사람이 읽을 이유 목록을 돌려준다."""
import os
import shutil

from .ownership import expand
from .scan import REPO

SYSTEM_PREFIXES = ("/System", "/usr", "/bin", "/sbin", "/etc", "/private/etc", "/Library",
                   "/Applications", "/dev", "/var/db")
WINDOWS_PREFIXES = ("c:\\windows", "c:\\program files", "c:\\programdata")
MIN_MARGIN = 5 * 1000**3


def _under(path, base):
    base = os.path.realpath(base)
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def margin_for(need_bytes):
    return max(MIN_MARGIN, int(need_bytes * 0.1))


def check_dest(dest, forbidden=(), need_bytes=0):
    """반환: {"ok", "reasons", "free", "need", "margin", "dest"}"""
    reasons = []
    if not dest or not str(dest).strip():
        return {"ok": False, "reasons": ["저장 폴더를 입력하세요."], "free": 0, "need": need_bytes,
                "margin": 0, "dest": ""}
    real = os.path.realpath(expand(str(dest).strip()))
    if _under(real, REPO):
        reasons.append("프로젝트(저장소) 폴더 안에는 저장할 수 없습니다.")
    if real == os.sep or os.path.splitdrive(real)[1] in ("", "\\", "/"):  # 어느 드라이브든 최상위
        reasons.append("디스크 최상위에는 저장할 수 없습니다.")
    if any(_under(real, p) for p in SYSTEM_PREFIXES) or real.lower().startswith(WINDOWS_PREFIXES):
        reasons.append("시스템 폴더에는 저장할 수 없습니다.")
    for f in forbidden:
        if f and _under(real, f):
            reasons.append("원본 디스크 안에는 저장할 수 없습니다.")
            break
    probe = real
    while not os.path.exists(probe) and probe != os.path.dirname(probe):
        probe = os.path.dirname(probe)
    free = 0
    if os.path.isdir(probe):
        if not os.access(probe, os.W_OK):
            reasons.append("이 폴더에는 쓸 수 없습니다(권한).")
        free = shutil.disk_usage(probe).free
    else:
        reasons.append("저장 폴더의 상위 경로를 찾을 수 없습니다.")
    margin = margin_for(need_bytes)
    if free < need_bytes + margin:
        reasons.append("여유 공간이 부족합니다. 필요 %.1f GB(마진 포함), 여유 %.1f GB. 묶음 크기를 줄이세요."
                       % ((need_bytes + margin) / 1e9, free / 1e9))
    return {"ok": not reasons, "reasons": reasons, "free": free, "need": need_bytes,
            "margin": margin, "dest": real}

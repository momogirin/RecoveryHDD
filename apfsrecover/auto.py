"""한 번에 실행하는 읽기 전용 진단: inspect → survey → 전체 NXSB/APSB 스캔.

결과는 outdir(저장소 밖)에 저장하고, sudo 로 실행된 경우 파일 소유자를
원래 사용자로 되돌린다.
"""
import contextlib
import io
import os
import shutil
import sys

from .__main__ import inspect
from .blockdev import ReadOnlyDevice
from .nxsb import parse_nxsb
from .scan import BS, REPO, scan
from .survey import survey


def _chown_tree(path):
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not uid:
        return
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            os.chown(os.path.join(root, name), int(uid), int(gid))
    os.chown(path, int(uid), int(gid))


def auto(path: str, outdir: str) -> int:
    outdir = os.path.realpath(os.path.expanduser(outdir))
    if outdir.startswith(REPO + os.sep) or outdir == REPO:
        print("outdir 가 저장소 안입니다."); return 2
    os.makedirs(outdir, exist_ok=True)
    if shutil.disk_usage(outdir).free < 1 << 30:
        print("여유 공간 1GB 미만"); return 2
    with ReadOnlyDevice(path) as dev:
        sb = parse_nxsb(dev.read_at(0, 4096))
    total = sb["block_count"] * sb["block_size"]
    try:
        with open(os.path.join(outdir, "1_inspect.txt"), "w") as f, contextlib.redirect_stdout(f):
            inspect(path)
        print("1/3 inspect 완료", flush=True)
        with open(os.path.join(outdir, "2_survey.txt"), "w") as f, contextlib.redirect_stdout(f):
            survey(path, total, 128, 250)
        print("2/3 survey 완료 → 2_survey.txt", flush=True)
        r = scan(path, 0, sb["block_count"], os.path.join(outdir, "3_scan_full.jsonl"))
        print("3/3 scan 완료", flush=True)
    finally:
        _chown_tree(outdir)
    return r

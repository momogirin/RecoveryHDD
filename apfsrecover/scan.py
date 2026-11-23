"""디스크에서 옛 NXSB(컨테이너)·APSB(볼륨) 슈퍼블록을 찾는 읽기 전용 스캔.

4096 바이트 정렬 블록의 오프셋 32 에 매직이 있고 Fletcher-64 체크섬이 맞는
것만 기록한다. 결과는 JSON Lines 로 저장한다(저장소 밖 경로만 허용).
"""
import errno
import json
import os
import shutil
import struct
import sys
import time

from .blockdev import ReadOnlyDevice, device_gone
from .nxsb import fletcher64_ok, obj_header, parse_nxsb

BS = 4096
CHUNK = 8 * 1024 * 1024
MAGICS = {b"NXSB": "NXSB", b"APSB": "APSB"}
REPO = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))


def parse_apsb(block: bytes) -> dict:
    h = obj_header(block)
    fs_index, features, ro, incompat = struct.unpack_from("<IQQQ", block, 36)
    omap_oid, root_tree_oid = struct.unpack_from("<QQ", block, 128)
    num_files, num_dirs = struct.unpack_from("<QQ", block, 184)
    fs_flags = struct.unpack_from("<Q", block, 264)[0]
    name = block[704:960].split(b"\0")[0].decode("utf-8", "replace")
    return {**h, "fs_index": fs_index, "incompat": incompat, "omap_oid": omap_oid,
            "root_tree_oid": root_tree_oid, "num_files": num_files,
            "num_directories": num_dirs, "fs_flags": fs_flags,
            "unencrypted_flag": bool(fs_flags & 1), "volname": name}


def scan(path: str, start_block: int, end_block: int, out_path: str) -> int:
    out_path = os.path.realpath(os.path.expanduser(out_path))
    if out_path.startswith(REPO + os.sep):
        print("출력 경로가 저장소 안입니다. 저장소 밖을 지정하세요."); return 2
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if shutil.disk_usage(os.path.dirname(out_path)).free < 1 << 30:
        print("출력 위치의 여유 공간이 1GB 미만입니다."); return 2
    found = 0
    consecutive_errors = 0
    t0 = time.time()
    with ReadOnlyDevice(path) as dev, open(out_path, "w", encoding="utf-8") as out:
        pos = start_block * BS
        end = end_block * BS
        while pos < end:
            n = min(CHUNK, end - pos)
            try:
                buf = dev.read_at(pos, n)
            except OSError as e:
                print("\n읽기 오류 offset %d: %s" % (pos, e), flush=True)
                out.write(json.dumps({"kind": "READ_ERROR", "offset": pos, "error": str(e)}) + "\n")
                consecutive_errors += 1
                if device_gone(e) or consecutive_errors >= 5:
                    msg = "장치 연결이 끊긴 것으로 보여 중단합니다. 재연결 후 시작 블록=%d 로 이어서 스캔하세요." % (pos // BS)
                    print(msg, flush=True)
                    out.write(json.dumps({"kind": "ABORTED", "resume_block": pos // BS}) + "\n")
                    return 4
                pos += n
                continue
            consecutive_errors = 0
            if not buf:
                print("\n읽기 종료/실패: offset %d" % pos); break
            for magic, kind in MAGICS.items():
                i = buf.find(magic)
                while i != -1:
                    if i >= 32 and (i - 32) % BS == 0 and i - 32 + BS <= len(buf):
                        blk = buf[i - 32:i - 32 + BS]
                        if fletcher64_ok(blk):
                            blkno = (pos + i - 32) // BS
                            rec = parse_nxsb(blk) if kind == "NXSB" else parse_apsb(blk)
                            rec = {k: v for k, v in rec.items() if not isinstance(v, bytes)}
                            rec.update(kind=kind, block=blkno)
                            out.write(json.dumps(rec) + "\n"); out.flush()
                            found += 1
                            print("\n[%s] 블록 %d xid=%d %s" % (
                                kind, blkno, rec["xid"], rec.get("volname", "")))
                    i = buf.find(magic, i + 1)
            pos += len(buf)
            el = time.time() - t0
            print("\r%.1f GB 스캔, %.0f MB/s, 발견 %d" % (
                (pos - start_block * BS) / 1e9, (pos - start_block * BS) / 1e6 / max(el, 1e-9), found),
                end="", flush=True)
    print("\n완료. 결과: %s (발견 %d)" % (out_path, found))
    return 0

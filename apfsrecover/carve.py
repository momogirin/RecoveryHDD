"""시그니처 기반 추출(2순위). 읽기 전용으로 디스크를 읽고, 결과 파일은 저장소 밖에만 쓴다.

JPEG: SOI(FFD8FF)에서 시작해 마커 구조를 따라가 EOI(FFD9)까지의 정확한 길이를 구한다.
MP4/MOV: 'ftyp' 박스에서 시작해 최상위 박스 크기를 따라가며 길이를 구한다(moov+mdat 필요).
파일이 조각나 있으면 구조가 어긋나므로 길이 계산에 실패하고 건너뛴다.
"""
import json
import os
import re
import shutil
import struct

from .blockdev import ReadOnlyDevice
from .scan import BS, CHUNK, REPO

JPEG_READ = 48 * 1024 * 1024
MP4_TYPES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"pnot",
             b"moof", b"mfra", b"meta", b"junk", b"pdin", b"sidx", b"styp", b"prfl"}
# ftyp 메이저 브랜드 → 확장자. 'crx ' 는 Canon CR3(RAW 사진) 또는 CRM(영상)이다.
# CR3/CRM 구분은 아직 검증하지 않았으므로 우선 cr3 로 표시한다.
BRAND_EXT = {b"crx ": "cr3", b"qt  ": "mov", b"heic": "heic", b"heix": "heic",
             b"mif1": "heic", b"msf1": "heic", b"avif": "avif"}
ENTROPY_END =re.compile(rb"\xff[\x01-\xcf\xd8-\xfe]")


def jpeg_length(data: bytes):
    """data[0:] 가 JPEG 시작일 때 길이를 반환. 구조가 맞지 않으면 None."""
    if data[:3] != b"\xff\xd8\xff" or len(data) < 4 or data[3] < 0xC0:
        return None
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        m = data[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m == 0xD9:
            return i + 2
        if m in (0x01,) or 0xD0 <= m <= 0xD8:
            i += 2
            continue
        seglen = struct.unpack_from(">H", data, i + 2)[0]
        if seglen < 2:
            return None
        i += 2 + seglen
        if m == 0xDA:  # 스캔 데이터: 다음 '진짜' 마커까지 건너뜀
            mo = ENTROPY_END.search(data, i)
            if not mo:
                return None
            i = mo.start()
    return None


def mp4_length(dev: ReadOnlyDevice, off: int, max_len: int):
    """off 가 ftyp 박스 시작일 때 최상위 박스를 따라가 전체 길이 반환. 실패하면 None."""
    pos = off
    seen = set()
    for _ in range(100000):
        h = dev.read_at(pos, 16)
        if len(h) < 8:
            break
        size, typ = struct.unpack_from(">I4s", h, 0)
        if pos == off and (typ != b"ftyp" or not 16 <= size <= 128):
            return None
        if typ not in MP4_TYPES:
            break
        if size == 1:
            if len(h) < 16:
                break
            size = struct.unpack_from(">Q", h, 8)[0]
        if size < 8 or pos + size - off > max_len:
            break
        seen.add(typ)
        pos += size
    if {b"moov", b"mdat"} <= seen:
        return pos - off
    return None


def carve_sample(path, start_block, end_block, outdir, max_jpeg, max_mp4,
                 min_jpeg=200_000, max_mp4_len=1_500_000_000):
    outdir = os.path.realpath(os.path.expanduser(outdir))
    if outdir == REPO or outdir.startswith(REPO + os.sep):
        print("outdir 가 저장소 안입니다."); return 2
    os.makedirs(outdir, exist_ok=True)
    free = shutil.disk_usage(outdir).free
    need = max_jpeg * JPEG_READ // 4 + max_mp4 * max_mp4_len
    print("저장 위치 여유 %.1f GB (최악의 경우 필요 %.1f GB)" % (free / 1e9, need / 1e9))
    if free < need + (5 << 30):
        print("여유 공간이 부족합니다."); return 2
    manifest = []
    nj = nm = 0
    pos, end = start_block * BS, end_block * BS
    with ReadOnlyDevice(path) as dev:
        while pos < end and (nj < max_jpeg or nm < max_mp4):
            buf = dev.read_at(pos, min(CHUNK, end - pos))
            if not buf:
                break
            hits = []
            for sig, kind, delta in ((b"\xff\xd8\xff", "jpg", 0), (b"ftyp", "mp4", 4)):
                i = buf.find(sig)
                while i != -1:
                    if (i - delta) % BS == 0:
                        hits.append((i - delta, kind))
                    i = buf.find(sig, i + 1)
            for rel, kind in sorted(hits):
                off = pos + rel
                if kind == "jpg" and nj < max_jpeg:
                    data = dev.read_at(off, JPEG_READ)
                    ln = jpeg_length(data)
                    if ln and ln >= min_jpeg:
                        nj += 1
                        name = "jpg_%04d_blk%d.jpg" % (nj, off // BS)
                        open(os.path.join(outdir, name), "wb").write(data[:ln])
                        manifest.append({"file": name, "offset": off, "length": ln})
                        print("JPEG 추출 %s  %d bytes" % (name, ln), flush=True)
                elif kind == "mp4" and nm < max_mp4:
                    ln = mp4_length(dev, off, max_mp4_len)
                    if ln and ln >= 500_000:
                        nm += 1
                        brand = dev.read_at(off + 8, 4)
                        ext = BRAND_EXT.get(brand, "mp4")
                        name = "%s_%04d_blk%d.%s" % (ext, nm, off // BS, ext)
                        with open(os.path.join(outdir, name), "wb") as f:
                            done = 0
                            while done < ln:
                                part = dev.read_at(off + done, min(8 << 20, ln - done))
                                if not part:
                                    break
                                f.write(part); done += len(part)
                        manifest.append({"file": name, "offset": off, "length": ln})
                        print("MP4 추출 %s  %d bytes" % (name, ln), flush=True)
            pos += len(buf)
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid:
        for root, dirs, files in os.walk(outdir):
            for nme in dirs + files:
                os.chown(os.path.join(root, nme), int(uid), int(gid))
        os.chown(outdir, int(uid), int(gid))
    print("완료: JPEG %d, MP4 %d → %s" % (nj, nm, outdir))
    return 0

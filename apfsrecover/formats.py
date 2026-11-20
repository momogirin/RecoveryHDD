"""파일 형식 판별과 길이·구조 계산 (읽기 전용).

판정 원칙: 구조가 끝까지 맞으면 'ok', 일부만 맞으면 'suspect', 맞지 않으면 기록하지 않는다.
'ok' 는 구조가 맞다는 뜻이며 파일이 실제로 열린다는 보증이 아니다(열림은 verify 단계).
"""
import re
import struct
from datetime import datetime, timedelta

from .exif import parse_tiff, to_iso

ENTROPY_END = re.compile(rb"\xff[\x01-\xcf\xd8-\xfe]")
SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
BMFF_TYPES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"pnot",
              b"moof", b"mfra", b"meta", b"junk", b"pdin", b"sidx", b"styp", b"prfl"}
MP4_BRANDS = {b"mp41", b"mp42", b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"avc1", b"M4V ",
              b"MSNV", b"XAVC", b"3gp4", b"3gp5", b"3gp6", b"3g2a"}
HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"mif1", b"msf1"}
MAX_MOOV = 8 << 20
ZERO_RUN = b"\0" * 4096


def jpeg_parse(data: bytes):
    """반환: (status, length, info). status: 'ok' | 'bad' | 'short'(데이터가 모자라 판단 불가)."""
    n = len(data)
    if n < 4:
        return "short", 0, {}
    if data[:3] != b"\xff\xd8\xff" or data[3] < 0xC0:
        return "bad", 0, {}
    info = {}
    i = 2
    while True:
        if i + 2 > n:
            return "short", 0, info
        if data[i] != 0xFF:
            return "bad", 0, info
        m = data[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m == 0xD9:
            return "ok", i + 2, info
        if m == 0xD8:  # 최상위 구조에서 두 번째 SOI 는 다른 파일이 이어진 것(잘린 파일)
            return "bad", 0, info
        if m == 0x01 or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            return "short", 0, info
        seglen = struct.unpack_from(">H", data, i + 2)[0]
        if seglen < 2:
            return "bad", 0, info
        if i + 2 + seglen > n:
            return "short", 0, info
        if m in SOF and seglen >= 8 and "width" not in info:
            info["height"], info["width"] = struct.unpack_from(">HH", data, i + 5)
        if m == 0xE1 and "exif" not in info and data[i + 4:i + 10] == b"Exif\0\0":
            info["exif"] = data[i + 10:i + 2 + seglen]
        i += 2 + seglen
        if m == 0xDA:
            mo = ENTROPY_END.search(data, i)
            end = mo.start() if mo else n
            # 스캔 데이터 안에 4096바이트 이상의 0 이 이어지면 지워졌거나 잘린 파일로 본다
            if ZERO_RUN in data[i:end]:
                return "bad", 0, info
            if not mo:
                return "short", 0, info
            i = end


def jpeg_probe(dev, off, cap=64 << 20, first=4 << 20):
    """JPEG 길이를 구한다. 필요한 만큼만 점점 늘려 읽는다. 반환: (length, info) 또는 None."""
    size = first
    while True:
        data = dev.read_at(off, size)
        status, length, info = jpeg_parse(data)
        if status == "ok":
            return length, info
        if status == "bad" or len(data) < size or size >= cap:
            return None
        size *= 2


def bmff_probe(dev, off, max_len):
    """ftyp 박스에서 시작해 최상위 박스를 따라간다. 반환 dict 또는 None."""
    pos = off
    seen = []
    brand = None
    moov = None
    for _ in range(100000):
        h = dev.read_at(pos, 16)
        if len(h) < 8:
            break
        size, typ = struct.unpack_from(">I4s", h, 0)
        if pos == off:
            if typ != b"ftyp" or not 16 <= size <= 128:
                return None
            brand = dev.read_at(pos + 8, 4)
        if typ not in BMFF_TYPES:
            break
        hdr = 8
        if size == 1:
            if len(h) < 16:
                break
            size = struct.unpack_from(">Q", h, 8)[0]
            hdr = 16
        if size < hdr or pos + size - off > max_len:
            break
        if typ == b"moov" and moov is None and size <= MAX_MOOV:
            moov = dev.read_at(pos + hdr, size - hdr)
        seen.append(typ)
        pos += size
    if not seen or brand is None:
        return None
    return {"length": pos - off, "brand": brand, "seen": set(seen), "moov": moov}


def classify_bmff(res):
    """bmff_probe 결과 → (kind, structure) 또는 None(기록하지 않음)."""
    brand, seen = res["brand"], res["seen"]
    if brand == b"crx ":
        kind, need = "raw-cr3", {b"moov", b"mdat"}   # CR3/CRM 구분은 미검증
    elif brand == b"qt  ":
        kind, need = "video-mov", {b"moov", b"mdat"}
    elif brand in HEIC_BRANDS:
        kind, need = "heic", {b"meta", b"mdat"}
    elif brand in MP4_BRANDS:
        kind, need = "video-mp4", {b"moov", b"mdat"}
    else:
        kind, need = "unknown-bmff", {b"moov", b"mdat"}
    if need <= seen:
        return kind, "ok"
    if b"mdat" in seen or b"moov" in seen or b"meta" in seen:
        return kind, "suspect"
    return None


def _box_after(buf: bytes, tag: bytes):
    i = buf.find(tag)
    while i != -1:
        if i >= 4:
            size = struct.unpack_from(">I", buf, i - 4)[0]
            if 8 <= size <= len(buf) - (i - 4):
                return buf[i + 4:i - 4 + size]
        i = buf.find(tag, i + 1)
    return None


def bmff_meta(kind, moov):
    """moov 바이트에서 촬영일/카메라를 읽는다. 읽지 못하면 빈 dict."""
    meta = {}
    if not moov:
        return meta
    if kind == "raw-cr3":
        for tag in (b"CMT1", b"CMT2"):
            body = _box_after(moov, tag)
            if body:
                for k, v in parse_tiff(body).items():
                    meta.setdefault(k, v)
    i = moov.find(b"mvhd")
    if i != -1 and kind in ("video-mp4", "video-mov") and i + 16 <= len(moov):
        ver = moov[i + 4]
        try:
            secs = struct.unpack_from(">I" if ver == 0 else ">Q", moov, i + 8)[0]
            t = datetime(1904, 1, 1) + timedelta(seconds=secs)
            if t.year >= 1990:
                if "taken" not in meta:
                    meta["taken"] = t.strftime("%Y-%m-%dT%H:%M:%S")
                    meta["taken_utc"] = True  # mvhd 는 UTC
        except (struct.error, OverflowError):
            pass
    return meta


def jpeg_meta(info):
    meta = {}
    if info.get("exif"):
        meta.update(parse_tiff(info["exif"]))
    if "width" in info:
        meta["width"], meta["height"] = info["width"], info["height"]
    return meta

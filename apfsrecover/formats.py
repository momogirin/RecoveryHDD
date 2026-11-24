"""파일 형식 판별과 길이·구조 계산 (읽기 전용).

판정 원칙: 구조가 끝까지 맞으면 'ok', 일부만 맞으면 'suspect', 맞지 않으면 기록하지 않는다.
'ok' 는 구조가 맞다는 뜻이며 파일이 실제로 열린다는 보증이 아니다(열림은 verify 단계).
"""
import re
import struct
import zlib
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


# ---- ZIP (docx/xlsx/pptx 포함) ----
ZIP_OFFICE = (("word/", "docx"), ("xl/", "xlsx"), ("ppt/", "pptx"))
ZIP_MAX_ENTRIES = 2_000_000


def _zip64(extra, want_usize, want_csize):
    """zip64 추가 필드(0x0001)에서 (usize, csize) 를 읽는다. 없으면 None."""
    i = 0
    while i + 4 <= len(extra):
        tag, ln = struct.unpack_from("<HH", extra, i)
        if tag == 1:
            body, k, out = extra[i + 4:i + 4 + ln], 0, {}
            if want_usize and k + 8 <= len(body):
                out["u"] = struct.unpack_from("<Q", body, k)[0]; k += 8
            if want_csize and k + 8 <= len(body):
                out["c"] = struct.unpack_from("<Q", body, k)[0]
            return out
        i += 4 + ln
    return {}


def _deflate_end(dev, start, limit):
    """data descriptor 를 쓰는 deflate 항목의 압축 데이터 길이를 zlib 으로 찾는다. 실패하면 None."""
    d = zlib.decompressobj(-15)
    pos, total = start, 0
    while pos < limit:
        chunk = dev.read_at(pos, min(4 << 20, limit - pos))
        if not chunk:
            return None
        try:
            d.decompress(chunk)
        except zlib.error:
            return None
        if d.eof:
            return total + len(chunk) - len(d.unused_data)
        total += len(chunk)
        pos += len(chunk)
    return None


def zip_probe(dev, off, max_len):
    """ZIP 을 앞에서부터 따라가 끝(중앙 디렉터리+EOCD)까지의 길이를 구한다.
    반환: {"length","entries","complete","names"} 또는 None(zip 이 아님).
    complete=True 는 EOCD 까지 찾았고 항목 수가 맞다는 뜻이다."""
    pos, limit = off, off + max_len
    local = cd = 0
    names = []
    complete = False
    for _ in range(ZIP_MAX_ENTRIES):
        h = dev.read_at(pos, 46)
        if len(h) < 4:
            break
        sig = h[:4]
        if sig == b"PK" and cd == 0 and len(h) >= 30:
            ver, flags, method, _, _, _, csize, usize, nlen, elen = struct.unpack_from("<HHHHHIIIHH", h, 4)
            if ver > 100 or nlen == 0 or method not in (0, 8, 9, 12, 14, 93, 95, 98, 99):
                break  # 진짜 zip 항목 머리가 아님
            if flags & 0x1 and method == 99:  # 암호화(AES)도 길이는 같게 따라간다
                pass
            ext = dev.read_at(pos + 30 + nlen, elen) if elen else b""
            if len(names) < 30:
                names.append(dev.read_at(pos + 30, nlen).decode("utf-8", "replace"))
            if csize == 0xFFFFFFFF or usize == 0xFFFFFFFF:
                z = _zip64(ext, usize == 0xFFFFFFFF, csize == 0xFFFFFFFF)
                if "c" in z:
                    csize = z["c"]
            data = pos + 30 + nlen + elen
            if flags & 0x8:
                if method != 8:
                    break
                csize = _deflate_end(dev, data, limit)
                if csize is None:
                    break
                dpos = data + csize
                d = dev.read_at(dpos, 24)
                skip = 4 if d[:4] == b"PK" else 0
                c32 = struct.unpack_from("<I", d, skip + 4)[0] if len(d) >= skip + 8 else None
                pos = dpos + skip + (12 if c32 == csize else 20)
            else:
                pos = data + csize
            if pos > limit:
                break
            local += 1
        elif sig == b"PK" and len(h) >= 46:
            nlen, elen, clen = struct.unpack_from("<HHH", h, 28)
            pos += 46 + nlen + elen + clen
            cd += 1
        elif sig == b"PK":
            pos += 12 + struct.unpack_from("<Q", h, 4)[0]
        elif sig == b"PK":
            pos += 20
        elif sig == b"PK":
            pos += 6 + struct.unpack_from("<H", h, 4)[0]
        elif sig == b"PK" and len(h) >= 22:
            pos += 22 + struct.unpack_from("<H", h, 20)[0]
            total = struct.unpack_from("<H", h, 10)[0]
            complete = local > 0 and (cd == local and (total == local or total == 0xFFFF))
            break
        else:
            break
    if local == 0 or pos <= off or pos > limit:
        return None
    return {"length": pos - off, "entries": local, "complete": complete, "names": names}


def zip_kind(names):
    """첫 항목들의 이름으로 docx/xlsx/pptx 를 구별한다. 아니면 'zip'."""
    if any(n == "Contents/header.xml" or n.startswith("Contents/section") for n in names):
        return "hwpx"  # 한글(HWPX)은 ZIP 구조이고 Contents/ 아래에 본문이 있다
    if names and names[0] == "[Content_Types].xml" or any(n == "[Content_Types].xml" for n in names):
        for pre, k in ZIP_OFFICE:
            if any(n.startswith(pre) for n in names):
                return k
    return "zip"

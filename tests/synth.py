"""테스트용 합성 파일/디스크 이미지 생성기 (실제 디스크, 실제 사진 불필요)."""
import os
import random
import struct
from datetime import datetime

BS = 4096


def _entry(tag, typ, cnt, val):
    return struct.pack("<HHII", tag, typ, cnt, val)


def make_tiff(make, model, dto):
    mk, md, dt = (s.encode() + b"\0" for s in (make, model, dto))
    assert len(mk) > 4 and len(md) > 4, "4바이트 이하 문자열은 TIFF 에서 인라인 저장이라 이 생성기가 처리하지 않는다"
    ifd0_off = 8
    exif_off = ifd0_off + 2 + 12 * 3 + 4
    data_off = exif_off + 2 + 12 + 4
    o_mk, o_md = data_off, data_off + len(mk)
    o_dt = o_md + len(md)
    ifd0 = (struct.pack("<H", 3) + _entry(0x010F, 2, len(mk), o_mk) + _entry(0x0110, 2, len(md), o_md)
            + _entry(0x8769, 4, 1, exif_off) + struct.pack("<I", 0))
    exif = struct.pack("<H", 1) + _entry(0x9003, 2, len(dt), o_dt) + struct.pack("<I", 0)
    return b"II*\0" + struct.pack("<I", 8) + ifd0 + exif + mk + md + dt


def _entropy(rng, n):
    raw = bytearray(rng.randrange(0, 255) for _ in range(n))  # 0xFF 없음
    for k in range(0, n - 2, 997):  # 바이트 스터핑(FF00)도 섞는다
        raw[k:k + 2] = b"\xff\x00"
    return bytes(raw)


def make_jpeg(rng, entropy_len, tiff=None, w=640, h=480, eoi=True):
    out = b"\xff\xd8"
    if tiff:
        body = b"Exif\0\0" + tiff
        out += b"\xff\xe1" + struct.pack(">H", 2 + len(body)) + body
    out += b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    out += b"\xff\xda" + struct.pack(">H", 12) + b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
    out += _entropy(rng, entropy_len)
    return out + (b"\xff\xd9" if eoi else b"")


def box(typ, payload):
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def make_mvhd(when: datetime):
    secs = int((when - datetime(1904, 1, 1)).total_seconds())
    body = b"\0\0\0\0" + struct.pack(">II", secs, secs) + b"\0" * 88
    return box(b"mvhd", body)


def ftyp(brand):
    return box(b"ftyp", brand + b"\0\0\0\0" + brand)


def make_mp4(rng, mdat_len, when, brand=b"mp42", moov=True, mdat=True):
    out = ftyp(brand)
    if moov:
        out += box(b"moov", make_mvhd(when))
    if mdat:
        out += box(b"mdat", rng.randbytes(mdat_len))
    return out


def make_cr3(rng, mdat_len, tiff1, tiff2):
    uuid = box(b"uuid", b"\0" * 16 + box(b"CMT1", tiff1) + box(b"CMT2", tiff2))
    return ftyp(b"crx ") + box(b"moov", uuid) + box(b"mdat", rng.randbytes(mdat_len))


def build_image(path, items, total_blocks):
    """items: [(블록번호, bytes)]. 나머지는 0. 반환: {블록번호: 길이}"""
    img = bytearray(total_blocks * BS)
    for blk, data in items:
        assert (blk * BS) + len(data) <= len(img)
        img[blk * BS:blk * BS + len(data)] = data
    with open(path, "wb") as f:
        f.write(img)
    return {blk: len(d) for blk, d in items}

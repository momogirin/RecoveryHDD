"""목차(중앙 디렉터리)가 없거나 끝이 잘린 ZIP 에서 온전한 항목만 모아 새 ZIP(복원본)을 만든다.

- 원본 파일은 읽기만 한다. 복원본은 다른 경로에 새로 쓴다.
- 항목마다 압축 데이터를 풀어 CRC32 를 확인하고, 맞는 항목만 넣는다. 지원: 저장(0), deflate(8), bzip2(12).
  그 밖의 방식(lzma, zstd, 암호화 등)은 CRC 를 확인할 수 없어, 데이터 설명자(flag 3)가 없을 때만 헤더의 크기를 믿고
  '확인 못 한 항목'으로 센다.
- 앞에서부터 따라가다 처음 문제가 생긴 곳에서 멈춘다(그 뒤는 어디서 이어지는지 알 수 없다).
"""
import bz2
import os
import struct
import zipfile
import zlib

LH, CD, EOCD, DD = b"PK\x03\x04", b"PK\x01\x02", b"PK\x05\x06", b"PK\x07\x08"
CH = 1 << 20
FF32 = 0xFFFFFFFF
STOP_TEXT = {"central": "목차를 만났어요", "end": "파일 끝에서 끝났어요", "nonheader": "다음 항목 머리를 찾지 못했어요",
             "damaged": "항목 하나가 잘렸거나 손상됐어요", "unsupported": "확인할 수 없는 방식을 만났어요"}


def _zip64(extra, want_usize, want_csize):
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


def _stream_check(f, method, start, csize, has_dd):
    """압축 데이터를 읽어 (사용한 길이, CRC32, 풀린 크기) 를 돌려준다. 실패하면 None.
    csize 가 None 이면(데이터 설명자) 압축 스트림이 끝나는 곳을 직접 찾는다."""
    f.seek(start)
    crc = total = used = 0
    if method == 0:
        left = csize
        while left > 0:
            b = f.read(min(CH, left))
            if not b:
                return None
            crc, total, left = zlib.crc32(b, crc), total + len(b), left - len(b)
        return csize, crc, total
    if method == 8:
        d = zlib.decompressobj(-15)
    elif method == 12:
        d = bz2.BZ2Decompressor()
    else:
        return None
    while True:
        b = f.read(CH)
        if not b:
            return None  # 스트림이 끝나기 전에 파일이 끝났다 = 잘림
        try:
            if method == 8:
                data = b
                while True:
                    out = d.decompress(data, 8 << 20)
                    crc, total = zlib.crc32(out, crc), total + len(out)
                    if d.eof or not d.unconsumed_tail:
                        break
                    data = d.unconsumed_tail
                eof, unused = d.eof, d.unused_data
            else:
                out = d.decompress(b)
                crc, total = zlib.crc32(out, crc), total + len(out)
                eof, unused = d.eof, d.unused_data
        except (zlib.error, OSError, ValueError):
            return None
        if eof:
            return used + len(b) - len(unused), crc, total
        used += len(b)


def scan_entries(f, size):
    """앞에서부터 온전한 항목을 모은다. 반환: (항목 목록, 멈춘 이유 코드)."""
    entries, pos = [], 0
    while True:
        f.seek(pos)
        h = f.read(30)
        if len(h) < 4:
            return entries, "end"
        if h[:4] in (CD, EOCD):
            return entries, "central"
        if len(h) < 30 or h[:4] != LH:
            return entries, "nonheader"
        ver, flags, method, mtime, mdate, crc, csize, usize, nlen, elen = struct.unpack_from("<HHHHHIIIHH", h, 4)
        if nlen == 0 or ver > 100:  # 진짜 항목 머리가 아님(0으로 채워진 가짜 등)
            return entries, "nonheader"
        name, extra = f.read(nlen), f.read(elen)
        start = pos + 30 + nlen + elen
        if csize == FF32 or usize == FF32:
            z = _zip64(extra, usize == FF32, csize == FF32)
            usize, csize = z.get("u", usize), z.get("c", csize)
        has_dd = bool(flags & 8)
        verified = method in (0, 8, 12)
        if verified:
            res = _stream_check(f, method, start, None if has_dd else csize, has_dd) if not (method == 0 and has_dd) else None
            if res is None:
                return entries, "damaged" if not (method == 0 and has_dd) else "unsupported"
            used, got_crc, got_usize = res
            end = start + used
            if has_dd:
                f.seek(end)
                dd = f.read(24)
                s = 4 if dd[:4] == DD else 0
                exp_crc = struct.unpack_from("<I", dd, s)[0] if len(dd) >= s + 4 else None
                if len(dd) >= s + 12 and struct.unpack_from("<II", dd, s + 4) == (used & FF32, got_usize & FF32):
                    end += s + 12
                elif len(dd) >= s + 20 and struct.unpack_from("<QQ", dd, s + 4) == (used, got_usize):
                    end += s + 20
                else:
                    return entries, "damaged"
                csize, usize, crc = used, got_usize, exp_crc
            else:
                if used != csize:
                    return entries, "damaged"
            if got_crc != crc or got_usize != usize:
                return entries, "damaged"
        else:
            if has_dd or start + csize > size:
                return entries, "unsupported" if has_dd else "damaged"
            end = start + csize
        entries.append({"start": pos, "end": end, "flags": flags, "method": method, "time": mtime, "date": mdate, "crc": crc,
                        "csize": csize, "usize": usize, "name": name, "ver": ver, "verified": verified})
        pos = end


def _cd_record(e, lho):
    z64 = e["csize"] >= FF32 or e["usize"] >= FF32 or lho >= FF32
    extra = b""
    if z64:
        body = b""
        if e["usize"] >= FF32:
            body += struct.pack("<Q", e["usize"])
        if e["csize"] >= FF32:
            body += struct.pack("<Q", e["csize"])
        if lho >= FF32:
            body += struct.pack("<Q", lho)
        extra = struct.pack("<HH", 1, len(body)) + body
    return struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 45 if z64 else 20, max(e["ver"], 45) if z64 else e["ver"], e["flags"],
                       e["method"], e["time"], e["date"], e["crc"], FF32 if e["csize"] >= FF32 else e["csize"],
                       FF32 if e["usize"] >= FF32 else e["usize"], len(e["name"]), len(extra), 0, 0, 0, 0,
                       FF32 if lho >= FF32 else lho) + e["name"] + extra


def salvage(src, dst):
    """src 에서 온전한 항목만 모아 dst 에 새 ZIP 을 만든다.
    반환: {"entries","unverified","stopped","stopped_text","bytes","lost"} 또는 None(온전한 항목이 없음).
    lost: 항목 하나가 잘렸거나 손상돼 빠졌다는 뜻(1). 목차를 만났거나 끝에서 끝났으면 0."""
    size = os.path.getsize(src)
    with open(src, "rb") as f:
        entries, why = scan_entries(f, size)
    if not entries:
        return None
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part"
    try:
        with open(src, "rb") as f, open(tmp, "wb") as out:
            lhos = []
            for e in entries:
                lhos.append(out.tell())
                f.seek(e["start"])
                left = e["end"] - e["start"]
                while left > 0:
                    b = f.read(min(CH, left))
                    if not b:
                        raise OSError("원본을 끝까지 읽지 못했습니다")
                    out.write(b)
                    left -= len(b)
            cd_start = out.tell()
            for e, lho in zip(entries, lhos):
                out.write(_cd_record(e, lho))
            cd_size = out.tell() - cd_start
            n = len(entries)
            if n >= 0xFFFF or cd_start >= FF32 or cd_size >= FF32:
                z64_start = out.tell()
                out.write(struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, n, n, cd_size, cd_start))
                out.write(struct.pack("<IIQI", 0x07064B50, 0, z64_start, 1))
                out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, min(n, 0xFFFF), min(n, 0xFFFF), min(cd_size, FF32), min(cd_start, FF32), 0))
            else:
                out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, cd_size, cd_start, 0))
        with zipfile.ZipFile(tmp) as z:  # 새 ZIP 이 열리는지, 항목 수가 맞는지 확인(압축을 다시 풀지는 않는다)
            if len(z.infolist()) != len(entries):
                raise OSError("복원본의 항목 수가 맞지 않습니다")
        os.replace(tmp, dst)
    except (OSError, zipfile.BadZipFile):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    return {"entries": len(entries), "unverified": sum(1 for e in entries if not e["verified"]), "stopped": why,
            "stopped_text": STOP_TEXT.get(why, why), "bytes": os.path.getsize(dst), "lost": 1 if why in ("damaged", "unsupported") else 0}

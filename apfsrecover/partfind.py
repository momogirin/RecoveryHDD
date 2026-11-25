"""사라진 파티션 찾기(읽기 전용).

1) 파티션 표(GPT, MBR)를 읽는다. 표가 깨졌으면 표 없이 진행한다.
2) 표에 적힌 위치 + 디스크 앞쪽 2GB 안의 1MB 경계 + 맥 기본 위치(409640 섹터)에서 파일시스템의 시작 표시를 찾는다.
   APFS(NXSB, 체크섬 확인) · HFS+ · NTFS · exFAT · FAT32.
3) 표에 없는데 시작 표시가 있으면 '표에 없음(사라진 파티션일 수 있음)'으로 알린다.
디스크 전체를 훑지는 않는다(앞쪽과 표에 적힌 위치만). 결과는 추정이 아니라 '그 위치에 그 표시가 있었다'는 사실이다.
"""
import struct

from . import gpt
from .nxsb import NX_MAGIC, fletcher64_ok

MB = 1 << 20
FRONT = 2 << 30
MAC_DEFAULT = 409640 * 512
MBR_TYPES = {0x07: "NTFS/exFAT", 0x0B: "FAT32", 0x0C: "FAT32", 0xAF: "HFS+", 0xEE: "GPT 보호", 0x83: "Linux"}


def sniff(read_at, off):
    """off 위치에서 파일시스템 시작 표시를 찾는다. 반환: {"fs","size","note"} 또는 None."""
    b = read_at(off, 4096)
    if len(b) < 4096:
        return None
    if struct.unpack_from("<I", b, 32)[0] == NX_MAGIC and struct.unpack_from("<H", b, 24)[0] == 1:
        bs, bc = struct.unpack_from("<IQ", b, 36)
        if bs in (4096, 8192, 16384, 32768, 65536) and bc:
            ok = fletcher64_ok(b[:bs]) if bs == 4096 else fletcher64_ok(read_at(off, bs))
            return {"fs": "APFS", "size": bs * bc, "note": "체크섬 맞음" if ok else "체크섬 안 맞음(손상됐을 수 있음)", "ok": ok}
    if b[3:11] == b"NTFS    ":
        bps = struct.unpack_from("<H", b, 11)[0]
        total = struct.unpack_from("<Q", b, 40)[0]
        return {"fs": "NTFS", "size": bps * total if bps else None, "note": "", "ok": True}
    if b[3:11] == b"EXFAT   ":
        length = struct.unpack_from("<Q", b, 72)[0]
        shift = b[108]
        return {"fs": "exFAT", "size": length << shift if shift < 16 else None, "note": "", "ok": True}
    if b[82:90] == b"FAT32   " and b[510:512] == b"\x55\xaa":
        bps = struct.unpack_from("<H", b, 11)[0]
        total = struct.unpack_from("<I", b, 32)[0]
        return {"fs": "FAT32", "size": bps * total if bps else None, "note": "", "ok": True}
    if b[1024:1026] in (b"H+", b"HX"):
        bsz, nblk = struct.unpack_from(">II", b, 1024 + 40)
        return {"fs": "HFS+", "size": bsz * nblk if bsz else None, "note": "", "ok": True}
    return None


def _mbr(read_at):
    b = read_at(0, 512)
    if len(b) < 512 or b[510:512] != b"\x55\xaa":
        return None
    out = []
    for i in range(4):
        e = b[446 + 16 * i:446 + 16 * (i + 1)]
        typ = e[4]
        lba, n = struct.unpack_from("<II", e, 8)
        if typ and n:
            out.append({"number": i + 1, "offset": lba * 512, "size": n * 512, "content": MBR_TYPES.get(typ, "0x%02x" % typ), "name": ""})
    return out


def find(read_at, size=None, on_progress=None, should_cancel=None):
    """반환: {"table": "GPT"|"MBR"|"없음", "parts": [{offset,size,fs,label,in_table,note,ok}], "checked": n, "finished"}"""
    table, entries = "없음", []
    g = None
    try:
        g = gpt.parse_gpt(read_at)
    except OSError:
        g = None
    if g:
        table, entries = "GPT", g["partitions"]
    else:
        m = _mbr(read_at)
        if m and not any(e["content"] == "GPT 보호" for e in m):
            table, entries = "MBR", m
    cands = {e["offset"] for e in entries} | {MAC_DEFAULT} | set(range(0, FRONT, MB)) | {0}
    if size:
        cands = {c for c in cands if c < size}
    found, done = {}, 0
    cands = sorted(cands)
    for c in cands:
        if should_cancel and should_cancel():
            return {"table": table, "parts": _merge(entries, found), "checked": done, "finished": False}
        try:
            s = sniff(read_at, c)
        except OSError:
            s = None
        if s:
            found[c] = s
        done += 1
        if on_progress and done % 64 == 0:
            on_progress({"done": done, "total": len(cands), "found": len(found)})
    return {"table": table, "parts": _merge(entries, found), "checked": done, "finished": True}


def _merge(entries, found):
    parts = []
    seen = set()
    for e in entries:
        s = found.get(e["offset"])
        parts.append({"offset": e["offset"], "size": e["size"], "fs": s["fs"] if s else None, "label": e.get("name") or "",
                      "table_type": e.get("content"), "in_table": True, "ok": bool(s and s.get("ok")),
                      "note": (s["note"] if s else "표에는 있지만 시작 표시를 못 찾음")})
        seen.add(e["offset"])
    for off, s in sorted(found.items()):
        if off in seen:
            continue
        inside = any(p["in_table"] and p["offset"] < off < p["offset"] + p["size"] for p in parts)
        if inside:
            continue  # 표에 있는 파티션 안쪽의 흔적(예: 파일 속 데이터)은 따로 보이지 않는다
        parts.append({"offset": off, "size": s["size"], "fs": s["fs"], "label": "", "table_type": None, "in_table": False,
                      "ok": bool(s.get("ok")), "note": "표에 없음 — 사라진 파티션일 수 있어요. " + s["note"]})
    return sorted(parts, key=lambda p: p["offset"])

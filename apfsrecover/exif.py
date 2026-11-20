"""최소 TIFF/EXIF 파서 (표준 라이브러리만). 읽을 수 있는 필드만 채우고 나머지는 비운다."""
import re
import struct

ISO_RE = re.compile(rb"(\d{4}):(\d\d):(\d\d) (\d\d):(\d\d):(\d\d)")


def to_iso(raw):
    """'YYYY:MM:DD HH:MM:SS' → ISO 문자열. 형식이 다르거나 1990년 이전이면 None."""
    if not raw:
        return None
    m = ISO_RE.match(raw if isinstance(raw, bytes) else raw.encode())
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    if y < 1990 or not (1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60 and s < 61):
        return None
    return "%04d-%02d-%02dT%02d:%02d:%02d" % (y, mo, d, h, mi, s)


def parse_tiff(buf: bytes) -> dict:
    """TIFF 헤더로 시작하는 버퍼에서 make, model, taken(촬영 시각), 픽셀 크기를 읽는다."""
    out = {}
    if len(buf) < 8:
        return out
    e = {b"II": "<", b"MM": ">"}.get(buf[:2])
    if not e or struct.unpack_from(e + "H", buf, 2)[0] != 42:
        return out
    seen = set()

    def ascii_at(valoff, cnt):
        if cnt <= 4:
            raw = buf[valoff:valoff + cnt]
        else:
            o = struct.unpack_from(e + "I", buf, valoff)[0]
            raw = buf[o:o + cnt]
        return raw.split(b"\0")[0].strip()

    def ifd(off, depth):
        if off in seen or depth > 3 or off < 8 or off + 2 > len(buf):
            return
        seen.add(off)
        n = struct.unpack_from(e + "H", buf, off)[0]
        for k in range(min(n, 300)):
            p = off + 2 + 12 * k
            if p + 12 > len(buf):
                break
            tag, typ, cnt = struct.unpack_from(e + "HHI", buf, p)
            vo = p + 8
            try:
                if typ == 2 and tag in (0x010F, 0x0110, 0x0132, 0x9003, 0x9004):
                    raw = ascii_at(vo, cnt)
                    if tag == 0x010F:
                        out.setdefault("make", raw.decode("ascii", "replace"))
                    elif tag == 0x0110:
                        out.setdefault("model", raw.decode("ascii", "replace"))
                    elif tag == 0x9003:
                        out.setdefault("taken", to_iso(raw))
                    elif tag in (0x9004, 0x0132):
                        out.setdefault("taken_fallback", to_iso(raw))
                elif typ in (3, 4) and cnt == 1:
                    val = struct.unpack_from(e + ("H" if typ == 3 else "I"), buf, vo)[0]
                    if tag == 0xA002:
                        out.setdefault("pix_w", val)
                    elif tag == 0xA003:
                        out.setdefault("pix_h", val)
                    elif tag == 0x8769:
                        ifd(val, depth + 1)
            except (struct.error, IndexError):
                continue

    try:
        ifd(struct.unpack_from(e + "I", buf, 4)[0], 0)
    except struct.error:
        pass
    if not out.get("taken") and out.get("taken_fallback"):
        out["taken"] = out["taken_fallback"]
    out.pop("taken_fallback", None)
    return {k: v for k, v in out.items() if v not in (None, "")}

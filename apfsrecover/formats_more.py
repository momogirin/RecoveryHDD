"""추가 파일 형식의 판별과 길이 계산 (읽기 전용): PDF, 한글(HWP)·옛 오피스(OLE), PNG, GIF, WebP, AVI, WAV, BMP, RTF, 7z, SQLite.

판정 원칙은 formats.py 와 같다: 길이가 파일 안의 정보로 확실히 정해지고 구조가 맞으면 기록, 아니면 기록하지 않는다.
각 probe(dev, off, max_len) 는 {"kind","length","structure",...} 또는 None 을 돌려준다.
'ok' 는 구조가 맞다는 뜻이며 파일이 실제로 열린다는 보증이 아니다(열림은 verify 단계).
dev 는 read_at(offset, length) 만 있으면 된다(ReadOnlyDevice 또는 FileAt).
"""
import re
import struct
import zlib

PNG_SIG = b"\x89PNG\r\n\x1a\n"
OLE_SIG = bytes.fromhex("d0cf11e0a1b11ae1")
SEVENZ_SIG = b"7z\xbc\xaf\x27\x1c"
SQLITE_SIG = b"SQLite format 3\x00"

MIN_PDF, MIN_OLE, MIN_PNG, MIN_GIF, MIN_WEBP = 1_000, 2_048, 20_000, 5_000, 10_000
MIN_WAV, MIN_AVI, MIN_BMP, MIN_RTF, MIN_7Z, MIN_SQLITE = 100_000, 100_000, 50_000, 512, 1_024, 8_192
MAX_PDF = 1 << 30
MAX_OLE = 2 << 30
MAX_RTF = 256 << 20

# 카탈로그 종류 -> 판별 키 / 확장자 / 꺼낼 폴더 / 열림 확인에 쓰는 판별 키
OLE_KINDS = ("hwp", "doc", "xls", "ppt")
KIND_PROBE = {"pdf": "pdf", "hwp": "ole", "doc": "ole", "xls": "ole", "ppt": "ole", "png": "png", "gif": "gif",
              "webp": "riff", "avi": "riff", "wav": "riff", "bmp": "bmp", "rtf": "rtf", "7z": "7z", "sqlite": "sqlite"}
KIND_EXT = {"pdf": "pdf", "hwp": "hwp", "hwpx": "hwpx", "doc": "doc", "xls": "xls", "ppt": "ppt", "png": "png",
            "gif": "gif", "webp": "webp", "avi": "avi", "wav": "wav", "bmp": "bmp", "rtf": "rtf", "7z": "7z", "sqlite": "sqlite"}
KIND_DIR = {"pdf": "문서", "hwp": "문서", "hwpx": "문서", "doc": "문서", "xls": "문서", "ppt": "문서", "rtf": "문서",
            "png": "사진", "gif": "사진", "webp": "사진", "bmp": "사진", "avi": "영상", "7z": "압축",
            "wav": "음성·데이터", "sqlite": "음성·데이터"}


class FileAt:
    """파일을 dev.read_at 모양으로 읽는다(열림 확인용)."""
    def __init__(self, path):
        self.f = open(path, "rb")

    def read_at(self, offset, length):
        self.f.seek(offset)
        return self.f.read(length)

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Stream:
    """off 부터 limit 바이트 안에서 앞으로만 읽는 창. 한계를 넘으면 None."""
    WIN = 1 << 20

    def __init__(self, dev, off, limit):
        self.dev, self.base, self.limit = dev, off, max(0, limit)
        self.pos, self.buf, self.bpos = 0, b"", 0

    def take(self, n):
        if n < 0 or self.pos + n > self.limit:
            return None
        if not (self.bpos <= self.pos and self.pos + n <= self.bpos + len(self.buf)):
            want = min(max(n, self.WIN), self.limit - self.pos)
            self.buf, self.bpos = self.dev.read_at(self.base + self.pos, want), self.pos
        a = self.pos - self.bpos
        data = self.buf[a:a + n]
        if len(data) < n:
            return None
        self.pos += n
        return data

    def skip(self, n):
        if n < 0 or self.pos + n > self.limit:
            return False
        self.pos += n
        return True


# ---------------------------------------------------------------- 판별(블록 시작 16바이트만 본다)
_BY_FIRST = {0xFF: "jpg", 0x50: "zip", 0x25: "pdf", 0xD0: "ole", 0x89: "png", 0x47: "gif", 0x52: "riff",
             0x42: "bmp", 0x7B: "rtf", 0x37: "7z", 0x53: "sqlite"}
_SIG_OK = {
    "jpg": lambda h: h[:3] == b"\xff\xd8\xff", "zip": lambda h: h[:2] == b"PK", "pdf": lambda h: h[:5] == b"%PDF-",
    "ole": lambda h: h[:8] == OLE_SIG, "png": lambda h: h[:8] == PNG_SIG, "gif": lambda h: h[:6] in (b"GIF87a", b"GIF89a"),
    "riff": lambda h: h[:4] == b"RIFF" and h[8:12] in (b"WEBP", b"AVI ", b"WAVE"), "bmp": lambda h: h[:2] == b"BM",
    "rtf": lambda h: h[:6] == b"{\\rtf1", "7z": lambda h: h[:6] == SEVENZ_SIG, "sqlite": lambda h: h[:16] == SQLITE_SIG,
}


def sniff(h):
    """4096 바이트로 정렬된 블록의 첫 16바이트로 후보 종류를 정한다. 아니면 None. (ftyp 는 4바이트 뒤에 있다)"""
    if not h:
        return None
    if h[4:8] == b"ftyp":
        return "bmff"
    k = _BY_FIRST.get(h[0])
    return k if k and _SIG_OK[k](h) else None


# ---------------------------------------------------------------- PDF
_OBJ = re.compile(rb"\d+\s+\d+\s+obj")
_LIN = re.compile(rb"/Linearized\s+[\d.]+.{0,300}?/L\s+(\d+)", re.S)
_CONT = re.compile(rb"\d+\s+\d+\s+obj|endstream|endobj|>>")


def pdf_probe(dev, off, max_len):
    lim = min(max_len, MAX_PDF)
    head = dev.read_at(off, 1024)
    if not re.match(rb"%PDF-(1\.[0-7]|2\.0)", head) or not _OBJ.search(head):
        return None  # 글에서 "%PDF-" 만 언급한 것 같은 가짜를 거른다
    m = _LIN.search(head)
    if m:  # 선형화 PDF 는 길이가 머리에 적혀 있다
        L = int(m.group(1))
        if MIN_PDF <= L <= lim:
            tail = dev.read_at(off + max(0, L - 1024), min(1024, L))
            if b"%%EOF" in tail:
                return {"kind": "pdf", "length": L, "structure": "ok" if b"startxref" in tail else "suspect"}
    base, carry = 0, b""
    while base < lim:
        data = dev.read_at(off + base, min(4 << 20, lim - base))
        if not data:
            break
        buf, start = carry + data, base - len(carry)
        i = buf.find(b"%%EOF")
        while i != -1:
            end = start + i + 5
            tail = dev.read_at(off + end, 64)
            j = 0
            while j < len(tail) and j < 2 and tail[j] in b"\r\n":
                j += 1
            nxt = tail[j:].lstrip(b" \t\r\n")
            if not _CONT.match(nxt):  # 뒤에 같은 파일이 이어지는 표시(증분 저장·내장 파일)가 없으면 여기가 끝
                length = end + j
                if length < MIN_PDF:
                    return None
                before = dev.read_at(off + max(0, end - 1024), min(1024, end))
                return {"kind": "pdf", "length": length, "structure": "ok" if b"startxref" in before else "suspect"}
            i = buf.find(b"%%EOF", i + 1)
        carry, base = buf[-4:], base + len(data)
    return None


# ---------------------------------------------------------------- OLE (한글 HWP 5.x, doc, xls, ppt)
_FREE, _MAXREG = 0xFFFFFFFF, 0xFFFFFFFA


def ole_probe(dev, off, max_len):
    h = dev.read_at(off, 512)
    if len(h) < 512 or h[:8] != OLE_SIG:
        return None
    major, bo, sh = struct.unpack_from("<HHH", h, 0x1A)
    if bo != 0xFFFE or (major, sh) not in ((3, 9), (4, 12)):
        return None
    ss = 1 << sh
    nfat, dirsec, _, _, _, _, difat_first, ndifat = struct.unpack_from("<IIIIIIII", h, 0x2C)
    if not 1 <= nfat <= 20000 or ndifat > 2000:
        return None
    ids = [x for x in struct.unpack_from("<109I", h, 0x4C) if x < _MAXREG]
    sec, per = difat_first, ss // 4 - 1
    for _ in range(ndifat):
        if sec >= _MAXREG:
            break
        d = dev.read_at(off + (sec + 1) * ss, ss)
        if len(d) < ss:
            return None
        vals = struct.unpack("<%dI" % (ss // 4), d)
        ids += [x for x in vals[:per] if x < _MAXREG]
        sec = vals[per]
    if len(ids) < nfat:
        return None
    fat = []
    for sid in ids[:nfat]:
        d = dev.read_at(off + (sid + 1) * ss, ss)
        if len(d) < ss:
            return None
        fat += struct.unpack("<%dI" % (ss // 4), d)
    last = max((i for i, v in enumerate(fat) if v != _FREE), default=-1)
    if last < 0:
        return None
    length = ss * (last + 2)  # 머리 구역 1개 + 사용 구역 수
    if length > min(max_len, MAX_OLE) or length < MIN_OLE:
        return None
    names, sid, seen = set(), dirsec, 0
    while sid < _MAXREG and seen < 4096:
        if sid >= len(fat):
            return None
        d = dev.read_at(off + (sid + 1) * ss, ss)
        if len(d) < ss:
            return None
        for k in range(ss // 128):
            e = d[k * 128:(k + 1) * 128]
            nlen, typ = struct.unpack_from("<HB", e, 64)
            name = e[:max(0, nlen - 2)].decode("utf-16le", "replace") if 2 <= nlen <= 64 else ""
            if seen == 0 and k == 0 and (name != "Root Entry" or typ != 5):
                return None
            if typ in (1, 2, 5) and name:
                names.add(name)
        sid, seen = fat[sid], seen + 1
    if "Root Entry" not in names:
        return None
    if "FileHeader" in names and ("BodyText" in names or "DocInfo" in names):
        kind = "hwp"
    elif "WordDocument" in names:
        kind = "doc"
    elif "Workbook" in names or "Book" in names:
        kind = "xls"
    elif "PowerPoint Document" in names:
        kind = "ppt"
    else:
        return None  # 그 밖의 OLE(msi, Thumbs.db 등)는 기록하지 않는다
    return {"kind": kind, "length": length, "structure": "ok"}


# ---------------------------------------------------------------- PNG / GIF
def png_probe(dev, off, max_len):
    s = Stream(dev, off, max_len)
    if s.take(8) != PNG_SIG:
        return None
    hd = s.take(8)
    if not hd or hd[4:] != b"IHDR" or struct.unpack(">I", hd[:4])[0] != 13:
        return None
    body, crc = s.take(13), s.take(4)
    if body is None or crc is None or zlib.crc32(b"IHDR" + body) != struct.unpack(">I", crc)[0]:
        return None
    w, h = struct.unpack(">II", body[:8])
    if not (0 < w <= 1 << 20 and 0 < h <= 1 << 20):
        return None
    for _ in range(200_000):
        hd = s.take(8)
        if hd is None:
            return None
        ln, typ = struct.unpack(">I4s", hd)
        if ln > 0x7FFFFFFF or not typ.isalpha() or not s.skip(ln + 4):
            return None
        if typ == b"IEND":
            if ln != 0 or s.pos < MIN_PNG:
                return None
            return {"kind": "png", "length": s.pos, "structure": "ok", "width": w, "height": h}
    return None


def _subblocks(s):
    for _ in range(5_000_000):
        n = s.take(1)
        if n is None:
            return False
        if n[0] == 0:
            return True
        if not s.skip(n[0]):
            return False
    return False


def gif_probe(dev, off, max_len):
    s = Stream(dev, off, max_len)
    hd = s.take(13)
    if not hd or hd[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    w, h, flags = struct.unpack_from("<HHB", hd, 6)
    if w == 0 or h == 0 or (flags & 0x80 and not s.skip(3 << ((flags & 7) + 1))):
        return None
    frames = 0
    for _ in range(2_000_000):
        b = s.take(1)
        if b is None:
            return None
        if b == b"\x3b":
            if frames == 0 or s.pos < MIN_GIF:
                return None
            return {"kind": "gif", "length": s.pos, "structure": "ok", "width": w, "height": h, "frames": frames}
        if b == b"\x21":
            if s.take(1) is None or not _subblocks(s):
                return None
        elif b == b"\x2c":
            d = s.take(9)
            if d is None or (d[8] & 0x80 and not s.skip(3 << ((d[8] & 7) + 1))) or s.take(1) is None or not _subblocks(s):
                return None
            frames += 1
        else:
            return None
    return None


# ---------------------------------------------------------------- RIFF (WebP, AVI, WAV) / BMP
def riff_probe(dev, off, max_len):
    h = dev.read_at(off, 24)
    if len(h) < 24 or h[:4] != b"RIFF":
        return None
    size = struct.unpack_from("<I", h, 4)[0]
    form, first = h[8:12], h[12:16]
    if form == b"WEBP" and first in (b"VP8 ", b"VP8L", b"VP8X"):
        kind, lo = "webp", MIN_WEBP
    elif form == b"AVI " and first == b"LIST" and h[20:24] == b"hdrl":
        kind, lo = "avi", MIN_AVI
    elif form == b"WAVE" and first == b"fmt ":
        kind, lo = "wav", MIN_WAV
    else:
        return None
    length = size + 8 + (size & 1)
    if length > max_len or length < lo:
        return None
    return {"kind": kind, "length": length, "structure": "ok"}


def bmp_probe(dev, off, max_len):
    h = dev.read_at(off, 64)
    if len(h) < 30 or h[:2] != b"BM":
        return None
    fsize, res, poff, dib = struct.unpack_from("<IIII", h, 2)
    if res != 0 or dib not in (12, 40, 52, 56, 64, 108, 124):
        return None
    if dib == 12:
        w, ht, planes, bpp = struct.unpack_from("<HHHH", h, 18)
        comp = 0
    else:
        w, ht, planes, bpp, comp = struct.unpack_from("<iiHHI", h, 18)
        ht = abs(ht)
    if planes != 1 or bpp not in (1, 4, 8, 16, 24, 32) or comp not in (0, 1, 2, 3):
        return None
    if not (0 < w <= 30000 and 0 < ht <= 30000) or poff < 14 + dib or poff >= fsize or fsize > max_len or fsize < MIN_BMP:
        return None
    if comp == 0:
        need = poff + ((w * bpp + 31) // 32) * 4 * ht
        if fsize < need or fsize - need > 65536:
            return None
    return {"kind": "bmp", "length": fsize, "structure": "ok", "width": w, "height": ht}


# ---------------------------------------------------------------- RTF / 7z / SQLite
_BRACE = re.compile(rb"\\[\\{}]|[{}]")


def rtf_probe(dev, off, max_len):
    lim = min(max_len, MAX_RTF)
    depth, base = 0, 0
    while base < lim:
        data = dev.read_at(off + base, min(1 << 20, lim - base))
        if not data:
            return None
        if base == 0 and (data[:6] != b"{\\rtf1" or sum(1 for b in data[:4096] if b >= 0x80) > 80):
            return None  # 글자 파일이어야 한다
        cut = len(data) - 1 if data.endswith(b"\\") and base + len(data) < lim else len(data)
        cut = cut or len(data)  # 경계에서 역슬래시 하나만 남은 경우(무한 반복 방지)
        nul = data.find(b"\0")  # 파일 뒤의 0 채움은 정상이다. 닫는 괄호보다 앞에 NUL 이 있을 때만 글 파일이 아니다
        end = cut if nul == -1 else min(cut, nul)
        for m in _BRACE.finditer(data, 0, end):
            t = m.group()
            if t == b"{":
                depth += 1
            elif t == b"}":
                depth -= 1
                if depth == 0:
                    length = base + m.end()
                    return {"kind": "rtf", "length": length, "structure": "ok"} if length >= MIN_RTF else None
        if nul != -1:
            return None  # 끝나기 전에 NUL 을 만났다
        base += cut
    return None


def sevenz_probe(dev, off, max_len):
    h = dev.read_at(off, 32)
    if len(h) < 32 or h[:6] != SEVENZ_SIG or zlib.crc32(h[12:32]) != struct.unpack_from("<I", h, 8)[0]:
        return None
    noff, nsize, ncrc = struct.unpack_from("<QQI", h, 12)
    length = 32 + noff + nsize
    if length > max_len or length < MIN_7Z or nsize == 0 or nsize > 64 << 20:
        return None
    nh = dev.read_at(off + 32 + noff, nsize)
    ok = len(nh) == nsize and zlib.crc32(nh) == ncrc
    return {"kind": "7z", "length": length, "structure": "ok" if ok else "suspect"}


def sqlite_probe(dev, off, max_len):
    h = dev.read_at(off, 100)
    if len(h) < 100 or h[:16] != SQLITE_SIG:
        return None
    ps = struct.unpack_from(">H", h, 16)[0]
    ps = 65536 if ps == 1 else ps
    if ps < 512 or ps > 65536 or ps & (ps - 1) or (h[21], h[22], h[23]) != (64, 32, 32):
        return None
    chg, pages = struct.unpack_from(">II", h, 24)
    if pages == 0 or chg != struct.unpack_from(">I", h, 92)[0] or struct.unpack_from(">I", h, 56)[0] not in (0, 1, 2, 3):
        return None
    length = pages * ps
    if length > max_len or length < MIN_SQLITE:
        return None
    return {"kind": "sqlite", "length": length, "structure": "ok", "pages": pages}


PROBES = {"pdf": pdf_probe, "ole": ole_probe, "png": png_probe, "gif": gif_probe, "riff": riff_probe,
          "bmp": bmp_probe, "rtf": rtf_probe, "7z": sevenz_probe, "sqlite": sqlite_probe}


def probe(key, dev, off, max_len):
    return PROBES[key](dev, off, max_len)

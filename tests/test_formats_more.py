"""추가 형식(PDF, 한글·옛 오피스, PNG, GIF, WebP, AVI, WAV, BMP, RTF, 7z, SQLite) 찾기와 열림 확인.
파일은 모두 이 테스트가 만든 가짜 데이터다(실제 디스크와 무관)."""
import io
import os
import random
import sqlite3
import struct
import tempfile
import unittest
import wave
import zlib

from apfsrecover import catalog, formats_more as fm, verify
from apfsrecover.blockdev import ReadOnlyDevice
from apfsrecover.formats import zip_kind
from . import synth

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

FREE, END, FATSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD


def noise(r, n):
    return bytes(r.randrange(256) for _ in range(n))


def make_cfb(names, filler_sectors=4):
    """아주 작은 OLE(복합 문서): 머리 + FAT 1개 + 디렉터리 1개 + 채움 구역."""
    ent = bytearray()
    for i, (nm, typ) in enumerate([("Root Entry", 5)] + [(n, 2) for n in names]):
        e = bytearray(128)
        raw = nm.encode("utf-16le") + b"\0\0"
        e[:len(raw)] = raw
        struct.pack_into("<HBB", e, 64, len(raw), typ, 1)
        struct.pack_into("<III", e, 68, FREE, FREE, FREE)
        ent += e
    ent += bytes(512 - len(ent))
    total = 2 + filler_sectors
    fat = [FATSECT, END] + [END] * filler_sectors + [FREE] * (128 - total)
    hdr = bytearray(512)
    hdr[:8] = fm.OLE_SIG
    struct.pack_into("<HHH", hdr, 0x18, 0x3E, 3, 0xFFFE)
    struct.pack_into("<HH", hdr, 0x1E, 9, 6)
    struct.pack_into("<IIIIIIII", hdr, 0x2C, 1, 1, 0, 4096, END, 0, END, 0)
    struct.pack_into("<109I", hdr, 0x4C, 0, *([FREE] * 108))
    return bytes(hdr) + struct.pack("<128I", *fat) + bytes(ent) + b"\xAB" * (512 * filler_sectors)


def make_pdf(r, kind="plain"):
    body = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    body += b"2 0 obj\n<< /Length 6000 >>\nstream\n" + noise(r, 6000) + b"\nendstream\nendobj\n"
    if kind == "embedded":  # 안에 다른 PDF 의 끝 표시가 들어 있는 경우
        body += b"3 0 obj\n<< /Length 40 >>\nstream\n%PDF-1.3\n1 0 obj\n<<>>\nendobj\n%%EOF\nendstream\nendobj\n"
    body += b"xref\n0 3\n0000000000 65535 f \ntrailer\n<< /Root 1 0 R >>\nstartxref\n100\n%%EOF\n"
    if kind == "incremental":  # 증분 저장: 끝 표시 뒤에 같은 파일이 이어진다
        body += b"4 0 obj\n<< /Foo 1 >>\nendobj\nxref\n0 1\ntrailer\n<< >>\nstartxref\n999\n%%EOF\n"
    return body


def make_7z(r):
    payload = noise(r, 5000)
    nh = noise(r, 120)
    start = struct.pack("<QQI", len(payload), len(nh), zlib.crc32(nh))
    return b"7z\xbc\xaf\x27\x1c\x00\x04" + struct.pack("<I", zlib.crc32(start)) + start + payload + nh


def make_avi(r, size=120_000):
    chunk = b"LIST" + struct.pack("<I", 4) + b"hdrl"
    junk_len = size - 8 - 4 - len(chunk) - 8
    body = b"AVI " + chunk + b"JUNK" + struct.pack("<I", junk_len) + noise(r, junk_len)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def make_wav(frames=60000):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes((bytes(range(256)) * (frames * 2 // 256 + 1))[:frames * 2])
    return b.getvalue()


def make_rtf(r):
    body = b"{\\rtf1\\ansi{\\fonttbl{\\f0 Malgun;}}\\pard Hello \\{ not a group \\} still text\\par "
    body += b"{\\pict\\pngblip " + b"".join(b"%02x" % r.randrange(256) for _ in range(800)) + b"}\\par}"
    return body


def make_sqlite(path):
    con = sqlite3.connect(path)
    con.execute("create table t(a integer, b text)")
    con.executemany("insert into t values(?,?)", [(i, "x" * 80) for i in range(300)])
    con.commit(); con.close()
    return open(path, "rb").read()


def pil_bytes(fmt, r, size=(300, 300), **kw):
    im = Image.frombytes("RGB", size, noise(r, size[0] * size[1] * 3))
    if fmt in ("GIF",):
        im = im.convert("P")
    b = io.BytesIO()
    im.save(b, fmt, **kw)
    return b.getvalue()


class MoreFormatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = random.Random(11)

    def tearDown(self):
        self.tmp.cleanup()

    def image(self, files, blocks=900):
        p = os.path.join(self.tmp.name, "d.img")
        synth.build_image(p, files, blocks)
        return p

    def probe(self, key, data):
        p = self.image([(2, data)], 2 + len(data) // 4096 + 4)
        with ReadOnlyDevice(p) as dev:
            return fm.probe(key, dev, 2 * 4096, 1 << 30)

    # ---- 종류별 길이
    def test_pdf_plain_incremental_embedded(self):
        for kind in ("plain", "incremental", "embedded"):
            d = make_pdf(self.r, kind)
            res = self.probe("pdf", d)
            self.assertEqual((res["length"], res["structure"]), (len(d), "ok"), kind)
        self.assertIsNone(self.probe("pdf", ("%PDF-1.4 이 글은 PDF 를 언급만 합니다. " * 100).encode("utf-8")))

    def test_pdf_linearized_uses_declared_length(self):
        pre = b"%PDF-1.4\n1 0 obj\n<< /Linearized 1 /L @@@@@@@@ /H [ 1 2 ] >>\nendobj\n"
        body = pre + noise(self.r, 3000) + b"\nstartxref\n0\n%%EOF\n"
        total = len(body)
        body = body.replace(b"@@@@@@@@", b"%08d" % total, 1)
        self.assertEqual(len(body), total)
        res = self.probe("pdf", body + b"\0" * 5000)
        self.assertEqual(res["length"], total)

    def test_ole_kinds(self):
        for names, kind in ((["FileHeader", "DocInfo", "BodyText"], "hwp"), (["WordDocument"], "doc"),
                            (["Workbook"], "xls"), (["PowerPoint Document"], "ppt")):
            d = make_cfb(names)
            res = self.probe("ole", d + b"\0" * 9000)
            self.assertEqual((res["kind"], res["length"]), (kind, len(d)), kind)
        self.assertIsNone(self.probe("ole", make_cfb(["Something"])))   # 그 밖의 OLE 는 기록하지 않는다

    @unittest.skipUnless(Image, "Pillow 필요")
    def test_images(self):
        for fmt, key, kind in (("PNG", "png", "png"), ("GIF", "gif", "gif"), ("WEBP", "riff", "webp"), ("BMP", "bmp", "bmp")):
            d = pil_bytes(fmt, self.r, **({"lossless": True} if fmt == "WEBP" else {}))
            res = self.probe(key, d + b"\0" * 5000)
            self.assertIsNotNone(res, fmt)
            self.assertEqual((res["kind"], res["length"]), (kind, len(d)), fmt)

    def test_truncated_png_is_not_recorded(self):
        if not Image:
            self.skipTest("Pillow 필요")
        d = pil_bytes("PNG", self.r)
        self.assertIsNone(self.probe("png", d[:len(d) // 2]))

    def test_riff_avi_wav_and_wrong_size(self):
        a, w = make_avi(self.r), make_wav()
        self.assertEqual(self.probe("riff", a + b"\0" * 5000)["length"], len(a))
        res = self.probe("riff", w + b"\0" * 5000)
        self.assertEqual((res["kind"], res["length"]), ("wav", len(w)))
        p = self.image([(2, a[:len(a) // 2])], 2 + len(a) // 4096 + 4)
        with ReadOnlyDevice(p) as dev:                                       # 길이 정보가 검사 범위보다 크면 기록하지 않는다
            self.assertIsNone(fm.probe("riff", dev, 2 * 4096, len(a) // 2))

    def test_rtf_7z_sqlite(self):
        d = make_rtf(self.r)
        self.assertEqual(self.probe("rtf", d + b"\0" * 5000)["length"], len(d))   # 파일 뒤의 0 채움은 정상
        self.assertEqual(self.probe("rtf", d + b" more text " * 10)["length"], len(d))
        self.assertIsNone(self.probe("rtf", d[:300] + b"\0\0" + d[300:]))          # 글 중간에 NUL 이 있으면 글 파일이 아니다
        self.assertIsNone(self.probe("rtf", d[:-1]))                              # 닫는 괄호가 없으면 끝을 알 수 없다
        z = make_7z(self.r)
        res = self.probe("7z", z + b"\0" * 5000)
        self.assertEqual((res["length"], res["structure"]), (len(z), "ok"))
        bad = bytearray(z); bad[-10] ^= 0xFF                                  # 끝 머리 한 바이트 손상 -> 의심
        self.assertEqual(self.probe("7z", bytes(bad))["structure"], "suspect")
        db = make_sqlite(os.path.join(self.tmp.name, "a.db"))
        res = self.probe("sqlite", db + b"\0" * 5000)
        self.assertEqual((res["length"], res["structure"]), (len(db), "ok"))

    def test_hwpx_is_recognized_from_zip_names(self):
        self.assertEqual(zip_kind(["mimetype", "version.xml", "Contents/header.xml", "Contents/section0.xml"]), "hwpx")
        self.assertEqual(zip_kind(["[Content_Types].xml", "word/document.xml"]), "docx")

    # ---- 검사(catalog) 통합: 여러 형식이 섞인 디스크
    def test_catalog_finds_everything_and_ignores_text_mentions(self):
        files = [(2, make_pdf(self.r, "incremental")), (40, make_cfb(["WordDocument"]) + b""), (80, make_7z(self.r)),
                 (120, make_avi(self.r)), (200, make_wav()), (300, make_rtf(self.r) + b" t"),
                 (400, ("%PDF-1.4 PDF 라는 단어만 있는 글 " * 200).encode("utf-8"))]
        db = make_sqlite(os.path.join(self.tmp.name, "a.db"))
        files.append((500, db))
        if Image:
            files += [(600, pil_bytes("PNG", self.r)), (700, pil_bytes("BMP", self.r))]
        img = self.image(files, 900)
        sess = os.path.join(self.tmp.name, "s")
        self.assertEqual(catalog.catalog(img, sess, 0, 900, fresh=True), 0)
        recs = catalog.read_jsonl(os.path.join(sess, "catalog.jsonl"))
        got = {r["kind"]: (r["offset"] // 4096, r["length"], r["structure"]) for r in recs}
        self.assertEqual(got["pdf"][0], 2)
        self.assertEqual(got["doc"][0], 40)
        for k in ("7z", "avi", "wav", "rtf", "sqlite"):
            self.assertIn(k, got, k)
        self.assertEqual(sum(1 for r in recs if r["kind"] == "pdf"), 1)       # 글 속 "%PDF-" 는 파일이 아니다
        if Image:
            self.assertIn("png", got)
            self.assertIn("bmp", got)
        self.assertTrue(all(r["structure"] == "ok" for r in recs))

    # ---- 열림 확인
    def test_verify_new_kinds(self):
        tools = verify.available_tools()
        d = os.path.join(self.tmp.name, "out"); os.makedirs(d)
        samples = {"pdf": make_pdf(self.r), "doc": make_cfb(["WordDocument"]), "7z": make_7z(self.r), "avi": make_avi(self.r),
                   "wav": make_wav(), "rtf": make_rtf(self.r)}
        for kind, data in samples.items():
            p = os.path.join(d, "f." + kind)
            open(p, "wb").write(data)
            res = verify.verify_file(p, kind, len(data), tools)
            self.assertEqual(res["status"], "ok", (kind, res))
            bad = bytearray(data)                                                  # 구조가 깨지는 위치를 손상
            if kind == "doc":
                bad[1024:1536] = bytes(512)                                        # 디렉터리 구역
            elif kind in ("avi", "wav"):
                bad[4:8] = struct.pack("<I", len(data))                            # 길이 정보
            elif kind == "rtf":
                bad[-1:] = b" "                                                    # 닫는 괄호
            elif kind == "7z":
                bad[-10] ^= 0xFF                                                   # 끝 머리
            else:
                bad[-20:] = bytes(20)                                              # PDF 끝 표시
            open(p, "wb").write(bytes(bad))
            self.assertEqual(verify.verify_file(p, kind, len(data), tools)["status"], "fail", kind)
        db = os.path.join(d, "a.sqlite")
        data = make_sqlite(db)
        self.assertEqual(verify.verify_file(db, "sqlite", len(data), tools)["status"], "ok")
        if Image:
            for fmt, kind in (("PNG", "png"), ("GIF", "gif"), ("BMP", "bmp")):
                b = pil_bytes(fmt, self.r)
                p = os.path.join(d, "i." + kind)
                open(p, "wb").write(b)
                self.assertEqual(verify.verify_file(p, kind, len(b), tools)["status"], "ok", kind)


if __name__ == "__main__":
    unittest.main()

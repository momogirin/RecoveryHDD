"""ZIP 찾기: 보통 zip, 스트리밍(data descriptor) zip, docx, 잘린 zip."""
import io
import os
import random
import tempfile
import unittest
import zipfile

from apfsrecover import catalog, verify
from apfsrecover.blockdev import ReadOnlyDevice
from apfsrecover.formats import zip_kind, zip_probe
from . import synth


class _NoSeek(io.RawIOBase):
    """seek 불가 스트림: zipfile 이 data descriptor(플래그 0x8)를 쓰게 만든다(맥 보관함 방식)."""
    def __init__(self): self.b = io.BytesIO()
    def writable(self): return True
    def write(self, d): return self.b.write(d)
    def tell(self): return self.b.tell()


def make_zip(r, n=5, method=zipfile.ZIP_DEFLATED, stream=False, names=None):
    out = _NoSeek() if stream else io.BytesIO()
    with zipfile.ZipFile(out, "w", method) as z:
        for i in range(n):
            nm = names[i] if names else "photo_%d.jpg" % i
            z.writestr(nm, bytes(r.randrange(256) for _ in range(3000)) + b"A" * 20000)
    return (out.b if stream else out).getvalue()


class ZipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = random.Random(7)

    def tearDown(self):
        self.tmp.cleanup()

    def image(self, files, blocks=600):
        p = os.path.join(self.tmp.name, "d.img")
        synth.build_image(p, files, blocks)
        return p

    def probe(self, data):
        p = self.image([(2, data)])
        with ReadOnlyDevice(p) as dev:
            return zip_probe(dev, 2 * 4096, 1 << 30)

    def test_lengths_for_all_styles(self):
        for kw in ({}, {"method": zipfile.ZIP_STORED}, {"stream": True}, {"n": 1}, {"n": 200}):
            data = make_zip(self.r, **kw)
            res = self.probe(data)
            self.assertIsNotNone(res, kw)
            self.assertEqual(res["length"], len(data), kw)
            self.assertTrue(res["complete"], kw)

    def test_truncated_zip_is_suspect_not_complete(self):
        data = make_zip(self.r, 8)
        res = self.probe(data[:len(data) // 2])
        self.assertTrue(res is None or not res["complete"])

    def test_not_a_zip(self):
        self.assertIsNone(self.probe(b"PK\x03\x04" + b"\0" * 100))

    def test_office_kind(self):
        names = ["[Content_Types].xml", "word/document.xml"]
        self.assertEqual(zip_kind(names), "docx")
        self.assertEqual(zip_kind(["a.jpg"]), "zip")

    def test_catalog_finds_zip_between_other_files(self):
        z = make_zip(self.r, 6)
        jpg = synth.make_jpeg(self.r, 200_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35"))
        img = self.image([(2, jpg), (100, z)], 400)
        sess = os.path.join(self.tmp.name, "s")
        self.assertEqual(catalog.catalog(img, sess, 0, 400, fresh=True), 0)
        recs = {r["kind"]: r for r in catalog.read_jsonl(os.path.join(sess, "catalog.jsonl"))}
        self.assertEqual(recs["zip"]["length"], len(z))
        self.assertEqual(recs["zip"]["structure"], "ok")
        self.assertIn("jpeg", recs)

    def test_verify_zip(self):
        z = make_zip(self.r, 3)
        p = os.path.join(self.tmp.name, "a.zip")
        open(p, "wb").write(z)
        tools = verify.available_tools()
        self.assertEqual(verify.verify_file(p, "zip", len(z), tools)["status"], "ok")
        bad = bytearray(z); bad[200] ^= 0xFF  # 압축 데이터 한 바이트 손상
        open(p, "wb").write(bytes(bad))
        self.assertEqual(verify.verify_file(p, "zip", len(z), tools)["status"], "fail")


if __name__ == "__main__":
    unittest.main()
